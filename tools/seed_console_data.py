"""Seed a small event log for the Ontology Console Data Monitor view.

Runs the real ingestion pipeline end-to-end: entities, location observations,
and one deliberately rejected record so the console shows the review path too.

Usage: .venv/bin/python tools/seed_console_data.py [--force]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.events import EventLog
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline

DEFAULT_LOG = REPO_ROOT / "data" / "events.jsonl"

SOURCE_REGISTRY = "https://data.example/source/gov-registry"
SOURCE_SAT = "https://data.example/source/sat-pass-0820"
LOC_CAM_RANH = "https://data.example/entity/loc-cam-ranh"


def main() -> int:
    """Seed the demo event log."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite an existing log")
    args = parser.parse_args()

    log_path = DEFAULT_LOG
    if log_path.exists():
        if not args.force:
            print(f"{log_path} already exists; use --force to overwrite.")
            return 1
        log_path.unlink()

    pipeline = IngestionPipeline(
        identity=IdentityService(),
        log=EventLog(log_path),
        ontology_path=REPO_ROOT / "ontology" / "core" / "core.ttl",
        shapes_path=REPO_ROOT / "shapes" / "core_shapes.ttl",
    )

    for name, entity_type in (
        ("Coast Guard Region 4", "Organization"),
        ("Maritime Headquarters", "Organization"),
        ("Patrol Vessel 01", "Platform"),
        ("Patrol Vessel 02", "Platform"),
    ):
        result = pipeline.ingest_entity(
            name=name, entity_type=entity_type, source_id=SOURCE_REGISTRY
        )
        print(
            ("accepted " if result.accepted else "rejected ") + f"{name} -> {result.canonical_id}"
        )

    for platform, at_time in (
        ("Patrol Vessel 01", "2026-08-20T03:00:00Z"),
        ("Patrol Vessel 02", "2026-08-21T09:30:00Z"),
    ):
        location = (
            LOC_CAM_RANH if platform.endswith("01") else "https://data.example/entity/loc-hanoi"
        )
        result = pipeline.ingest_location_observation(
            entity_name=platform,
            entity_type="Platform",
            location_uri=location,
            valid_from=at_time,
            source_ids=[SOURCE_SAT],
            confidence=0.9,
        )
        print(("accepted " if result.accepted else "rejected ") + f"observation of {platform}")

    # Deliberate rejection: unknown entity reference routes to the review queue.
    rejected = pipeline.ingest_location_observation(
        entity_name="Ghost Vessel",
        entity_type="Platform",
        location_uri=LOC_CAM_RANH,
        valid_from="2026-08-22T00:00:00Z",
        source_ids=[SOURCE_SAT],
    )
    print(f"rejected (expected) unresolved-entity reference: {rejected.reason.splitlines()[0]}")

    events = EventLog(log_path).read_all()
    print(f"\nSeeded {len(events)} events into {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
