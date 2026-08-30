"""Tests for the validated canonical ingestion pipeline (SHACL gate)."""

from __future__ import annotations

import pytest
from conftest import CORE_ONTOLOGY, SHAPES_FILE

from foundry.events import EventLog
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline

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
        assert [e.event_type for e in events] == ["EntityCreated", "LocationObserved"]

    def test_observation_for_unknown_entity_is_rejected(self, pipeline):
        result = pipeline.ingest_location_observation(
            entity_name="Ghost Vessel",
            entity_type="Platform",
            location_uri=LOCATION_URI,
            valid_from="2026-08-20T03:00:00Z",
            source_ids=[SOURCE_URI],
        )
        assert not result.accepted
        assert "unresolved entity" in result.reason
        assert pipeline._log.read_all() == []

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
