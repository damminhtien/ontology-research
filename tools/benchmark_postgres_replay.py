#!/usr/bin/env python3
"""Measure PostgreSQL projection rebuild, checkpoint reopen, and index rebuild."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from psycopg import connect, sql  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402

from foundry.events import EventLog  # noqa: E402


def _sha256(path: Path) -> str:
    """Hash a file without loading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_projector(event_log: Path, database_url: str, *, verify: bool) -> tuple[float, str]:
    """Run projection or checkpoint verification in a separate process."""
    command = [
        sys.executable,
        str(REPO_ROOT / "tools" / "project_events.py"),
        "--event-log",
        str(event_log),
        "--verify" if verify else "--rebuild",
    ]
    environment = os.environ.copy()
    environment["FOUNDRY_DATABASE_URL"] = database_url
    started = time.perf_counter()
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    elapsed = time.perf_counter() - started
    if result.returncode:
        raise RuntimeError(
            f"projector command failed with {result.returncode}: "
            f"{result.stdout.strip()} {result.stderr.strip()}"
        )
    return elapsed, result.stdout.strip()


def benchmark(event_log: Path, admin_database_url: str) -> dict[str, Any]:
    """Build and drop an isolated database populated from one immutable log."""
    if not event_log.is_file():
        raise FileNotFoundError(event_log)

    database_name = f"ontology_replay_eval_{uuid.uuid4().hex[:16]}"
    admin_url = make_conninfo(admin_database_url, dbname="postgres")
    database_url = make_conninfo(admin_database_url, dbname=database_name)
    with connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

    try:
        rebuild_seconds, rebuild_output = _run_projector(event_log, database_url, verify=False)
        recovery_seconds, recovery_output = _run_projector(event_log, database_url, verify=True)
        with connect(database_url, autocommit=True) as connection:
            database_version = connection.execute("SELECT version()").fetchone()[0]
            index_rebuild = []
            for table in ("read_entities", "identity_external_ids", "read_assertions"):
                started = time.perf_counter()
                connection.execute(sql.SQL("REINDEX TABLE {}").format(sql.Identifier(table)))
                index_rebuild.append(
                    {"table": table, "seconds": round(time.perf_counter() - started, 4)}
                )
            sizes = connection.execute(
                """SELECT pg_database_size(current_database()),
                          pg_indexes_size('read_entities'),
                          pg_indexes_size('identity_external_ids'),
                          pg_indexes_size('read_assertions')"""
            ).fetchone()
        verification = ast.literal_eval(recovery_output)
        if not isinstance(verification, dict) or verification.get("consistent") is not True:
            raise RuntimeError(f"checkpoint verification failed: {recovery_output}")
        event_count = sum(1 for _ in EventLog(event_log).iter_all())
        peak_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        if platform.system() != "Darwin":
            peak_rss *= 1024
        report = {
            "report_version": 1,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "source": {
                "event_log": "$FOUNDRY_DATA_ROOT/data/production.jsonl",
                "sha256": _sha256(event_log),
                "event_count": event_count,
                "last_sequence": EventLog(event_log).last_sequence,
            },
            "reproduction": {
                "command": (
                    "FOUNDRY_DATABASE_URL=<local PostgreSQL DSN> "
                    ".venv/bin/python tools/benchmark_postgres_replay.py "
                    "--event-log $FOUNDRY_DATA_ROOT/data/production.jsonl"
                ),
                "rebuild_output": rebuild_output,
                "reopen_verify_output": recovery_output,
                "temporary_database_dropped": True,
            },
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "logical_cpus": os.cpu_count(),
                "postgresql": database_version,
            },
            "results": {
                "full_projection_rebuild_seconds": round(rebuild_seconds, 4),
                "new_process_checkpoint_verify_seconds": round(recovery_seconds, 4),
                "index_rebuild": index_rebuild,
                "temporary_database_bytes": sizes[0],
                "index_bytes": {
                    "read_entities": sizes[1],
                    "identity_external_ids": sizes[2],
                    "read_assertions": sizes[3],
                },
                "child_process_peak_rss_bytes": peak_rss,
            },
        }
    finally:
        with connect(admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )
    return report


def main() -> int:
    """Run the isolated PostgreSQL replay benchmark."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("FOUNDRY_DATABASE_URL"),
        help="local PostgreSQL DSN used only to create and drop a temporary database",
    )
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "build/postgres-replay.json")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or FOUNDRY_DATABASE_URL is required")
    try:
        report = benchmark(args.event_log, args.database_url)
    except (FileNotFoundError, RuntimeError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
