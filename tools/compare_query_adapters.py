"""Compare matched PostgreSQL and Oxigraph operational query probes."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import resource
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "tools", REPO_ROOT):
    if _path.as_posix() not in sys.path:
        sys.path.insert(0, _path.as_posix())

from foundry.events import EventLog  # noqa: E402
from foundry.oxigraph_snapshot import (  # noqa: E402
    build_snapshot,
    directory_size_bytes,
    open_current_store,
)
from foundry.postgres import PostgresDatabase  # noqa: E402
from foundry.postgres_readmodel import PostgresReadModelStore  # noqa: E402
from foundry.query_benchmark import oxigraph_boundary, postgres_boundary  # noqa: E402
from foundry.query_service import QueryService  # noqa: E402
from foundry.readmodel import format_instant, parse_instant  # noqa: E402


def _measure(operation, runs: int) -> tuple[object, dict[str, float]]:
    expected = operation()
    timings = []
    for _ in range(runs):
        started = time.perf_counter_ns()
        actual = operation()
        timings.append((time.perf_counter_ns() - started) / 1_000_000)
        if actual != expected:
            raise RuntimeError("adapter query changed its result during the measurement")
    ordered = sorted(timings)

    def percentile(fraction: float) -> float:
        index = max(0, min(len(ordered) - 1, round(fraction * len(ordered) + 0.5) - 1))
        return round(ordered[index], 4)

    return expected, {
        "runs": runs,
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "mean_ms": round(statistics.fmean(timings), 4),
    }


def _first_probe(event_log_path: Path) -> tuple[str, dict]:
    log = EventLog(event_log_path)
    entity_id = next(
        (
            event.payload.get("entity_id")
            for event in log.iter_all()
            if event.event_type == "EntityCreated" and event.payload.get("entity_id")
        ),
        None,
    )
    if not entity_id:
        raise RuntimeError("event log needs at least one EntityCreated query probe")
    assertion = next(
        (
            event.payload
            for event in log.iter_all()
            if event.event_type == "AssertionMade"
            and event.payload.get("subject_id") == entity_id
            and event.payload.get("object", {}).get("kind") == "entity"
        ),
        None,
    )
    if assertion is None:
        raise RuntimeError("event log needs an entity-valued assertion probe")
    return entity_id, assertion


def compare(
    event_log_path: Path,
    snapshot_root: Path,
    database_url: str,
    *,
    runs: int = 25,
    timeout_ms: int = 5_000,
) -> dict:
    """Build the local Oxigraph snapshot and compare three matched read probes."""
    if runs < 1:
        raise ValueError("runs must be positive")
    entity_id, event_assertion = _first_probe(event_log_path)
    database = PostgresDatabase(database_url)
    model = PostgresReadModelStore(database)
    query_service = QueryService(model)
    last_sequence = 0
    for event in EventLog(event_log_path).iter_all():
        last_sequence = event.sequence or last_sequence
    if model.checkpoint_sequence != last_sequence:
        raise RuntimeError(
            "PostgreSQL projection checkpoint does not match the event log: "
            f"{model.checkpoint_sequence} != {last_sequence}; project the log first"
        )
    build_started = time.perf_counter()
    snapshot_path = build_snapshot(event_log_path, snapshot_root)
    snapshot_build_seconds = time.perf_counter() - build_started
    snapshot_manifest = json.loads((snapshot_path / "manifest.json").read_text(encoding="utf-8"))
    with database.connection() as connection:
        database_version = connection.execute("SELECT version()").fetchone()[0]

    try:
        from pyoxigraph import __version__ as oxigraph_version
    except ImportError as exc:
        raise RuntimeError("install requirements/evaluation-oxigraph.txt first") from exc
    open_started = time.perf_counter()
    store = open_current_store(snapshot_root)
    snapshot_open_seconds = time.perf_counter() - open_started
    postgres_queries = postgres_boundary(query_service)
    oxigraph_queries = oxigraph_boundary(store)
    pg_entity, pg_entity_latency = _measure(
        lambda: postgres_queries.entity_lookup(entity_id, timeout_ms=timeout_ms), runs
    )
    ox_entity, ox_entity_latency = _measure(
        lambda: oxigraph_queries.entity_lookup(entity_id, timeout_ms=timeout_ms), runs
    )
    if pg_entity is None or pg_entity != ox_entity:
        raise RuntimeError(f"entity lookup differs: PostgreSQL={pg_entity}, Oxigraph={ox_entity}")

    assertion_id = event_assertion["assertion_id"]
    pg_assertion, pg_provenance_latency = _measure(
        lambda: postgres_queries.assertion_provenance(assertion_id, timeout_ms=timeout_ms), runs
    )
    if pg_assertion is None:
        raise RuntimeError(f"PostgreSQL is missing assertion {assertion_id}")
    if pg_assertion.status != "asserted":
        raise RuntimeError(
            f"selected assertion {assertion_id} is not active; choose an event log "
            "with an active probe"
        )
    ox_assertion, ox_provenance_latency = _measure(
        lambda: oxigraph_queries.assertion_provenance(assertion_id, timeout_ms=timeout_ms), runs
    )
    if pg_assertion != ox_assertion:
        raise RuntimeError(
            f"assertion provenance differs: PostgreSQL={pg_assertion}, Oxigraph={ox_assertion}"
        )

    instant = parse_instant(event_assertion["valid_from"])
    query_args = (
        event_assertion["subject_id"],
        instant,
    )
    query_options = {
        "predicate_iri": event_assertion["predicate_iri"],
        "limit": 100,
        "timeout_ms": timeout_ms,
    }
    pg_values, pg_temporal_latency = _measure(
        lambda: postgres_queries.relationships_as_of(*query_args, **query_options), runs
    )
    ox_values, ox_temporal_latency = _measure(
        lambda: oxigraph_queries.relationships_as_of(*query_args, **query_options), runs
    )
    if pg_values != ox_values:
        raise RuntimeError(f"as-of query differs: PostgreSQL={pg_values}, Oxigraph={ox_values}")

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "workload": {
            "entity_lookup": entity_id,
            "as_of_subject": event_assertion["subject_id"],
            "as_of_predicate": event_assertion["predicate_iri"],
            "as_of": format_instant(instant),
            "provenance_assertion": assertion_id,
            "boundary": "OperationalQueryBoundary",
            "operations": ["entity_lookup", "relationships_as_of", "assertion_provenance"],
            "runs": runs,
            "statement_timeout_ms": timeout_ms,
        },
        "source": {
            "event_log": str(event_log_path),
            "sequence": last_sequence,
            "event_count": snapshot_manifest["source"]["event_count"],
            "sha256": snapshot_manifest["source"]["sha256"],
            "snapshot": str(snapshot_path),
            "profile": snapshot_manifest["profile"],
            "ontology_modules": snapshot_manifest["ontology_modules"],
        },
        "storage": {
            "snapshot_build_seconds": round(snapshot_build_seconds, 4),
            "snapshot_open_seconds": round(snapshot_open_seconds, 4),
            "snapshot_bytes": directory_size_bytes(snapshot_path),
            "process_peak_rss_bytes": (
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                if sys.platform == "darwin"
                else resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
            ),
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "pyoxigraph": oxigraph_version,
            "psycopg": importlib.metadata.version("psycopg"),
            "postgresql": database_version,
        },
        "equivalent": True,
        "latency_scope": {
            "postgresql": (
                "OperationalQueryBoundary -> QueryService -> psycopg -> normalized result"
            ),
            "oxigraph": "OperationalQueryBoundary -> pyoxigraph -> normalized result",
            "comparable": True,
            "http_included": False,
            "timeout_enforcement": {
                "postgresql": "server-side statement timeout",
                "oxigraph": "wall-clock checked after in-process query; query is not interruptible",
            },
        },
        "results": {
            "entity": {
                "entity_id": pg_entity.entity_id,
                "entity_type": pg_entity.entity_type,
                "name": pg_entity.name,
            },
            "source_ids": list(pg_assertion.source_ids),
            "as_of_assertions": len(pg_values),
        },
        "latency_ms": {
            "postgresql": {
                "point_lookup": pg_entity_latency,
                "as_of": pg_temporal_latency,
                "provenance": pg_provenance_latency,
            },
            "oxigraph": {
                "point_lookup": ox_entity_latency,
                "as_of": ox_temporal_latency,
                "provenance": ox_provenance_latency,
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument(
        "--database-url", default=os.environ.get("FOUNDRY_DATABASE_URL"), required=False
    )
    parser.add_argument("--runs", type=int, default=25)
    parser.add_argument("--timeout-ms", type=int, default=5_000)
    parser.add_argument(
        "--output", type=Path, default=REPO_ROOT / "build/query-adapter-report.json"
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or FOUNDRY_DATABASE_URL is required")
    report = compare(
        args.event_log,
        args.snapshot_root,
        args.database_url,
        runs=args.runs,
        timeout_ms=args.timeout_ms,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
