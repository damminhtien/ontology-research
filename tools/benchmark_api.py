#!/usr/bin/env python3
"""Measure bounded OSINT API latency and server RSS on a local event log."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.events import EventLog  # noqa: E402
from foundry.paths import production_event_log_path  # noqa: E402


@dataclass(frozen=True)
class Probe:
    """One API operation with a stable, non-sensitive report label."""

    name: str
    method: str
    path: str
    body: dict[str, str] | None = None


def _sha256(path: Path) -> str:
    """Hash a file without loading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _physical_memory_bytes() -> int | None:
    """Read installed memory from psutil or the platform's native command."""
    try:
        import psutil

        return psutil.virtual_memory().total
    except ImportError:
        if platform.system() == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                check=False,
                text=True,
            )
            try:
                return int(result.stdout.strip())
            except ValueError:
                return None
        try:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (AttributeError, OSError, ValueError):
            return None


def _workload(event_log_path: Path) -> tuple[list[Probe], dict[str, int]]:
    """Select representative entity and assertion probes from the log."""
    entities: dict[str, dict[str, Any]] = {}
    assertions: list[dict[str, Any]] = []
    event_counts: Counter[str] = Counter()
    for event in EventLog(event_log_path).iter_all():
        event_counts[event.event_type] += 1
        payload = event.payload
        if event.event_type == "EntityCreated":
            entity_id = payload.get("entity_id")
            if entity_id and payload.get("name"):
                entities.setdefault(entity_id, payload)
        elif event.event_type == "AssertionMade":
            assertions.append(payload)

    if not entities:
        raise ValueError(f"event log contains no named entities: {event_log_path}")
    entity_ids = list(entities)
    entity_id = entity_ids[0]
    entity = entities[entity_id]
    assertion = next(
        (
            candidate
            for candidate in assertions
            if candidate.get("subject_id") == entity_id and candidate.get("assertion_id")
        ),
        None,
    )
    if assertion is None:
        raise ValueError(f"event log has no assertion for entity {entity_id}")
    if len(entity_ids) < 2:
        raise ValueError("identity candidate benchmark needs two named entities")

    entity_path = urllib.parse.quote(entity_id, safe="")
    assertion_path = urllib.parse.quote(assertion["assertion_id"], safe="")
    name_query = urllib.parse.urlencode({"q": entity["name"], "limit": 10})
    instant = urllib.parse.quote(str(assertion.get("valid_from", "")), safe=":-TZ.")
    candidate = {"entity_id": entity_id, "candidate_entity_id": entity_ids[1]}
    probes = [
        Probe("entity_search", "GET", f"/api/v1/entities?{name_query}"),
        Probe("entity_profile", "GET", f"/api/v1/entities/{entity_path}"),
        Probe("identifiers", "GET", f"/api/v1/entities/{entity_path}/identifiers"),
        Probe(
            "relations_as_of",
            "GET",
            f"/api/v1/entities/{entity_path}/relations?depth=1&limit=25&at={instant}",
        ),
        Probe(
            "timeline",
            "GET",
            f"/api/v1/entities/{entity_path}/timeline?limit=25",
        ),
        Probe(
            "assertion_evidence",
            "GET",
            f"/api/v1/assertions/{assertion_path}/evidence",
        ),
        Probe(
            "conflicts_as_of",
            "GET",
            f"/api/v1/entities/{entity_path}/conflicts?limit=25&at={instant}",
        ),
        Probe("identity_candidate_review", "POST", "/api/v1/identity/candidates", candidate),
    ]
    return probes, dict(event_counts)


def _request(base_url: str, probe: Probe, timeout_seconds: float) -> tuple[float, int, int]:
    """Send one request and return end-to-end latency, status, and byte count."""
    data = json.dumps(probe.body).encode("utf-8") if probe.body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base_url.rstrip("/") + probe.path,
        data=data,
        headers=headers,
        method=probe.method,
    )
    started = time.perf_counter_ns()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read()
            return (time.perf_counter_ns() - started) / 1_000_000, response.status, len(payload)
    except urllib.error.HTTPError as error:
        payload = error.read()
        return (time.perf_counter_ns() - started) / 1_000_000, error.code, len(payload)


def _percentile(values: list[float], fraction: float) -> float:
    """Return the nearest-rank percentile of a non-empty sample."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _server_pid(port: int) -> int | None:
    """Find the local process listening on the requested HTTP port."""
    result = subprocess.run(
        ["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        check=False,
        text=True,
    )
    for line in result.stdout.splitlines():
        if line.strip().isdigit():
            return int(line.strip())
    return None


class RssSampler:
    """Sample local API process RSS during the benchmark."""

    def __init__(self, pid: int | None, interval_seconds: float = 0.05) -> None:
        """Create the sampler for one process, if it can be identified."""
        self.pid = pid
        self.interval_seconds = interval_seconds
        self.initial_bytes: int | None = None
        self.peak_bytes: int | None = None
        self.final_bytes: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _read(self) -> int | None:
        if self.pid is None:
            return None
        try:
            import psutil

            return psutil.Process(self.pid).memory_info().rss
        except ImportError:
            result = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(self.pid)],
                capture_output=True,
                check=False,
                text=True,
            )
            try:
                return int(result.stdout.strip()) * 1024
            except ValueError:
                return None
        except Exception:
            return None

    def _sample(self) -> None:
        value = self._read()
        if value is None:
            return
        self.peak_bytes = max(self.peak_bytes or 0, value)
        if self.initial_bytes is None:
            self.initial_bytes = value
        self.final_bytes = value

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def __enter__(self) -> RssSampler:
        """Start process sampling."""
        self._sample()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop sampling and record the final RSS value."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._sample()


def _measure_probe(
    base_url: str,
    probe: Probe,
    *,
    requests: int,
    concurrency: int,
    timeout_seconds: float,
) -> dict[str, int | float]:
    """Measure one operation at one concurrency level."""
    for _ in range(min(5, requests)):
        _request(base_url, probe, timeout_seconds)
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(_request, base_url, probe, timeout_seconds) for _ in range(requests)
        ]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]
    elapsed_seconds = time.perf_counter() - started
    latencies = [result[0] for result in results]
    statuses = Counter(str(result[1]) for result in results)
    if statuses != {"200": requests}:
        raise RuntimeError(f"{probe.name} returned unexpected HTTP statuses: {dict(statuses)}")
    return {
        "request_count": requests,
        "concurrency": concurrency,
        "p50_ms": round(_percentile(latencies, 0.50), 4),
        "p95_ms": round(_percentile(latencies, 0.95), 4),
        "p99_ms": round(_percentile(latencies, 0.99), 4),
        "mean_ms": round(sum(latencies) / len(latencies), 4),
        "throughput_requests_per_second": round(requests / elapsed_seconds, 2),
        "response_bytes_mean": round(sum(result[2] for result in results) / requests, 1),
    }


def benchmark(
    base_url: str,
    event_log_path: Path,
    *,
    requests: int,
    concurrencies: tuple[int, ...],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Benchmark representative bounded API resources on a local event log."""
    if requests < 20:
        raise ValueError("requests must be at least 20 for useful tail percentiles")
    if not concurrencies or any(value < 1 for value in concurrencies):
        raise ValueError("concurrency levels must be positive")
    if not event_log_path.is_file():
        raise FileNotFoundError(event_log_path)

    probes, event_counts = _workload(event_log_path)
    port = urllib.parse.urlparse(base_url).port or 80
    pid = _server_pid(port)
    results: dict[str, list[dict[str, int | float]]] = {}
    with RssSampler(pid) as sampler:
        for probe in probes:
            results[probe.name] = [
                _measure_probe(
                    base_url,
                    probe,
                    requests=requests,
                    concurrency=concurrency,
                    timeout_seconds=timeout_seconds,
                )
                for concurrency in concurrencies
            ]

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "report_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source_commit": commit,
        "reproduction": {
            "event_log": "$FOUNDRY_DATA_ROOT/data/production.jsonl",
            "event_log_sha256": _sha256(event_log_path),
            "benchmark_source_sha256": _sha256(Path(__file__)),
            "command": (
                ".venv/bin/python tools/benchmark_api.py "
                "--base-url http://127.0.0.1:8787 "
                "--event-log $FOUNDRY_DATA_ROOT/data/production.jsonl "
                f"--requests {requests} --concurrency {','.join(map(str, concurrencies))}"
            ),
        },
        "dataset": {
            "event_counts": event_counts,
            "event_count": sum(event_counts.values()),
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "logical_cpus": os.cpu_count(),
            "physical_memory_bytes": _physical_memory_bytes(),
        },
        "measurement": {
            "scope": (
                "loopback HTTP request through response-body read; includes JSON serialization"
            ),
            "warmup_requests_per_operation": min(5, requests),
            "percentile_method": "nearest rank",
            "request_timeout_seconds": timeout_seconds,
            "server_pid_found": pid is not None,
            "server_rss_bytes": {
                "initial": sampler.initial_bytes,
                "peak": sampler.peak_bytes,
                "final": sampler.final_bytes,
            },
            "results": results,
        },
    }


def main() -> int:
    """Run the local API benchmark and write a JSON report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--event-log", type=Path, default=production_event_log_path())
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", default="1,8,32")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "build/api-benchmark.json")
    args = parser.parse_args()
    try:
        concurrencies = tuple(dict.fromkeys(int(value) for value in args.concurrency.split(",")))
        report = benchmark(
            args.base_url,
            args.event_log,
            requests=args.requests,
            concurrencies=concurrencies,
            timeout_seconds=args.timeout_seconds,
        )
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
