"""Ingest an AIS-style tracking feed through the canonical pipeline (Phase 4).

Maps a JSONL sensor feed (mmsi/name/sensor_id/track_id/location_uri/at_time)
into the tracking vertical: platforms resolve by MMSI (trusted external id,
ADR-0006), sensors register with their serials, and every observation/track
passes the domain SHACL contracts before entering the log.

Usage:
    .venv/bin/python tools/ingest_tracking_feed.py --feed feed.jsonl \
        [--log data/production.jsonl] [--lake /path | --no-lake]

Exit 0 with a per-stage summary; rejections (SHACL) are reported, never dropped.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.events import EventLog  # noqa: E402
from foundry.ingestion import IngestionPipeline  # noqa: E402
from foundry.lake import default_lake_root, persist_events  # noqa: E402
from foundry.merge import rebuild_identity  # noqa: E402
from foundry.tracking import parse_feed  # noqa: E402


def main() -> int:
    """Run one tracking-feed ingestion pass."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", required=True, help="JSONL feed file")
    parser.add_argument("--log", default="data/production.jsonl", help="event log path")
    parser.add_argument("--lake", default=None, help="lake root")
    parser.add_argument("--no-lake", action="store_true", help="skip the lake write")
    args = parser.parse_args()

    records = parse_feed(Path(args.feed).read_text(encoding="utf-8").splitlines())
    print(f"feed: {len(records)} record(s) from {args.feed}")

    log_path = Path(args.log)
    log = EventLog(log_path)
    identity = rebuild_identity(log)
    print(f"registry: {len(identity)} known canonical entit(ies)")
    pipeline = IngestionPipeline(
        identity=identity,
        log=log,
        ontology_path=REPO_ROOT / "ontology" / "core" / "core.ttl",
        shapes_path=REPO_ROOT / "shapes" / "core_shapes.ttl",
    )

    # identity for platforms comes from the feed's MMSI column
    platform_by_mmsi: dict[str, str] = {}
    sensors: dict[str, str] = {}
    events = []
    accepted = rejected = 0

    for record in records:
        if record.mmsi not in platform_by_mmsi:
            entity = pipeline.ingest_entity(
                name=record.name,
                entity_type="Platform",
                external_source="ais-mmsi",
                external_id=record.mmsi,
                source_id="tracking-feed",
            )
            if not entity.accepted:
                print(f"platform {record.mmsi} rejected: {entity.reason}", file=sys.stderr)
                rejected += 1
                continue
            platform_by_mmsi[record.mmsi] = entity.canonical_id
            if entity.event is not None:
                events.append(entity.event)

        if record.sensor_id not in sensors:
            sensor = pipeline.ingest_sensor(
                sensor_id=record.sensor_id,
                name=f"Sensor {record.sensor_id}",
                platform_name=record.name,
                source_id="tracking-feed",
            )
            if not sensor.accepted:
                print(f"sensor {record.sensor_id} rejected: {sensor.reason}", file=sys.stderr)
                rejected += 1
                continue
            sensors[record.sensor_id] = sensor.canonical_id
            if sensor.event is not None:
                events.append(sensor.event)

        observation = pipeline.ingest_observation(
            sensor_entity_id=sensors[record.sensor_id],
            subject_name=record.name,
            subject_entity_id=platform_by_mmsi[record.mmsi],
            at_time=record.at_time,
            location_uri=record.location_uri,
            source_ids=["tracking-feed"],
            confidence=0.9,
        )
        if not observation.accepted:
            print(f"observation rejected: {observation.reason}", file=sys.stderr)
            rejected += 1
            continue
        events.append(observation.event)
        accepted += 1

        track = pipeline.ingest_track(
            track_id=record.track_id,
            subject_name=record.name,
            subject_entity_id=platform_by_mmsi[record.mmsi],
            observation_ids=[observation.event.payload["observation_id"]],
            source_ids=["tracking-feed"],
        )
        if track.accepted and track.event is not None:
            events.append(track.event)
            accepted += 1
        else:
            rejected += 1

    print(f"ingested: {accepted} accepted, {rejected} rejected")
    if not args.no_lake and events:
        root = Path(args.lake) if args.lake else default_lake_root()
        root.mkdir(parents=True, exist_ok=True)
        files = persist_events(events, root)
        print(f"lake: wrote {sum(f.rows for f in files)} event(s) to {root.resolve()}")
    return 0 if rejected == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
