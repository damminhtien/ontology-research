#!/usr/bin/env python3
"""Assemble a privacy-preserving OSINT quality and performance report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from psycopg import connect  # noqa: E402

from foundry.events import EventLog  # noqa: E402


def _json(path: Path) -> dict[str, Any]:
    """Read one JSON report object."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _sha256(path: Path) -> str:
    """Hash a file without loading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _production_metrics(event_log_path: Path, database_url: str) -> dict[str, Any]:
    """Read-only counts and lineage invariants from the current projections."""
    event_counts: dict[str, int] = {}
    last_sequence = 0
    for event in EventLog(event_log_path).iter_all():
        event_counts[event.event_type] = event_counts.get(event.event_type, 0) + 1
        last_sequence = max(last_sequence, event.sequence or 0)

    with connect(database_url) as connection:
        identity = connection.execute(
            """SELECT count(*), count(DISTINCT source_id), count(DISTINCT entity_id),
                      (SELECT count(*) FROM (
                          SELECT entity_id FROM identity_source_records
                          GROUP BY entity_id HAVING count(DISTINCT source_id) > 1
                      ) cross_source)
               FROM identity_source_records"""
        ).fetchone()
        assertions = connection.execute(
            """SELECT count(*),
                      count(*) FILTER (WHERE jsonb_array_length(source_ids) > 0),
                      count(*) FILTER (WHERE jsonb_array_length(evidence) > 0),
                      count(*) FILTER (WHERE EXISTS (
                          SELECT 1 FROM jsonb_array_elements(evidence) e
                          WHERE coalesce(e->>'locator', '') <> '')),
                      count(*) FILTER (WHERE EXISTS (
                          SELECT 1 FROM jsonb_array_elements(evidence) e
                          WHERE e ? 'span')),
                      count(*) FILTER (WHERE EXISTS (
                          SELECT 1 FROM jsonb_array_elements(evidence) e
                          WHERE nullif(e->'span'->>'exact_text', '') IS NOT NULL)),
                      count(*) FILTER (WHERE valid_until IS NOT NULL
                                       AND valid_until <= valid_from),
                      count(*) FILTER (WHERE recorded_at = '' OR recorded_at IS NULL)
               FROM read_assertions"""
        ).fetchone()
        conflicts = connection.execute(
            """SELECT count(*) FROM read_assertions a JOIN read_assertions b
                 ON a.subject_id = b.subject_id
                AND a.predicate_iri = b.predicate_iri
                AND a.assertion_id < b.assertion_id
                AND a.object <> b.object
                AND a.status = 'asserted' AND b.status = 'asserted'
                AND a.superseded_by IS NULL AND b.superseded_by IS NULL
                AND a.valid_from < coalesce(b.valid_until, 'infinity'::timestamptz)
                AND b.valid_from < coalesce(a.valid_until, 'infinity'::timestamptz)"""
        ).fetchone()[0]
        database_summary = connection.execute(
            """SELECT version(), pg_database_size(current_database()),
                      pg_indexes_size('read_entities'),
                      pg_indexes_size('identity_external_ids'),
                      pg_indexes_size('read_assertions')"""
        ).fetchone()

    return {
        "event_log": {
            "path": "$FOUNDRY_DATA_ROOT/data/production.jsonl",
            "sha256": _sha256(event_log_path),
            "last_sequence": last_sequence,
            "event_count": sum(event_counts.values()),
            "event_types": dict(sorted(event_counts.items())),
        },
        "identity": {
            "source_records": identity[0],
            "source_count": identity[1],
            "canonical_entities": identity[2],
            "cross_source_canonical_entities": identity[3],
        },
        "assertions": {
            "count": assertions[0],
            "with_source": assertions[1],
            "with_evidence": assertions[2],
            "with_locator": assertions[3],
            "with_span_object": assertions[4],
            "with_exact_text": assertions[5],
            "invalid_intervals": assertions[6],
            "missing_recorded_time": assertions[7],
            "overlapping_conflicting_pairs": conflicts,
            "correctness_human_annotated": False,
            "evidence_precision_measured": False,
            "temporal_truth_measured": False,
        },
        "postgresql": {
            "version": database_summary[0],
            "database_bytes": database_summary[1],
            "index_bytes": {
                "read_entities": database_summary[2],
                "identity_external_ids": database_summary[3],
                "read_assertions": database_summary[4],
            },
        },
    }


def assemble_report(
    *,
    event_log_path: Path,
    organization_event_log_path: Path,
    database_url: str,
    gold_report_path: Path,
    api_report_path: Path,
    adapter_report_path: Path,
    replay_report_path: Path,
    splink_report_path: Path,
) -> dict[str, Any]:
    """Combine domain labels, production invariants, and measured runtimes."""
    gold = _json(gold_report_path)
    api = _json(api_report_path)
    adapters = _json(adapter_report_path)
    replay = _json(replay_report_path)
    splink = _json(splink_report_path)
    controlled = splink["results"]["febrl4"]["controlled_name_only"]
    splink_methods = controlled["methods"]

    entity_resolution = {
        "osint_organization_gold_pilot": {
            "dataset_id": gold["dataset_id"],
            "source_sha256": gold["source"]["sha256"],
            "record_event_count": gold["source"]["event_count"],
            "positive_pairs": gold["pair_counts"]["same_entity"],
            "negative_pairs": gold["pair_counts"]["different_entity"],
            "human_adjudicated": gold["label_policy"]["human_adjudicated"],
            "exact_name_alias_candidate": gold["pairs"][0]["exact_name_or_alias_candidate"],
            "name_only_resolver": {
                "decision": gold["pairs"][0]["name_only_resolver_decision"],
                "candidate_count": gold["pairs"][0]["name_only_resolver_candidate_count"],
                "positive_in_top_5": gold["pairs"][0]["name_only_resolver_candidate_at_5"],
            },
            "trusted_identifier_crosswalk": {
                "matched_pairs": gold["pair_counts"]["same_entity"],
                "namespace": "gleif-lei",
                "review_candidates": 0,
            },
            "pairwise_precision": None,
            "pairwise_recall": None,
            "false_merge_rate": None,
            "review_workload_estimate": None,
            "status": "one-positive-pair pilot; insufficient for population metrics",
        },
        "controlled_febrl4_name_only": {
            "source_report_sha256": _sha256(splink_report_path),
            "seed": splink["protocol"]["seed"],
            "review_budget_per_source_record": controlled["review_budget_per_source_record"],
            "threshold_policy": controlled["threshold_policy"],
            "test_entities": splink["results"]["febrl4"]["split_entities"]["test"],
            "methods": {
                name: {
                    key: method["test"][key]
                    for key in (
                        "candidate_recall",
                        "recall_at_5",
                        "pairwise_precision",
                        "pairwise_recall",
                        "false_suggestions",
                        "suggestions",
                        "review_budget_per_source_record",
                    )
                }
                for name, method in splink_methods.items()
                if "test" in method
            },
            "scope": "method benchmark only; FEBRL4 is not OSINT domain evidence",
        },
    }

    current = _production_metrics(event_log_path, database_url)
    current["domain_pilot_source"] = {
        "path": "$FOUNDRY_DATA_ROOT/data/crawl-runs/osint-mvp/osint-mvp-20261118-live/events.jsonl",
        "sha256": _sha256(organization_event_log_path),
    }
    adapter_storage = adapters.get("storage", {})
    return {
        "report_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "inputs": {
            "production_event_log_sha256": current["event_log"]["sha256"],
            "organization_pilot_event_log_sha256": _sha256(organization_event_log_path),
            "domain_gold_report_sha256": _sha256(gold_report_path),
            "api_benchmark_report_sha256": _sha256(api_report_path),
            "adapter_benchmark_report_sha256": _sha256(adapter_report_path),
            "postgres_replay_report_sha256": _sha256(replay_report_path),
            "splink_method_report_sha256": _sha256(splink_report_path),
        },
        "datasets": current,
        "entity_resolution": entity_resolution,
        "evidence_and_temporal": current["assertions"],
        "system_performance": {
            "api_boundary": {
                "event_log_sha256": api["reproduction"]["event_log_sha256"],
                "sample_count_per_operation_and_concurrency": api["measurement"]["results"][
                    "entity_search"
                ][0]["request_count"],
                "concurrencies": [
                    run["concurrency"] for run in api["measurement"]["results"]["entity_search"]
                ],
                "environment": api["environment"],
                "server_rss_bytes": api["measurement"]["server_rss_bytes"],
                "results": api["measurement"]["results"],
            },
            "postgres_oxigraph_operational_queries": {
                "equivalent": adapters["equivalent"],
                "source_event_count": adapters["source"]["event_count"],
                "runs_per_probe": adapters["workload"]["runs"],
                "latency_comparable": adapters["latency_scope"]["comparable"],
                "latency_scope": adapters["latency_scope"],
                "latency_ms": adapters["latency_ms"],
                "oxigraph_snapshot": {
                    "build_seconds": adapter_storage["snapshot_build_seconds"],
                    "open_seconds": adapter_storage["snapshot_open_seconds"],
                    "bytes": adapter_storage["snapshot_bytes"],
                    "process_peak_rss_bytes": adapter_storage["process_peak_rss_bytes"],
                },
            },
            "postgres_rebuild_and_reopen": replay["results"],
        },
        "interpretation": [
            (
                "One identifier-linked organization pair shows a name-only REVIEW with two "
                "candidates versus a unique LEI crosswalk; this is one case, not a general "
                "accuracy estimate."
            ),
            (
                "Assertion source/evidence/locator counts and interval checks measure lineage "
                "integrity, not factual correctness or evidence precision."
            ),
            (
                "PostgreSQL and Oxigraph returned equivalent results on the matched real-data "
                "probes; engine-level latency scopes differ and are not ranked directly."
            ),
            (
                "The production dataset contains 3,918 events and 1,244 source records; these "
                "runs do not establish scale behavior at one million records."
            ),
            (
                "AIS vessel records and a human-adjudicated vessel/organization gold set were "
                "unavailable in the captured local data."
            ),
        ],
        "reproduction": {
            "gold_pilot": (
                ".venv/bin/python tools/build_domain_gold.py --event-log "
                "$FOUNDRY_DATA_ROOT/data/crawl-runs/osint-mvp/"
                "osint-mvp-20261118-live/events.jsonl --output "
                "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/"
                "osint-organization-crosswalk-pilot-v1.json"
            ),
            "api": (
                ".venv/bin/python tools/benchmark_api.py "
                "--base-url http://127.0.0.1:8787 "
                '--event-log "$FOUNDRY_DATA_ROOT/data/production.jsonl" '
                "--requests 100 --concurrency 1,8,32 "
                '--output "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/'
                'api-boundary-v1.json"'
            ),
            "query_adapters": (
                'make query-adapter-eval QUERY_ADAPTER_ARGS="--event-log '
                "${FOUNDRY_DATA_ROOT}/data/production.jsonl --snapshot-root "
                "${FOUNDRY_DATA_ROOT}/evaluations/osint-quality-v1/oxigraph "
                "--runs 100 --output "
                "${FOUNDRY_DATA_ROOT}/evaluations/osint-quality-v1/"
                'query-adapters-v1.json"'
            ),
            "postgres_replay": (
                "FOUNDRY_DATABASE_URL=<local PostgreSQL DSN> "
                ".venv/bin/python tools/benchmark_postgres_replay.py --event-log "
                '"$FOUNDRY_DATA_ROOT/data/production.jsonl" --output '
                '"$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/'
                'postgres-replay-v1.json"'
            ),
            "assemble": (
                ".venv/bin/python tools/assemble_osint_quality_report.py "
                '--event-log "$FOUNDRY_DATA_ROOT/data/production.jsonl" '
                '--organization-event-log "$FOUNDRY_DATA_ROOT/data/crawl-runs/'
                'osint-mvp/osint-mvp-20261118-live/events.jsonl" '
                '--gold-report "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/'
                'osint-organization-crosswalk-pilot-v1.json" '
                '--api-report "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/'
                'api-boundary-v1.json" --adapter-report '
                '"$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/'
                'query-adapters-v1.json" --replay-report '
                '"$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/'
                'postgres-replay-v1.json" --database-url '
                "${FOUNDRY_DATABASE_URL:-postgresql:///ontology_research} "
                '--output "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/'
                'quality-report-v1.json"'
            ),
        },
        "report_outputs": {
            "api": "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/api-boundary-v1.json",
            "gold_pilot": (
                "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/"
                "osint-organization-crosswalk-pilot-v1.json"
            ),
            "query_adapters": (
                "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/query-adapters-v1.json"
            ),
            "postgres_replay": (
                "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/postgres-replay-v1.json"
            ),
        },
    }


def main() -> int:
    """Assemble metrics without including source names or identifiers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--organization-event-log", type=Path, required=True)
    parser.add_argument("--database-url", default=os.environ.get("FOUNDRY_DATABASE_URL"))
    parser.add_argument("--gold-report", type=Path, required=True)
    parser.add_argument("--api-report", type=Path, required=True)
    parser.add_argument("--adapter-report", type=Path, required=True)
    parser.add_argument("--replay-report", type=Path, required=True)
    parser.add_argument(
        "--splink-report",
        type=Path,
        default=REPO_ROOT / "benchmarks/evaluations/reports/splink-v2.json",
    )
    parser.add_argument(
        "--output", type=Path, default=REPO_ROOT / "build/osint-quality-report-v1.json"
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or FOUNDRY_DATABASE_URL is required")
    paths = (
        args.event_log,
        args.organization_event_log,
        args.gold_report,
        args.api_report,
        args.adapter_report,
        args.replay_report,
        args.splink_report,
    )
    if any(not path.is_file() for path in paths):
        parser.error("all input event logs and JSON reports must exist")
    report = assemble_report(
        event_log_path=args.event_log,
        organization_event_log_path=args.organization_event_log,
        database_url=args.database_url,
        gold_report_path=args.gold_report,
        api_report_path=args.api_report,
        adapter_report_path=args.adapter_report,
        replay_report_path=args.replay_report,
        splink_report_path=args.splink_report,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "source_commit": report["source_commit"],
                "source_event_count": report["datasets"]["event_log"]["event_count"],
                "source_record_count": report["datasets"]["identity"]["source_records"],
                "gold_positive_pairs": report["entity_resolution"]["osint_organization_gold_pilot"][
                    "positive_pairs"
                ],
                "api_operations": len(report["system_performance"]["api_boundary"]["results"]),
                "query_adapters_equivalent": report["system_performance"][
                    "postgres_oxigraph_operational_queries"
                ]["equivalent"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
