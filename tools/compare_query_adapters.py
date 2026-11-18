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
from foundry.namespaces import ASSERTION_MIDDLE_NS, CORE_ONTOLOGY_NS  # noqa: E402
from foundry.oxigraph_snapshot import (  # noqa: E402
    build_snapshot,
    directory_size_bytes,
    open_current_store,
)
from foundry.postgres import PostgresDatabase  # noqa: E402
from foundry.postgres_readmodel import PostgresReadModelStore  # noqa: E402
from foundry.query_service import QueryService  # noqa: E402
from foundry.readmodel import format_instant, parse_instant  # noqa: E402


def _select(store, query: str) -> tuple[list[str], list[list[str | None]]]:
    result = store.query(query)
    variables = [variable.value for variable in result.variables]
    rows = [
        [
            solution[variable].value if solution[variable] is not None else None
            for variable in result.variables
        ]
        for solution in result
    ]
    return variables, rows


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
            and event.payload.get("object", {}).get("kind") in {"entity", "location"}
        ),
        None,
    )
    if assertion is None:
        raise RuntimeError("event log needs an entity-valued or location-valued assertion probe")
    return entity_id, assertion


def _sparql_iri(value: str) -> str:
    from rdflib import URIRef

    return URIRef(value).n3()


def _asof_query(assertion: dict) -> str:
    from rdflib import Literal, URIRef
    from rdflib.namespace import XSD

    subject = _sparql_iri(assertion["subject_id"])
    predicate = _sparql_iri(assertion["predicate_iri"])
    at = parse_instant(assertion["valid_from"])
    instant = Literal(format_instant(at), datatype=XSD.dateTime).n3()
    object_property = (
        "hasObject" if assertion["object"]["kind"] in {"entity", "location"} else "literalValue"
    )
    status = URIRef(f"{ASSERTION_MIDDLE_NS}status").n3()
    assertion_type = URIRef(f"{ASSERTION_MIDDLE_NS}predicate").n3()
    describes = URIRef(f"{CORE_ONTOLOGY_NS}describes").n3()
    valid_from = URIRef(f"{CORE_ONTOLOGY_NS}validFrom").n3()
    valid_until = URIRef(f"{CORE_ONTOLOGY_NS}validUntil").n3()
    object_iri = URIRef(f"{ASSERTION_MIDDLE_NS}{object_property}").n3()
    supersedes = URIRef(f"{ASSERTION_MIDDLE_NS}supersedes").n3()
    retracts = URIRef(f"{ASSERTION_MIDDLE_NS}retracts").n3()
    return f"""PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT ?assertion ?object WHERE {{
          ?assertion {describes} {subject} ; {assertion_type} {predicate} ;
                     {status} \"asserted\" ; {valid_from} ?from ; {object_iri} ?object .
          FILTER (?from <= {instant})
          OPTIONAL {{ ?assertion {valid_until} ?until . }}
          FILTER (!BOUND(?until) || {instant} < ?until)
          FILTER NOT EXISTS {{ ?replacement {supersedes} ?assertion . }}
          FILTER NOT EXISTS {{ ?retraction {retracts} ?assertion . }}
        }} ORDER BY ?assertion ?object"""


def _provenance_queries(assertion_id: str) -> tuple[str, str]:
    from rdflib import URIRef

    assertion = _sparql_iri(assertion_id)
    has_source = URIRef(f"{CORE_ONTOLOGY_NS}hasSource").n3()
    evidence = URIRef(f"{ASSERTION_MIDDLE_NS}evidence").n3()
    evidence_source = URIRef(f"{ASSERTION_MIDDLE_NS}evidenceSource").n3()
    locator = URIRef(f"{ASSERTION_MIDDLE_NS}locator").n3()
    span_start = URIRef(f"{ASSERTION_MIDDLE_NS}spanStart").n3()
    span_end = URIRef(f"{ASSERTION_MIDDLE_NS}spanEnd").n3()
    span_unit = URIRef(f"{ASSERTION_MIDDLE_NS}spanUnit").n3()
    exact_text = URIRef(f"{ASSERTION_MIDDLE_NS}exactText").n3()
    source_query = f"SELECT ?source WHERE {{ {assertion} {has_source} ?source }} ORDER BY ?source"
    evidence_query = f"""SELECT ?source ?locator ?start ?end ?unit ?text WHERE {{
      {assertion} {evidence} ?evidence_node .
      ?evidence_node {evidence_source} ?source .
      OPTIONAL {{ ?evidence_node {locator} ?locator . }}
      OPTIONAL {{ ?evidence_node {span_start} ?start . }}
      OPTIONAL {{ ?evidence_node {span_end} ?end . }}
      OPTIONAL {{ ?evidence_node {span_unit} ?unit . }}
      OPTIONAL {{ ?evidence_node {exact_text} ?text . }}
    }} ORDER BY ?source ?locator ?start ?end"""
    return source_query, evidence_query


def _expected_evidence(assertion: dict) -> list[tuple]:
    rows = []
    for item in assertion.get("evidence", []):
        span = item.get("span") or {}
        rows.append(
            (
                item.get("source_id"),
                item.get("locator"),
                str(span["start"]) if "start" in span else None,
                str(span["end"]) if "end" in span else None,
                span.get("unit"),
                span.get("exact_text"),
            )
        )
    return sorted(rows, key=repr)


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
    name_query = (
        f"SELECT ?name WHERE {{ {_sparql_iri(entity_id)} "
        f"{_sparql_iri(CORE_ONTOLOGY_NS + 'name')} ?name }} ORDER BY ?name"
    )
    pg_entity, pg_entity_latency = _measure(
        lambda: query_service.entity_lookup(entity_id, timeout_ms=timeout_ms), runs
    )
    ox_entity, ox_entity_latency = _measure(lambda: _select(store, name_query), runs)
    names = [row[0] for row in ox_entity[1]]
    if pg_entity is None or names != [pg_entity.name]:
        raise RuntimeError(f"entity lookup differs: PostgreSQL={pg_entity}, Oxigraph={names}")

    assertion_id = event_assertion["assertion_id"]
    pg_assertion, pg_provenance_latency = _measure(
        lambda: query_service.assertion_provenance(assertion_id, timeout_ms=timeout_ms), runs
    )
    if pg_assertion is None:
        raise RuntimeError(f"PostgreSQL is missing assertion {assertion_id}")
    if pg_assertion["status"] != "asserted":
        raise RuntimeError(
            f"selected assertion {assertion_id} is not active; choose an event log "
            "with an active probe"
        )
    source_query, evidence_query = _provenance_queries(assertion_id)
    ox_sources, ox_sources_latency = _measure(lambda: _select(store, source_query), runs)
    ox_evidence, ox_evidence_latency = _measure(lambda: _select(store, evidence_query), runs)
    postgres_sources = sorted(pg_assertion["source_ids"])
    oxigraph_sources = sorted(row[0] for row in ox_sources[1])
    expected_evidence = _expected_evidence(pg_assertion)
    actual_evidence = sorted([tuple(row) for row in ox_evidence[1]], key=repr)
    if postgres_sources != oxigraph_sources or expected_evidence != actual_evidence:
        raise RuntimeError(
            "assertion provenance differs: "
            f"PostgreSQL sources/evidence={postgres_sources}/{expected_evidence}, "
            f"Oxigraph={oxigraph_sources}/{actual_evidence}"
        )

    instant = parse_instant(event_assertion["valid_from"])
    object_kind = event_assertion["object"]["kind"]
    if object_kind == "entity":
        pg_temporal, pg_temporal_latency = _measure(
            lambda: query_service.relationships_as_of(
                event_assertion["subject_id"],
                instant,
                predicate_iri=event_assertion["predicate_iri"],
                limit=100,
                timeout_ms=timeout_ms,
            ),
            runs,
        )
        temporal_query = _asof_query(event_assertion)
        ox_temporal, ox_temporal_latency = _measure(lambda: _select(store, temporal_query), runs)
        pg_values = sorted(
            (row["assertion_id"], row["object"]["value"]) for row in pg_temporal.items
        )
        ox_values = sorted((row[0], row[1]) for row in ox_temporal[1])
    elif object_kind == "location":
        pg_temporal, pg_temporal_latency = _measure(
            lambda: query_service.location_resolution(
                event_assertion["subject_id"], instant, timeout_ms=timeout_ms
            ),
            runs,
        )
        temporal_query = _asof_query(event_assertion)
        ox_temporal, ox_temporal_latency = _measure(lambda: _select(store, temporal_query), runs)
        pg_values = sorted(
            (assertion_id, fact.location_uri)
            for fact in (
                ([pg_temporal.fact] if pg_temporal.fact else []) + list(pg_temporal.conflicts)
            )
            for assertion_id in fact.assertion_ids
        )
        ox_values = sorted((row[0], row[1]) for row in ox_temporal[1])
    else:
        raise RuntimeError(f"unsupported temporal comparison object kind: {object_kind}")
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
            "postgresql": "QueryService plus psycopg adapter and row decoding",
            "oxigraph": "direct Store.query plus result materialization",
            "comparable": False,
        },
        "results": {
            "entity_name": pg_entity.name,
            "source_ids": postgres_sources,
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
                "provenance_sources": ox_sources_latency,
                "provenance_evidence": ox_evidence_latency,
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
