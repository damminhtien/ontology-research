"""Consolidate historical OSINT run logs into the production event log.

Usage:
    .venv/bin/python tools/consolidate_osint_runs.py [--dry-run]

Run logs and archived snapshots remain unchanged. The destination log receives
normal validated events and a completion manifest is written outside the repo.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.events import EventLog  # noqa: E402
from foundry.merge import rebuild_identity  # noqa: E402
from foundry.osint_consolidation import consolidate_crawl_data  # noqa: E402
from foundry.paths import crawl_path, production_event_log_path  # noqa: E402


def main() -> int:
    """Replay old crawl evidence through the shared production write path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=crawl_path() / "osint-vietnam-scs",
        help="historical crawl run root",
    )
    parser.add_argument(
        "--event-log",
        type=Path,
        default=production_event_log_path(),
        help="canonical destination event log",
    )
    parser.add_argument("--dry-run", action="store_true", help="validate inputs without writing")
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    if args.dry_run:
        report = _dry_run(root)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    destination = EventLog(args.event_log.expanduser())
    result = consolidate_crawl_data(
        crawl_root=root,
        destination=destination,
        ontology_path=REPO_ROOT / "ontology" / "core" / "core.ttl",
        shapes_path=REPO_ROOT / "shapes" / "core_shapes.ttl",
    )
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "crawl_root": str(root),
        "event_log": str(destination.path.resolve()),
        "last_sequence": destination.last_sequence,
        "result": result.to_dict(),
        "canonical_log": _canonical_log_summary(destination),
    }
    manifest_path = root / "canonicalization-manifest.json"
    temporary_path = manifest_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(manifest_path)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _canonical_log_summary(log: EventLog) -> dict[str, object]:
    """Report final destination totals, including earlier resumable passes."""
    counts: Counter[str] = Counter()
    document_versions: set[tuple[str, str]] = set()
    for event in log.iter_all():
        counts[event.event_type] += 1
        if event.event_type == "DocumentRegistered":
            document_versions.add(
                (event.payload["source_record_key"], event.payload["content_hash"])
            )
    identity = rebuild_identity(log)
    canonical_entities = {
        identity.merged_into(entity_id) or entity_id for entity_id in identity.store.entity_ids()
    }
    return {
        "last_sequence": log.last_sequence,
        "event_counts": dict(sorted(counts.items())),
        "canonical_entities": len(canonical_entities),
        "source_records": len(identity.store.source_records()),
        "document_versions": len(document_versions),
        "assertions": counts["AssertionMade"],
    }


def _dry_run(root: Path) -> dict[str, object]:
    """Check run event contracts and archive manifests without creating events."""
    from foundry.osint_consolidation import (
        AIRCRAFT_SOURCE_IDS,
        SUPPORTED_EVENT_TYPES,
        _read_archive,
    )
    from foundry.source_adapters import load_source_contracts

    logs = sorted(path for path in root.glob("*/events.jsonl") if path.is_file())
    event_count = 0
    event_types: dict[str, int] = {}
    for path in logs:
        for event in EventLog(path).iter_all():
            event_count += 1
            event_types[event.event_type] = event_types.get(event.event_type, 0) + 1
            if event.event_type not in SUPPORTED_EVENT_TYPES:
                raise ValueError(f"unsupported event type {event.event_type} in {path}")
    raw_manifests = sorted(root.glob("*/raw/*/manifest.jsonl"))
    contracts = load_source_contracts(REPO_ROOT / "config" / "source_registry.json")
    raw_snapshots = 0
    for manifest in raw_manifests:
        source_id = manifest.parent.name
        if source_id in AIRCRAFT_SOURCE_IDS:
            raw_snapshots += sum(
                1 for _ in _read_archive(manifest, source_id, contracts[source_id])
            )
    return {
        "dry_run": True,
        "source_logs": len(logs),
        "source_events": event_count,
        "raw_manifests": len(raw_manifests),
        "aircraft_raw_snapshots_checksum_verified": raw_snapshots,
        "event_types": dict(sorted(event_types.items())),
        "destination": "unchanged",
    }


if __name__ == "__main__":
    raise SystemExit(main())
