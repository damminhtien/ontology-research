"""Ingest bounded OSINT samples for the Vietnam and South China Sea region."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.events import EVENT_TYPE_REVIEW_QUEUED, EventLog
from foundry.ingestion import IngestionPipeline
from foundry.merge import rebuild_identity
from foundry.namespaces import CORE_LOCATED_AT
from foundry.osint_public_sources import (
    FirmsAreaConnector,
    FirmsHotspotMapper,
    GdacsConnector,
    GdacsEventMapper,
    GlobalFishingWatchConnector,
    GlobalFishingWatchMapper,
    OverpassConnector,
    OverpassMapper,
    RegionalReportRecord,
    UsgsEarthquakeMapper,
    UsgsWeeklyConnector,
)
from foundry.osint_sources import (
    VIETNAM_SCS,
    AdsbPositionMapper,
    AdsbSnapshotConnector,
    AisReportRecord,
    AisStreamConnector,
    AisStreamMapper,
    GdeltConnector,
    GdeltEventMapper,
    GdeltReportRecord,
    OpenSkyConnector,
    OpenSkyMapper,
)
from foundry.paths import crawl_path
from foundry.source_adapters import (
    CheckpointStore,
    JsonHttpClient,
    RawArchive,
    SourceAdapterError,
    load_source_contracts,
    run_connector_page,
)


def main() -> int:
    """Fetch, archive, map, validate, commit and summarize one isolated run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        choices=(
            "all",
            "opensky",
            "adsb-lol",
            "adsb-fi",
            "gdelt",
            "aisstream",
            "openstreetmap",
            "usgs-earthquakes",
            "nasa-firms",
            "global-fishing-watch",
            "gdacs",
        ),
        default="all",
        help="source to collect; all runs the ten regional sources when configured",
    )
    parser.add_argument(
        "--run-id",
        default=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        help="isolated run directory name",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=crawl_path() / "osint-vietnam-scs",
        help="external crawl root; defaults outside the repository",
    )
    parser.add_argument(
        "--source-registry",
        type=Path,
        default=REPO_ROOT / "config" / "source_registry.json",
        help="source contract registry",
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=15,
        help="bounded AISStream collection duration (1-300 seconds)",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=200,
        help="maximum regional records mapped per source in this run",
    )
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("--run-id may contain only letters, digits, dash and underscore")
    if args.max_records < 1:
        parser.error("--max-records must be positive")
    if not 1 <= args.duration_seconds <= 300:
        parser.error("--duration-seconds must be in [1, 300]")

    source_ids = {
        "gdelt": "gdelt-events",
        "openstreetmap": "openstreetmap",
        "usgs-earthquakes": "usgs-earthquakes",
        "nasa-firms": "nasa-firms",
        "global-fishing-watch": "global-fishing-watch",
        "gdacs": "gdacs",
    }
    requested = (
        (
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
        if args.source == "all"
        else (source_ids.get(args.source, args.source),)
    )
    credentials = {
        "aisstream": os.environ.get("AISSTREAM_API_KEY", "").strip(),
        "nasa-firms": os.environ.get("FIRMS_MAP_KEY", "").strip(),
        "global-fishing-watch": os.environ.get("GFW_API_ACCESS_TOKEN", "").strip(),
    }
    if (
        args.source != "all"
        and args.source
        in {
            "aisstream",
            "nasa-firms",
            "global-fishing-watch",
        }
        and not credentials[source_ids.get(args.source, args.source)]
    ):
        parser.error(f"--source {args.source} requires its API credential in the environment")

    run_root = args.root.expanduser() / args.run_id
    run_root.mkdir(parents=True, exist_ok=True)
    run_config = _load_or_create_run_config(run_root)
    log = EventLog(run_root / "events.jsonl")
    identity = rebuild_identity(log)
    pipeline = IngestionPipeline(
        identity=identity,
        log=log,
        ontology_path=REPO_ROOT / "ontology" / "core" / "core.ttl",
        shapes_path=REPO_ROOT / "shapes" / "core_shapes.ttl",
    )
    http = JsonHttpClient(
        user_agent="semantic-foundry-osint/0.1 (+https://github.com/damminhtien/ontology-research)",
        minimum_interval=1.0,
        retries=3,
    )
    archive = RawArchive(run_root / "raw")
    checkpoints = CheckpointStore(run_root / "checkpoints")
    contracts = load_source_contracts(args.source_registry)
    source_results: dict[str, dict[str, object]] = {}

    for source_id in requested:
        if source_id in credentials and not credentials[source_id]:
            source_results[source_id] = {
                "status": "skipped",
                "reason": f"credential for {source_id} is unset",
            }
            continue
        try:
            if source_id in {"opensky", "adsb-lol", "adsb-fi"}:
                if source_id == "opensky":
                    connector = OpenSkyConnector(contract=contracts[source_id], http=http)
                    mapper = OpenSkyMapper(max_records=args.max_records)
                else:
                    connector = AdsbSnapshotConnector(
                        contract=contracts[source_id], http=http, region=VIETNAM_SCS
                    )
                    mapper = AdsbPositionMapper(max_records=args.max_records)
                counts = {"entities": 0, "positions": 0, "reviews": 0, "skipped": 0}

                def ingest_positions(
                    _raw, positions, _archived, counts=counts, current_source_id=source_id
                ) -> None:
                    payload = json.loads(_raw.body)
                    if current_source_id == "opensky":
                        source_rows = payload.get("states") or []
                    else:
                        source_rows = payload.get("ac") or []
                    counts["skipped"] += max(0, len(source_rows) - len(positions))
                    for position in positions:
                        entity = pipeline.ingest_entity(
                            name=position.entity_name,
                            entity_type="Platform",
                            source_id=position.source_id,
                            record_id=position.source_record_id,
                            external_source=position.external_namespace,
                            external_id=position.external_id,
                            content_hash=position.identity_content_hash,
                        )
                        if not entity.accepted:
                            if entity.event and entity.event.event_type == EVENT_TYPE_REVIEW_QUEUED:
                                counts["reviews"] += 1
                                continue
                            raise SourceAdapterError(
                                f"{position.source_id} entity {position.source_record_id} "
                                "was not committed"
                            )
                        counts["entities"] += int(entity.is_new)
                        assertion = pipeline.ingest_assertion(
                            subject_id=entity.canonical_id,
                            predicate_iri=CORE_LOCATED_AT,
                            object_kind="location",
                            object_value=position.location_iri,
                            valid_from=position.observed_at,
                            valid_until=position.valid_until,
                            source_ids=[position.source_id],
                            evidence=[
                                {"source_id": position.source_id, "locator": position.locator}
                            ],
                            evidence_quality="unknown",
                            idempotency_key=(
                                f"osint-position:{position.source_id}:{position.position_record_id}"
                            ),
                        )
                        if not assertion.accepted:
                            raise SourceAdapterError(
                                f"{position.source_id} position "
                                f"{position.position_record_id} rejected: "
                                f"{assertion.reason}"
                            )
                        counts["positions"] += int(not assertion.replayed)

                page = run_connector_page(
                    connector,
                    mapper,
                    archive=archive,
                    checkpoints=checkpoints,
                    handle=ingest_positions,
                )
                source_results[source_id] = {
                    "status": "complete",
                    "snapshot": page.snapshot_hashes[0] if page else None,
                    "mapped_records": page.mapped_records if page else 0,
                    **counts,
                }
            elif source_id == "gdelt-events":
                connector = GdeltConnector(contract=contracts[source_id], http=http)
                mapper = GdeltEventMapper(max_records=args.max_records)
                counts = {"documents": 0}

                def ingest_gdelt(_raw, reports, _archived, counts=counts) -> None:
                    for report in reports:
                        _register_gdelt_report(pipeline, report)
                        counts["documents"] += 1

                page = run_connector_page(
                    connector,
                    mapper,
                    archive=archive,
                    checkpoints=checkpoints,
                    handle=ingest_gdelt,
                )
                source_results[source_id] = {
                    "status": "complete",
                    "snapshot": page.snapshot_hashes[0] if page else None,
                    "mapped_records": page.mapped_records if page else 0,
                    **counts,
                }
            elif source_id == "aisstream":
                connector = AisStreamConnector(
                    contract=contracts[source_id],
                    api_key=credentials[source_id],
                    duration_seconds=args.duration_seconds,
                    max_messages=args.max_records,
                )
                mapper = AisStreamMapper(max_records=args.max_records)
                counts = {"documents": 0}

                def ingest_ais(_raw, reports, _archived, counts=counts) -> None:
                    for report in reports:
                        _register_ais_report(pipeline, report)
                        counts["documents"] += 1

                page = run_connector_page(
                    connector,
                    mapper,
                    archive=archive,
                    checkpoints=checkpoints,
                    handle=ingest_ais,
                )
                source_results[source_id] = {
                    "status": "complete",
                    "snapshot": page.snapshot_hashes[0] if page else None,
                    "mapped_records": page.mapped_records if page else 0,
                    **counts,
                }
            elif source_id == "openstreetmap":
                connector = OverpassConnector(contract=contracts[source_id], http=http)
                previous = checkpoints.load(source_id) or {}
                previous_mapped = int(previous.get("mapped_records", 0))
                mapper = OverpassMapper(
                    max_records=args.max_records,
                    already_mapped=previous_mapped,
                )

                def ingest_overpass(_raw, reports, _archived) -> None:
                    for report in reports:
                        _register_regional_report(pipeline, report)

                page = None
                snapshot_hashes: list[str] = []
                while not previous.get("complete") and previous_mapped < args.max_records:
                    page = run_connector_page(
                        connector,
                        mapper,
                        archive=archive,
                        checkpoints=checkpoints,
                        handle=ingest_overpass,
                    )
                    if page is None:
                        break
                    snapshot_hashes.extend(page.snapshot_hashes)
                    previous_mapped = page.mapped_records
                    if page.complete or previous_mapped >= args.max_records:
                        break
                checkpoint = checkpoints.load(source_id) or {}
                is_complete = checkpoint.get("complete") is True
                source_results[source_id] = {
                    "status": "complete" if is_complete else "limited",
                    "reason": None
                    if is_complete
                    else "max-records reached before all area tiles were queried",
                    "snapshots": snapshot_hashes,
                    "mapped_records": int(checkpoint.get("mapped_records", 0)),
                    "documents": int(checkpoint.get("mapped_records", 0)),
                }
            else:
                if source_id == "usgs-earthquakes":
                    connector = UsgsWeeklyConnector(contract=contracts[source_id], http=http)
                    mapper = UsgsEarthquakeMapper(max_records=args.max_records)
                elif source_id == "nasa-firms":
                    connector = FirmsAreaConnector(
                        contract=contracts[source_id],
                        http=http,
                        map_key=credentials[source_id],
                        snapshot_date=date.fromisoformat(run_config["firms_date"]),
                    )
                    mapper = FirmsHotspotMapper(max_records=args.max_records)
                elif source_id == "global-fishing-watch":
                    connector = GlobalFishingWatchConnector(
                        contract=contracts[source_id],
                        http=http,
                        api_token=credentials[source_id],
                        start_date=date.fromisoformat(run_config["gfw_start_date"]),
                        end_date=date.fromisoformat(run_config["gfw_end_date"]),
                        max_records=args.max_records,
                    )
                    mapper = GlobalFishingWatchMapper(max_records=args.max_records)
                elif source_id == "gdacs":
                    connector = GdacsConnector(contract=contracts[source_id], http=http)
                    mapper = GdacsEventMapper(max_records=args.max_records)
                else:
                    raise SourceAdapterError(f"unsupported source {source_id}")
                counts = {"documents": 0}

                def ingest_reports(_raw, reports, _archived, counts=counts) -> None:
                    for report in reports:
                        _register_regional_report(pipeline, report)
                        counts["documents"] += 1

                page = run_connector_page(
                    connector,
                    mapper,
                    archive=archive,
                    checkpoints=checkpoints,
                    handle=ingest_reports,
                )
                source_results[source_id] = {
                    "status": "complete",
                    "snapshot": page.snapshot_hashes[0] if page else None,
                    "mapped_records": page.mapped_records if page else 0,
                    **counts,
                }
        except SourceAdapterError as exc:
            source_results[source_id] = {"status": "error", "error": str(exc)}

    event_counts = Counter(event.event_type for event in log.iter_all())
    summary = {
        "run_id": args.run_id,
        "run_root": str(run_root.resolve()),
        "event_log": str((run_root / "events.jsonl").resolve()),
        "area_of_interest": {
            "name": VIETNAM_SCS.name,
            "south": VIETNAM_SCS.south,
            "west": VIETNAM_SCS.west,
            "north": VIETNAM_SCS.north,
            "east": VIETNAM_SCS.east,
            "is_political_boundary": False,
        },
        "sources": source_results,
        "source_windows": run_config,
        "event_counts": dict(sorted(event_counts.items())),
        "total_events": sum(event_counts.values()),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    summary_path = run_root / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if any(result.get("status") == "error" for result in source_results.values()) else 0


def _register_gdelt_report(pipeline: IngestionPipeline, report: GdeltReportRecord) -> None:
    """Register one GDELT-coded report without asserting it as verified fact."""
    pipeline.register_document(
        uri=report.locator,
        title=report.title,
        source_system=report.source_id,
        content_ref=report.locator,
        record_id=report.record_id,
        content=report.text,
    )


def _register_ais_report(pipeline: IngestionPipeline, report: AisReportRecord) -> None:
    """Register one validated AISStream report, keeping receipt-time semantics."""
    pipeline.register_document(
        uri=report.locator,
        title=report.title,
        source_system=report.source_id,
        content_ref=report.locator,
        record_id=report.record_id,
        content=report.text,
    )


def _register_regional_report(pipeline: IngestionPipeline, report: RegionalReportRecord) -> None:
    """Register a mapper output as a document, never as an unverified assertion."""
    pipeline.register_document(
        uri=report.locator,
        title=report.title,
        source_system=report.source_id,
        content_ref=report.locator,
        record_id=report.record_id,
        content=report.text,
    )


def _load_or_create_run_config(run_root: Path) -> dict[str, str]:
    """Pin date-sensitive source windows so interrupted runs are reproducible."""
    path = run_root / "run-config.json"
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceAdapterError(f"cannot read run config {path}: {exc}") from exc
        required = {"created_at", "firms_date", "gfw_start_date", "gfw_end_date"}
        if not isinstance(config, dict) or not required.issubset(config):
            raise SourceAdapterError(f"invalid run config at {path}")
        return {key: str(config[key]) for key in required}

    now = datetime.now(UTC)
    today = now.date()
    config = {
        "created_at": now.isoformat(),
        "firms_date": (today - timedelta(days=1)).isoformat(),
        "gfw_start_date": (today - timedelta(days=10)).isoformat(),
        "gfw_end_date": (today - timedelta(days=3)).isoformat(),
    }
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config


if __name__ == "__main__":
    raise SystemExit(main())
