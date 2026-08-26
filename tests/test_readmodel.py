"""Tests for the read model and projector (CQRS projection contract)."""

from __future__ import annotations

import pytest

from foundry.events import EventLog, make_event
from foundry.projector import Projector, replay_log
from foundry.readmodel import ReadModel, parse_instant

E1 = "urn:world:entity:aaaa"
E2 = "urn:world:entity:bbbb"
LOC_A = "https://data.example/entity/loc-da-nang"
LOC_B = "https://data.example/entity/loc-cam-ranh"
SRC = "https://data.example/source/ais-feed"


def _entity_event(entity_id: str, name: str, occurred: str):
    return make_event(
        "EntityCreated",
        {
            "entity_id": entity_id,
            "entity_type": "Platform",
            "name": name,
            "source_id": SRC,
            "confidence": 1.0,
        },
    )


def _location_event(entity_id: str, location: str, valid_from: str):
    return make_event(
        "LocationObserved",
        {
            "entity_id": entity_id,
            "location_uri": location,
            "valid_from": valid_from,
            "source_ids": [SRC],
            "confidence": 0.9,
        },
    )


@pytest.fixture()
def projected_model(tmp_path) -> ReadModel:
    """Two entities; E1 observed at LOC_A then later at LOC_B."""
    log = EventLog(tmp_path / "events.jsonl")
    log.extend(
        [
            _entity_event(E1, "Patrol Vessel 01", "2026-08-01T00:00:00Z"),
            _entity_event(E2, "Patrol Vessel 02", "2026-08-01T01:00:00Z"),
            _location_event(E1, LOC_A, "2026-07-15T00:00:00Z"),
            _location_event(E1, LOC_B, "2026-08-20T03:00:00Z"),
        ]
    )
    model, _stats = replay_log(log)
    return model


class TestParseInstant:
    def test_parses_z_and_offset(self):
        assert parse_instant("2026-08-20T03:00:00Z") == parse_instant("2026-08-20T03:00:00+00:00")

    def test_rejects_naive_and_malformed(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            parse_instant("2026-08-20 03:00:00")
        with pytest.raises(ValueError, match="invalid instant"):
            parse_instant("not-a-date")


class TestReadModelQueries:
    def test_q1_entity_lookup(self, projected_model):
        view = projected_model.get_entity(E1)
        assert view is not None
        assert view.name == "Patrol Vessel 01"
        assert view.entity_type == "Platform"
        assert projected_model.get_entity("urn:missing") is None

    def test_find_by_name_is_case_insensitive(self, projected_model):
        assert projected_model.find_by_name("patrol vessel 01")[0].entity_id == E1

    def test_current_location_is_latest(self, projected_model):
        fact = projected_model.current_location(E1)
        assert fact.location_uri == LOC_B
        assert fact.as_of == "2026-08-20T03:00:00Z"
        assert fact.source_ids == (SRC,)

    def test_q4_temporal_asof(self, projected_model):
        early = projected_model.location_as_of(E1, parse_instant("2026-08-01T00:00:00Z"))
        assert early.location_uri == LOC_A

        later = projected_model.location_as_of(E1, parse_instant("2026-08-21T00:00:00Z"))
        assert later.location_uri == LOC_B

        before_any = projected_model.location_as_of(E1, parse_instant("2026-01-01T00:00:00Z"))
        assert before_any is None

    def test_entities_at_reverse_index(self, projected_model):
        assert projected_model.entities_at(LOC_B) == {E1}
        assert projected_model.entities_at(LOC_A) == set()

    def test_stats(self, projected_model):
        # Events were stamped at make_event time (real clock), so any instant
        # in the future yields a positive, measurable lag.
        stats = projected_model.stats(now=parse_instant("2099-01-01T00:00:00Z"))
        assert stats["entities"] == 2
        assert stats["with_location"] == 1
        assert stats["locations"] == 2
        assert stats["lag_seconds"] > 0
        assert stats["last_event_time"] is not None


class TestProjectorContract:
    def test_replay_is_idempotent(self, tmp_path):
        events = [
            _entity_event(E1, "V01", "2026-08-01T00:00:00Z"),
            _location_event(E1, LOC_A, "2026-07-15T00:00:00Z"),
            _location_event(E1, LOC_B, "2026-08-20T03:00:00Z"),
        ]

        def build() -> tuple[dict, dict, dict]:
            model = ReadModel()
            stats = Projector(model).replay(list(events))
            return (
                model.get_entity(E1).__dict__,
                model.current_location(E1).__dict__,
                {"applied": stats.applied, "skipped": stats.skipped},
            )

        assert build() == build()

    def test_out_of_order_appends_project_latest_by_valid_from(self, tmp_path):
        # Appended newest-first on purpose: validFrom decides, not order.
        log = EventLog(tmp_path / "events.jsonl")
        log.extend(
            [
                _entity_event(E1, "V01", "2026-08-01T00:00:00Z"),
                _location_event(E1, LOC_B, "2026-08-20T03:00:00Z"),
                _location_event(E1, LOC_A, "2026-07-15T00:00:00Z"),
            ]
        )
        model, _ = replay_log(log)
        assert model.current_location(E1).location_uri == LOC_B
        early = model.location_as_of(E1, parse_instant("2026-08-01T00:00:00Z"))
        assert early.location_uri == LOC_A

    def test_unknown_event_types_are_skipped_not_fatal(self, tmp_path):
        model = ReadModel()
        known = make_event(
            "EntityCreated",
            {
                "entity_id": E1,
                "entity_type": "Platform",
                "name": "V01",
                "source_id": SRC,
                "confidence": 1.0,
            },
        )
        unknown = type(known)(
            event_id=known.event_id,
            event_type="AffiliationAssessed",
            schema_version=known.schema_version,
            occurred_at=known.occurred_at,
            payload=known.payload,
        )
        stats = Projector(model).replay([known, unknown])
        assert stats.applied == 1
        assert stats.skipped == 1
        assert stats.by_type["AffiliationAssessed"] == 1

    def test_replay_log_builds_fresh_model(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        log.extend(
            [
                _entity_event(E1, "V01", "2026-08-01T00:00:00Z"),
                _location_event(E1, LOC_A, "2026-07-15T00:00:00Z"),
            ]
        )
        model, stats = replay_log(log)
        assert stats.applied == 2
        assert model.get_entity(E1) is not None
        assert model.current_location(E1).location_uri == LOC_A
