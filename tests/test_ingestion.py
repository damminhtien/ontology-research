"""Tests for the validated canonical ingestion pipeline (SHACL gate)."""

from __future__ import annotations

import pytest
from conftest import CORE_ONTOLOGY, SHAPES_FILE

from foundry.events import EventLog
from foundry.identity import IdentityService, SourceRecordIdentity
from foundry.ingestion import IngestionPipeline
from foundry.merge import rebuild_identity
from foundry.provenance import stable_source_record_key

LOCATION_URI = "https://data.example/entity/loc-cam-ranh"
SOURCE_URI = "https://data.example/source/sat-pass-0820"


@pytest.fixture()
def pipeline(tmp_path) -> IngestionPipeline:
    """A fresh pipeline with its own identity service and event log."""
    return IngestionPipeline(
        identity=IdentityService(),
        log=EventLog(tmp_path / "events.jsonl"),
        ontology_path=CORE_ONTOLOGY,
        shapes_path=SHAPES_FILE,
    )


class TestEntityIngestion:
    def test_new_entity_emits_entity_created(self, pipeline):
        result = pipeline.ingest_entity(
            name="Coast Guard Region 4",
            entity_type="Organization",
            source_id="https://data.example/source/gov-registry",
        )
        assert result.accepted and result.event_id
        events = pipeline._log.read_all()
        assert len(events) == 1
        assert events[0].event_type == "EntityCreated"

    def test_duplicate_reference_does_not_recreate_entity(self, pipeline):
        first = pipeline.ingest_entity(
            name="Patrol Vessel 01", entity_type="Platform", source_id="s1"
        )
        second = pipeline.ingest_entity(
            name="Patrol Vessel 01", entity_type="Platform", source_id="s1"
        )
        assert first.canonical_id == second.canonical_id
        assert len(pipeline._log.read_all()) == 1

    def test_changed_source_record_content_is_logged_as_a_new_version(self, pipeline):
        first = pipeline.ingest_entity(
            name="Patrol Vessel 01",
            entity_type="Platform",
            source_id="fleet-feed",
            record_id="row-17",
        )
        updated = pipeline.ingest_entity(
            name="Patrol Vessel 01 (updated label)",
            entity_type="Platform",
            source_id="fleet-feed",
            record_id="row-17",
        )

        assert first.canonical_id == updated.canonical_id
        assert updated.event.event_type == "SourceRecordMatched"
        assert len(updated.event.payload["content_hash"]) == 64
        assert len(pipeline._log.read_all()) == 2
        assert (
            pipeline._identity.lookup(
                name="Patrol Vessel 01 (updated label)", entity_type="Platform"
            ).canonical_id
            == first.canonical_id
        )

    def test_aliases_are_persisted_on_entity_created(self, pipeline):
        result = pipeline.ingest_entity(
            name="Ủy ban An toàn Hàng hải",
            entity_type="Organization",
            source_id="https://data.example/source/gov-registry",
            aliases=["Maritime Safety Committee"],
        )
        assert result.accepted
        event = pipeline._log.read_all()[0]
        # the primary name is the Vietnamese label; the English label must be
        # persisted so downstream projections (read model, lake) can serve
        # bilingual data instead of dropping it.
        assert event.payload["name"] == "Ủy ban An toàn Hàng hải"
        assert event.payload["name_aliases"] == ["Maritime Safety Committee"]

    def test_aliases_excluding_primary_name_are_not_repeated(self, pipeline):
        result = pipeline.ingest_entity(
            name="Patrol Vessel 01",
            entity_type="Platform",
            source_id="s1",
            aliases=["Patrol Vessel 01", "PV-01"],
        )
        assert result.accepted
        event = pipeline._log.read_all()[0]
        assert "Patrol Vessel 01" not in event.payload["name_aliases"]
        assert event.payload["name_aliases"] == ["PV-01"]

    def test_external_ids_are_persisted_on_entity_created(self, pipeline):
        result = pipeline.ingest_entity(
            name="Hội Chữ thập đỏ Việt Nam",
            entity_type="Organization",
            source_id="s1",
            external_source="wikidata",
            external_id="Q10832632",
        )
        assert result.accepted
        event = pipeline._log.read_all()[0]
        assert event.payload["external_ids"] == [{"source": "wikidata", "external_id": "Q10832632"}]
        assert event.payload["source_record_key"] == stable_source_record_key("s1", "s1")
        assert len(event.payload["content_hash"]) == 64

    def test_partial_external_identifier_is_rejected_without_a_write(self, pipeline):
        with pytest.raises(ValueError, match="must be supplied together"):
            pipeline.ingest_entity(
                name="Org A",
                entity_type="Organization",
                source_id="catalog",
                external_source="wikidata",
            )
        assert pipeline._log.read_all() == []

    def test_entity_without_external_id_persists_empty_bindings(self, pipeline):
        result = pipeline.ingest_entity(
            name="Patrol Vessel 02", entity_type="Platform", source_id="s1"
        )
        assert result.accepted
        event = pipeline._log.read_all()[0]
        assert event.payload["external_ids"] == []

    def test_unknown_type_is_rejected_not_raised(self, pipeline):
        result = pipeline.ingest_entity(name="X", entity_type="Tank", source_id="s")
        assert not result.accepted
        assert "not an ingestible core type" in result.reason

    @pytest.mark.parametrize("bad_name", ["", "   "])
    def test_malformed_name_raises(self, pipeline, bad_name):
        with pytest.raises(ValueError, match="non-empty"):
            pipeline.ingest_entity(name=bad_name, entity_type="Platform", source_id="s")


class TestObservationIngestion:
    def ingest_platform(self, pipeline: IngestionPipeline) -> str:
        return pipeline.ingest_entity(
            name="Patrol Vessel 01", entity_type="Platform", source_id="s1"
        ).canonical_id

    def test_valid_observation_is_accepted_and_logged(self, pipeline):
        self.ingest_platform(pipeline)
        result = pipeline.ingest_location_observation(
            entity_name="Patrol Vessel 01",
            entity_type="Platform",
            location_uri=LOCATION_URI,
            valid_from="2026-08-20T03:00:00Z",
            source_ids=[SOURCE_URI],
            confidence=0.92,
        )
        assert result.accepted, result.reason
        events = pipeline._log.read_all()
        assert [e.event_type for e in events] == ["EntityCreated", "AssertionMade"]

    def test_observation_for_unknown_entity_is_accepted_pending(self, pipeline):
        result = pipeline.ingest_location_observation(
            entity_name="Ghost Vessel",
            entity_type="Platform",
            location_uri=LOCATION_URI,
            valid_from="2026-08-20T03:00:00Z",
            source_ids=[SOURCE_URI],
        )
        # D12/§4.7: unresolved references are recorded as pending assertions,
        # not rejected — they link up when the entity is minted later
        assert result.accepted and result.pending
        assert result.event.event_type == "AssertionMade"
        assert result.event.payload["subject_id"] == ""
        assert result.event.payload["subject_ref"]["surface_form"] == "Ghost Vessel"
        assert [e.event_type for e in pipeline._log.read_all()] == ["AssertionMade"]

    def test_observation_missing_source_fails_shacl_gate(self, pipeline):
        self.ingest_platform(pipeline)
        with pytest.raises(ValueError, match="at least one source_id"):
            pipeline.ingest_location_observation(
                entity_name="Patrol Vessel 01",
                entity_type="Platform",
                location_uri=LOCATION_URI,
                valid_from="2026-08-20T03:00:00Z",
                source_ids=[],
            )

    def test_invalid_timestamp_raises(self, pipeline):
        self.ingest_platform(pipeline)
        with pytest.raises(ValueError, match="invalid valid_from"):
            pipeline.ingest_location_observation(
                entity_name="Patrol Vessel 01",
                entity_type="Platform",
                location_uri=LOCATION_URI,
                valid_from="not-a-date",
                source_ids=[SOURCE_URI],
            )

    def test_out_of_range_confidence_raises(self, pipeline):
        self.ingest_platform(pipeline)
        with pytest.raises(ValueError, match="outside \\[0, 1\\]"):
            pipeline.ingest_location_observation(
                entity_name="Patrol Vessel 01",
                entity_type="Platform",
                location_uri=LOCATION_URI,
                valid_from="2026-08-20T03:00:00Z",
                source_ids=[SOURCE_URI],
                confidence=1.5,
            )


class TestReviewQueue:
    """B4: identity-stage rejections become durable, replayable queue facts."""

    @staticmethod
    def _pipeline(log_path) -> IngestionPipeline:
        return IngestionPipeline(
            identity=IdentityService(),
            log=EventLog(log_path),
            ontology_path=CORE_ONTOLOGY,
            shapes_path=SHAPES_FILE,
        )

    def test_review_rejection_appends_durable_queue_event(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        pipeline = self._pipeline(log_path)
        pipeline.ingest_entity(
            name="Alpha Patrol Unit One", entity_type="Organization", source_id="s1"
        )
        rejected = pipeline.ingest_entity(
            name="Alpha Patrol Unit Two", entity_type="Organization", source_id="s2"
        )

        assert not rejected.accepted
        assert rejected.event is not None
        assert rejected.event.event_type == "ResolutionReviewQueued"
        assert rejected.event.payload["reference_name"] == "Alpha Patrol Unit Two"
        assert len(rejected.event.payload["candidates"]) >= 1
        assert "needs human review" in rejected.event.payload["reason"]

        events = EventLog(log_path).read_all()
        assert [e.event_type for e in events] == ["EntityCreated", "ResolutionReviewQueued"]

    def test_same_reference_queued_once_per_run(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        pipeline = self._pipeline(log_path)
        pipeline.ingest_entity(
            name="Alpha Patrol Unit One", entity_type="Organization", source_id="s1"
        )
        first = pipeline.ingest_entity(
            name="Alpha Patrol Unit Two", entity_type="Organization", source_id="s2"
        )
        second = pipeline.ingest_entity(
            name="Alpha Patrol Unit Two", entity_type="Organization", source_id="s3"
        )

        assert first.event is not None
        assert second.event is None  # dedup: same unresolved reference, one queue fact
        events = EventLog(log_path).read_all()
        assert [e.event_type for e in events] == ["EntityCreated", "ResolutionReviewQueued"]

    def test_ambiguous_observation_reference_is_queued(self, tmp_path):
        """Ambiguity (multimap collision) needs a human decision — queued, not guessed."""
        log_path = tmp_path / "events.jsonl"
        pipeline = self._pipeline(log_path)
        pipeline.ingest_entity(name="Trung đoàn 101", entity_type="Organization", source_id="s1")
        colliding = "urn:world:entity:" + "e" * 32
        pipeline._identity.register(
            entity_id=colliding, entity_type="Organization", aliases=["Trung đoàn 101"]
        )
        rejected = pipeline.ingest_location_observation(
            entity_name="Trung đoàn 101",
            entity_type="Organization",
            location_uri=LOCATION_URI,
            valid_from="2026-08-20T03:00:00Z",
            source_ids=[SOURCE_URI],
        )
        assert not rejected.accepted
        assert rejected.event is not None
        assert rejected.event.event_type == "ResolutionReviewQueued"


class TestExternalIdBinding:
    """Trusted ids arriving after an alias-hit are recorded as durable facts."""

    @staticmethod
    def _pipeline(log_path) -> IngestionPipeline:
        return IngestionPipeline(
            identity=IdentityService(),
            log=EventLog(log_path),
            ontology_path=CORE_ONTOLOGY,
            shapes_path=SHAPES_FILE,
        )

    def test_alias_hit_with_untrusted_id_requires_review(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        pipeline = self._pipeline(log_path)
        created = pipeline.ingest_entity(
            name="Org A",
            entity_type="Organization",
            source_id="wikidata:Q1",
            external_source="wikidata",
            external_id="Q1",
        )
        assert created.is_new

        # Name similarity and a candidate-only namespace do not establish identity.
        review = pipeline.ingest_entity(
            name="Org A",
            entity_type="Organization",
            source_id="reg:B",
            external_source="reg",
            external_id="B",
        )
        assert not review.accepted
        assert review.event is not None
        assert review.event.event_type == "ResolutionReviewQueued"

        events = EventLog(log_path).read_all()
        assert [e.event_type for e in events] == ["EntityCreated", "ResolutionReviewQueued"]
        assert (
            pipeline._identity.lookup(
                external_source="reg", external_id="B", entity_type="Organization"
            ).method
            == "miss"
        )

    def test_authoritative_match_records_source_record_idempotently(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        pipeline = self._pipeline(log_path)
        pipeline.ingest_entity(
            name="Org A",
            entity_type="Organization",
            source_id="wikidata:Q1",
            external_source="wikidata",
            external_id="Q1",
        )
        pipeline.ingest_entity(
            name="Alternate Org A label",
            entity_type="Organization",
            source_id="catalog",
            record_id="record-2",
            external_source="wikidata",
            external_id="Q1",
        )
        repeat = pipeline.ingest_entity(
            name="Alternate Org A label",
            entity_type="Organization",
            source_id="catalog",
            record_id="record-2",
            external_source="wikidata",
            external_id="Q1",
        )
        assert repeat.accepted and repeat.event is not None
        assert repeat.event.event_type == "SourceRecordMatched"
        assert len(EventLog(log_path).read_all()) == 2

    def test_rebuild_applies_source_record_matches(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        pipeline = self._pipeline(log_path)
        created = pipeline.ingest_entity(
            name="Org A",
            entity_type="Organization",
            source_id="wikidata:Q1",
            external_source="wikidata",
            external_id="Q1",
        )
        pipeline.ingest_entity(
            name="Alternate Org A label",
            entity_type="Organization",
            source_id="catalog",
            record_id="record-2",
            external_source="wikidata",
            external_id="Q1",
        )
        rebuilt = rebuild_identity(EventLog(log_path))
        hit = rebuilt.lookup(
            external_source="wikidata", external_id="Q1", entity_type="Organization"
        )
        assert hit.method == "external_id"
        assert hit.canonical_id == created.canonical_id
        resolution = rebuilt.resolve(
            name="alternate",
            entity_type="Organization",
            source_record=SourceRecordIdentity("catalog", "record-2"),
        )
        assert resolution.decision.value == "MATCH"
        assert resolution.canonical_id == created.canonical_id

    def test_same_ship_name_with_different_authoritative_ids_stays_distinct(self, pipeline):
        first = pipeline.ingest_entity(
            name="INS Same Name",
            entity_type="Platform",
            source_id="hull-source",
            record_id="hull-1",
            external_source="usn-hull",
            external_id="DDG-101",
        )
        second = pipeline.ingest_entity(
            name="INS Same Name",
            entity_type="Platform",
            source_id="fleet-source",
            record_id="fleet-2",
            external_source="usn-hull",
            external_id="DDG-202",
        )
        assert first.accepted and second.accepted
        assert first.canonical_id != second.canonical_id
        expected_bindings = (
            ("DDG-101", first.canonical_id),
            ("DDG-202", second.canonical_id),
        )
        for external_id, expected_id in expected_bindings:
            assert (
                pipeline._identity.lookup(
                    external_source="usn-hull",
                    external_id=external_id,
                    entity_type="Platform",
                ).canonical_id
                == expected_id
            )
