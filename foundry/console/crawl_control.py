"""Local process supervisor and durable metadata for OSINT crawl runs."""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from foundry.paths import crawl_path, production_event_log_path
from foundry.source_adapters import SourceAdapterError, load_source_contracts

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_REGISTRY = REPO_ROOT / "config" / "source_registry.json"
REGIONAL_SOURCE_IDS = (
    "opensky",
    "adsb-lol",
    "adsb-fi",
    "gdelt-events",
    "aisstream",
    "openstreetmap",
    "usgs-earthquakes",
    "nasa-firms",
    "global-fishing-watch",
    "gdacs",
)
SOURCE_NAMES = {
    "opensky": "OpenSky Network",
    "adsb-lol": "ADSB.lol",
    "adsb-fi": "adsb.fi",
    "gdelt-events": "GDELT Events",
    "aisstream": "AISStream",
    "openstreetmap": "OpenStreetMap",
    "usgs-earthquakes": "USGS Earthquake Catalog",
    "nasa-firms": "NASA FIRMS",
    "global-fishing-watch": "Global Fishing Watch",
    "gdacs": "GDACS",
}
CLI_SOURCE_IDS = {"gdelt-events": "gdelt"}
CREDENTIAL_ENV = {
    "aisstream": "AISSTREAM_API_KEY",
    "nasa-firms": "FIRMS_MAP_KEY",
    "global-fishing-watch": "GFW_API_ACCESS_TOKEN",
}
SECRET_ENV_NAMES = (
    "AISSTREAM_API_KEY",
    "FIRMS_MAP_KEY",
    "GFW_API_ACCESS_TOKEN",
    "FOUNDRY_CONSOLE_TOKEN",
    "FOUNDRY_PROVENANCE_TOKENS",
)
ACTIVE_STATUSES = ("queued", "running", "stopping")
DEFAULT_CONFIG = {"source_id": "all", "max_records": 200, "duration_seconds": 15}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def redact_secrets(text: str) -> str:
    """Remove configured credentials from captured command output."""
    for name in SECRET_ENV_NAMES:
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)([?&](?:map_key|api_key|token)=)[^&\s]+",
        r"\1[REDACTED]",
        text,
    )
    return re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1[REDACTED]", text)


class CrawlRunManager:
    """Launch the existing bounded ingestion CLI and retain monitorable run state."""

    def __init__(self, service_root: Path, raw_root: Path) -> None:
        """Initialize persistent run metadata, logs and raw archive directories."""
        self.service_root = service_root.expanduser().resolve()
        self.raw_root = raw_root.expanduser().resolve()
        self.log_root = self.service_root / "logs"
        self.db_path = self.service_root / "crawler.sqlite3"
        self.service_root.mkdir(parents=True, exist_ok=True)
        self.log_root.mkdir(parents=True, exist_ok=True)
        self.raw_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._log_readers: dict[str, threading.Thread] = {}
        self._initialize()

    @classmethod
    def from_defaults(cls) -> CrawlRunManager:
        """Build paths under the configured external crawl root."""
        root = crawl_path()
        return cls(root / "crawler-service", root / "osint-vietnam-scs")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a transactional SQLite connection and always close its handle."""
        connection = sqlite3.connect(self.db_path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 10000")
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    exit_code INTEGER,
                    pid INTEGER,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    max_records INTEGER NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    log_path TEXT NOT NULL,
                    summary_json TEXT,
                    error TEXT
                )"""
            )

    def sources(self) -> list[dict[str, Any]]:
        """Return regional source contracts and credential readiness, never secrets."""
        try:
            contracts = load_source_contracts(SOURCE_REGISTRY)
        except SourceAdapterError as exc:
            raise RuntimeError(str(exc)) from exc
        result = []
        for source_id in REGIONAL_SOURCE_IDS:
            contract = contracts[source_id]
            credential_env = CREDENTIAL_ENV.get(source_id)
            result.append(
                {
                    "id": source_id,
                    "name": SOURCE_NAMES[source_id],
                    "record_schema": contract.record_schema,
                    "freshness_policy": contract.freshness_policy,
                    "mapping_rules": contract.mapping_rules,
                    "credential_env": credential_env,
                    "credential_configured": bool(
                        credential_env and os.environ.get(credential_env, "").strip()
                    ),
                }
            )
        return result

    @staticmethod
    def validate_config(source_id: str, max_records: int, duration_seconds: int) -> None:
        """Reject unallowlisted sources and crawl limits outside service bounds."""
        if source_id != "all" and source_id not in REGIONAL_SOURCE_IDS:
            raise ValueError(f"unknown regional source {source_id!r}")
        if not 1 <= max_records <= 1000:
            raise ValueError("max_records must be in [1, 1000]")
        if not 1 <= duration_seconds <= 300:
            raise ValueError("duration_seconds must be in [1, 300]")

    def get_config(self) -> dict[str, Any]:
        """Return saved run settings or the service defaults."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = 'crawl_config'"
            ).fetchone()
        if row is None:
            return dict(DEFAULT_CONFIG)
        try:
            config = json.loads(row["value"])
        except json.JSONDecodeError:
            return dict(DEFAULT_CONFIG)
        if not isinstance(config, dict):
            return dict(DEFAULT_CONFIG)
        return {**DEFAULT_CONFIG, **config}

    def save_config(self, *, source_id: str, max_records: int, duration_seconds: int) -> dict:
        """Validate and persist settings for the next crawl run."""
        self.validate_config(source_id, max_records, duration_seconds)
        config = {
            "source_id": source_id,
            "max_records": max_records,
            "duration_seconds": duration_seconds,
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO settings(key, value) VALUES('crawl_config', ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (json.dumps(config, sort_keys=True),),
            )
        return config

    def start_run(self) -> dict[str, Any]:
        """Start one configured CLI run; a single active process bounds source load."""
        with self._lock:
            self._refresh_all()
            config = self.get_config()
            source_id = str(config["source_id"])
            max_records = int(config["max_records"])
            duration_seconds = int(config["duration_seconds"])
            self.validate_config(source_id, max_records, duration_seconds)
            credential_env = CREDENTIAL_ENV.get(source_id)
            if credential_env and not os.environ.get(credential_env, "").strip():
                raise ValueError(f"source {source_id} requires {credential_env}")
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                active = connection.execute(
                    "SELECT run_id FROM runs WHERE status IN (?, ?, ?) LIMIT 1",
                    ACTIVE_STATUSES,
                ).fetchone()
                if active:
                    raise RuntimeError(f"crawl run {active['run_id']} is already active")
                run_id = datetime.now(UTC).strftime("crawl-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
                log_path = self.log_root / f"{run_id}.log"
                created_at = _utc_now()
                connection.execute(
                    """INSERT INTO runs(
                        run_id, source_id, status, created_at, max_records,
                        duration_seconds, log_path
                    ) VALUES(?, ?, 'queued', ?, ?, ?, ?)""",
                    (run_id, source_id, created_at, max_records, duration_seconds, str(log_path)),
                )

            command = [
                sys.executable,
                str(REPO_ROOT / "tools" / "ingest_osint_region.py"),
                "--source",
                CLI_SOURCE_IDS.get(source_id, source_id),
                "--run-id",
                run_id,
                "--root",
                str(self.raw_root),
                "--event-log",
                str(production_event_log_path()),
                "--max-records",
                str(max_records),
                "--duration-seconds",
                str(duration_seconds),
            ]
            environment = dict(os.environ)
            for name in (
                "FOUNDRY_CONSOLE_TOKEN",
                "FOUNDRY_PROVENANCE_TOKENS",
                "FOUNDRY_DATABASE_URL",
                "DATABASE_URL",
                "PGPASSWORD",
            ):
                environment.pop(name, None)
            environment["PYTHONUNBUFFERED"] = "1"
            try:
                process = subprocess.Popen(
                    command,
                    cwd=REPO_ROOT,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                )
            except OSError as exc:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE runs SET status='failed', finished_at=?, error=? WHERE run_id=?",
                        (_utc_now(), redact_secrets(str(exc)), run_id),
                    )
                raise RuntimeError(f"could not start crawl process: {exc}") from exc

            with self._connect() as connection:
                connection.execute(
                    """UPDATE runs
                       SET status=CASE WHEN stop_requested=1 THEN 'stopping' ELSE 'running' END,
                           started_at=?, pid=? WHERE run_id=?""",
                    (_utc_now(), process.pid, run_id),
                )
            self._processes[run_id] = process
            reader = threading.Thread(
                target=self._copy_output,
                args=(process.stdout, log_path),
                daemon=True,
                name=f"crawl-log-{run_id}",
            )
            self._log_readers[run_id] = reader
            reader.start()
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT stop_requested FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
            if row is not None and row["stop_requested"]:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            return self.get_run(run_id)

    def _copy_output(self, stream: IO[str] | None, log_path: Path) -> None:
        if stream is None:
            return
        with stream, log_path.open("w", encoding="utf-8") as output:
            for line in stream:
                output.write(redact_secrets(line))
                output.flush()

    def _refresh_all(self) -> None:
        with self._connect() as connection:
            active_rows = connection.execute(
                "SELECT * FROM runs WHERE status IN (?, ?, ?)", ACTIVE_STATUSES
            ).fetchall()
        for row in active_rows:
            self._refresh_run(row["run_id"])

    def _refresh_run(self, run_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row["status"] not in ACTIVE_STATUSES:
            return

        process = self._processes.get(run_id)
        if process is not None:
            exit_code = process.poll()
            if exit_code is None:
                return
            reader = self._log_readers.pop(run_id, None)
            if reader is not None:
                reader.join(timeout=1)
        else:
            exit_code = self._recover_exit_code(row)
            if exit_code is None:
                return

        summary = self._read_summary(run_id)
        stop_requested = bool(row["stop_requested"])
        if stop_requested:
            status = "stopped"
        elif exit_code < 0 and summary is None:
            status = "interrupted"
        else:
            status = "succeeded" if exit_code == 0 else "failed"
        error = {
            "failed": f"process exited with code {exit_code}",
            "interrupted": "process ended before writing a completion summary",
        }.get(status)
        with self._connect() as connection:
            connection.execute(
                """UPDATE runs SET status=?, finished_at=?, exit_code=?, summary_json=?, error=?
                   WHERE run_id=?""",
                (
                    status,
                    _utc_now(),
                    exit_code,
                    json.dumps(summary, sort_keys=True) if summary is not None else None,
                    error,
                    run_id,
                ),
            )
        self._processes.pop(run_id, None)

    def _recover_exit_code(self, row: sqlite3.Row) -> int | None:
        """Reconcile a run after a console restart without trusting a stale PID."""
        pid = row["pid"]
        if pid is None:
            try:
                created_at = datetime.fromisoformat(row["created_at"])
            except (TypeError, ValueError):
                created_at = datetime.min.replace(tzinfo=UTC)
            if (datetime.now(UTC) - created_at).total_seconds() < 30:
                return None
        if isinstance(pid, int) and self._pid_matches_run(pid, row["run_id"]):
            return None
        summary = self._read_summary(row["run_id"])
        if summary is not None:
            return (
                0
                if not any(
                    item.get("status") == "error"
                    for item in summary.get("sources", {}).values()
                    if isinstance(item, dict)
                )
                else 1
            )
        return -1

    @staticmethod
    def _pid_matches_run(pid: int, run_id: str) -> bool:
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        command = result.stdout
        return result.returncode == 0 and run_id in command and "ingest_osint_region.py" in command

    def _read_summary(self, run_id: str) -> dict[str, Any] | None:
        """Read only safe, bounded summary fields from a crawler-produced report."""
        path = self.raw_root / run_id / "summary.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None

        sources = value.get("sources")
        safe_sources: dict[str, dict[str, Any]] = {}
        if isinstance(sources, dict):
            count_fields = (
                "mapped_records",
                "entities",
                "entities_linked",
                "identities_reused",
                "positions",
                "positions_deduplicated",
                "reviews",
                "skipped",
                "documents",
                "documents_deduplicated",
            )
            for source_id, result in sources.items():
                if not isinstance(source_id, str) or not isinstance(result, dict):
                    continue
                safe_result: dict[str, Any] = {}
                if result.get("status") in {"complete", "skipped", "error"}:
                    safe_result["status"] = result["status"]
                for field in count_fields:
                    count = result.get(field)
                    if isinstance(count, int) and count >= 0:
                        safe_result[field] = count
                snapshot = result.get("snapshot")
                if isinstance(snapshot, str) and re.fullmatch(r"[0-9a-f]{64}", snapshot):
                    safe_result["snapshot"] = snapshot
                safe_sources[source_id] = safe_result

        event_counts = value.get("event_counts")
        safe_counts = (
            {
                key: count
                for key, count in event_counts.items()
                if isinstance(key, str) and isinstance(count, int) and count >= 0
            }
            if isinstance(event_counts, dict)
            else {}
        )
        total_events = value.get("total_events")
        deduplicated_records = value.get("deduplicated_records")
        result = {
            "sources": safe_sources,
            "event_counts": safe_counts,
            "total_events": total_events
            if isinstance(total_events, int) and total_events >= 0
            else 0,
            "deduplicated_records": deduplicated_records
            if isinstance(deduplicated_records, int) and deduplicated_records >= 0
            else 0,
        }
        generated_at = value.get("generated_at")
        if isinstance(generated_at, str) and len(generated_at) <= 64:
            result["generated_at"] = generated_at
        return result

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            summary = json.loads(row["summary_json"]) if row["summary_json"] else None
        except json.JSONDecodeError:
            summary = None
        return {
            "run_id": row["run_id"],
            "source_id": row["source_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "exit_code": row["exit_code"],
            "max_records": row["max_records"],
            "duration_seconds": row["duration_seconds"],
            "summary": summary,
            "error": row["error"],
        }

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return one run after reconciling it with the current process state."""
        self._refresh_run(run_id)
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent runs, bounded to at most 100 records."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be in [1, 100]")
        self._refresh_all()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def state(self) -> dict[str, Any]:
        """Build the dashboard payload from source contracts and recent runs."""
        runs = self.list_runs()
        return {
            "config": self.get_config(),
            "sources": self.sources(),
            "runs": runs,
            "active_run": next((run for run in runs if run["status"] in ACTIVE_STATUSES), None),
            "storage": "external crawl root / osint-vietnam-scs",
        }

    def logs(self, run_id: str, max_bytes: int = 32_000) -> str:
        """Return a bounded, redacted tail of one run's captured output."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT log_path FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        path = Path(row["log_path"]).resolve()
        if path.parent != self.log_root.resolve():
            raise ValueError("invalid crawl log location")
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - max_bytes))
                content = stream.read(max_bytes).decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""
        return redact_secrets(content)

    def stop_run(self, run_id: str) -> dict[str, Any] | None:
        """Request a graceful stop for one active crawl process group."""
        with self._lock:
            run = self.get_run(run_id)
            if run is None or run["status"] not in {"queued", "running"}:
                return run
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT pid FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                connection.execute(
                    "UPDATE runs SET status='stopping', stop_requested=1 WHERE run_id=?",
                    (run_id,),
                )
            pid = row["pid"] if row is not None else None
            if isinstance(pid, int):
                owned = run_id in self._processes or self._pid_matches_run(pid, run_id)
                if not owned:
                    with self._connect() as connection:
                        connection.execute(
                            "UPDATE runs SET status='interrupted', finished_at=?, exit_code=-1 "
                            "WHERE run_id=?",
                            (_utc_now(), run_id),
                        )
                    return self.get_run(run_id)
                with suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGTERM)
            else:
                return self.get_run(run_id)
            return self.get_run(run_id)

    def shutdown(self) -> None:
        """Terminate owned process groups when the console exits cleanly."""
        with self._lock:
            for run_id, process in list(self._processes.items()):
                if process.poll() is None:
                    with self._connect() as connection:
                        connection.execute(
                            "UPDATE runs SET status='stopping', stop_requested=1 WHERE run_id=?",
                            (run_id,),
                        )
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
            for process in self._processes.values():
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        continue
            self._refresh_all()


_manager: CrawlRunManager | None = None
_manager_lock = threading.Lock()


def get_crawl_manager() -> CrawlRunManager:
    """Return a manager rooted at the current configured data directory."""
    global _manager
    root = crawl_path()
    service_root = root / "crawler-service"
    raw_root = root / "osint-vietnam-scs"
    with _manager_lock:
        if _manager is None or _manager.service_root != service_root.expanduser().resolve():
            if _manager is not None:
                _manager.shutdown()
            _manager = CrawlRunManager(service_root, raw_root)
        return _manager


def shutdown_crawl_manager() -> None:
    """Stop crawler child processes before the console server exits."""
    if _manager is not None:
        _manager.shutdown()
