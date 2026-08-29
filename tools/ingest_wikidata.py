"""Ingest real Wikidata entities through the canonical pipeline into the lake.

Fetches organizations (P31 hierarchy) from the public Wikidata SPARQL
endpoint, pushes each record through identity resolution + the SHACL gate,
appends accepted events to the event log, and writes them to the columnar
lake (Parquet + zstd) for OLAP querying.

Usage:
    .venv/bin/python tools/ingest_wikidata.py [--limit 500] [--class Q43229]
                                              [--log data/events.jsonl]
                                              [--lake /data/lake] [--no-lake]

Rates to watch (roadmap KPIs):
    unresolved_rate  — fraction sent to review (precision-first, ADR-0003)
    shape violations — records the SHACL gate rejected, never silently dropped
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry import wikidata as wd
from foundry.events import EventLog
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline
from foundry.lake import default_lake_root, persist_events


def main() -> int:
    """Run one live Wikidata ingestion pass."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--class", dest="class_qid", default="Q43229", help="root Wikidata class")
    parser.add_argument("--limit", type=int, default=500, help="max SPARQL rows")
    parser.add_argument("--log", default="data/events.jsonl", help="event log path")
    parser.add_argument(
        "--lake", default=None, help="lake root (default $FOUNDRY_LAKE_ROOT or repo)"
    )
    parser.add_argument("--no-lake", action="store_true", help="skip the lake write")
    args = parser.parse_args()

    print(f"fetching P31/{args.class_qid} entities from Wikidata (limit={args.limit}) ...")
    records = wd.fetch_entities(class_qid=args.class_qid, limit=args.limit)
    mapped = sum(1 for r in records if r.entity_type is not None)
    print(f"normalized: {len(records)} records ({mapped} type-mapped)")

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline = IngestionPipeline(
        identity=IdentityService(),
        log=EventLog(log_path),
        ontology_path=REPO_ROOT / "ontology" / "core" / "core.ttl",
        shapes_path=REPO_ROOT / "shapes" / "core_shapes.ttl",
    )
    stats, _receipts, events = wd.ingest_records(pipeline, records)

    d = stats.as_dict()
    print(
        f"ingested: {d['accepted']}/{d['total']} accepted "
        f"(new {d['new_entities']}, merged {d['merged']}), "
        f"{d['rejected']} to review, {d['skipped_no_type']} no-type"
    )
    print(
        f"unresolved_rate: {stats.unresolved_rate:.1%} (KPI target < 1% once review queue drains)"
    )
    for reason, count in sorted(d["rejection_reasons"].items(), key=lambda kv: -kv[1])[:5]:
        print(f"  review x{count}: {reason}")

    if not args.no_lake and events:
        root = Path(args.lake) if args.lake else default_lake_root()
        root.mkdir(parents=True, exist_ok=True)
        files = persist_events(events, root)
        rows = sum(f.rows for f in files)
        print(f"lake: wrote {rows} events ({len(files)} parquet files) to {root.resolve()}")
    elif not events:
        print("lake: nothing new to write")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
