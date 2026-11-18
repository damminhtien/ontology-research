"""Contracts for cross-run OSINT identity linking and exact deduplication."""

from __future__ import annotations

from pathlib import Path

from conftest import CORE_ONTOLOGY, SHAPES_FILE

from foundry.events import EventLog
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline
from foundry.merge import rebuild_identity
from foundry.osint_consolidation import consolidate_crawl_data


def _pipeline(log: EventLog) -> IngestionPipeline:
    return IngestionPipeline(
        identity=IdentityService(),
        log=log,
        ontology_path=CORE_ONTOLOGY,
        shapes_path=SHAPES_FILE,
    )


def _write_run(root: Path, run_id: str, source_id: str, namespace: str) -> None:
    log = EventLog(root / run_id / "events.jsonl")
    pipeline = _pipeline(log)
    created = pipeline.ingest_entity(
        name=f"Aircraft {source_id}",
        entity_type="Platform",
        source_id=source_id,
        record_id="abc123",
        external_source=namespace,
        external_id="abc123",
        content_hash="a" * 64,
    )
    assert created.accepted
    pipeline.register_document(
        uri="https://example.org/report/42",
        title="Regional report",
        source_system="gdelt-events",
        record_id="42",
        content="unchanged source report",
    )


def test_consolidation_links_shared_icao24_and_deduplicates_documents(tmp_path):
    crawl_root = tmp_path / "crawl-runs"
    _write_run(crawl_root, "run-one", "opensky", "opensky-icao24")
    _write_run(crawl_root, "run-two", "adsb-fi", "adsb-fi-icao24")
    destination = EventLog(tmp_path / "production.jsonl")

    result = consolidate_crawl_data(
        crawl_root=crawl_root,
        destination=destination,
        ontology_path=CORE_ONTOLOGY,
        shapes_path=SHAPES_FILE,
    )

    events = destination.read_all()
    assert [event.event_type for event in events] == [
        "EntityCreated",
        "DocumentRegistered",
        "SourceRecordMatched",
    ]
    identity = rebuild_identity(destination)
    source_owners = set(identity.store.source_records().values())
    assert len(source_owners) == 1
    assert len(identity.store.source_records()) == 2
    assert result.source_logs == 2
    assert result.entity_records_linked == 2
    assert result.entities_created == 1
    assert result.identities_reused == 1
    assert result.documents_written == 1
    assert result.deduplicated_records == 1

    replay = consolidate_crawl_data(
        crawl_root=crawl_root,
        destination=destination,
        ontology_path=CORE_ONTOLOGY,
        shapes_path=SHAPES_FILE,
    )
    assert replay.new_events == 0
    assert len(destination.read_all()) == len(events)


def test_consolidation_keeps_legacy_aircraft_review_without_matching_archive(tmp_path):
    crawl_root = tmp_path / "crawl-runs"
    source_log = EventLog(crawl_root / "run-one" / "events.jsonl")
    _pipeline(source_log).queue_identity_review(
        reference_name="Aircraft deadbe",
        external_source="adsb-fi-icao24",
        external_id="deadbe",
        entity_type="Platform",
        candidates=(),
        reason="legacy provider-local identifier needs verification",
    )
    destination = EventLog(tmp_path / "production.jsonl")

    result = consolidate_crawl_data(
        crawl_root=crawl_root,
        destination=destination,
        ontology_path=CORE_ONTOLOGY,
        shapes_path=SHAPES_FILE,
    )

    reviews = [
        event for event in destination.read_all() if event.event_type == "ResolutionReviewQueued"
    ]
    assert len(reviews) == 1
    assert reviews[0].payload["external_source"] == "icao24"
    assert result.stale_aircraft_reviews_skipped == 0
