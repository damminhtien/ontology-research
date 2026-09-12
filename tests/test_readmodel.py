"""Tests for the read model and projector (CQRS projection contract)."""

from __future__ import annotations

from dataclasses import replace

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
            event_id=known.event_id + "unknown",
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


class TestSequenceOrderedReplay:
    """B2: replay follows the log sequence, dedups per event, checkpoints."""

    def test_replay_orders_by_sequence_not_wall_clock(self):
        created = _entity_event(E1, "V01", "2026-08-01T00:00:00Z")
        observed = _location_event(E1, LOC_A, "2026-07-15T00:00:00Z")
        created, observed = (
            replace(created, sequence=1),
            replace(observed, sequence=2),
        )
        # same second, and the list arrives in REVERSE log order
        model = ReadModel()
        stats = Projector(model).replay([observed, created])
        assert stats.applied == 2
        assert model.get_entity(E1) is not None
        assert model.current_location(E1).location_uri == LOC_A

    def test_duplicate_apply_is_counted_not_reapplied(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        log.extend(
            [
                _entity_event(E1, "V01", "2026-08-01T00:00:00Z"),
                _location_event(E1, LOC_A, "2026-07-15T00:00:00Z"),
                _location_event(E1, LOC_B, "2026-08-20T03:00:00Z"),
            ]
        )
        events = log.read_all()
        model = ReadModel()
        projector = Projector(model)
        first = projector.replay(events)
        assert first.applied == 3 and first.duplicates == 0

        second = projector.replay(events)
        assert second.applied == 0
        assert second.duplicates == 3
        # location history did not double: current location still the latest
        assert model.current_location(E1).location_uri == LOC_B
        assert model.entities_at(LOC_A) == set()  # E1 moved to LOC_B

    def test_checkpoint_resume_continues_incremental_replay(self, tmp_path):
        """Checkpoint anchors incremental replay: the prefix is skipped, not re-applied.

        The in-memory model is not persisted (ADR-0004); ``after_sequence`` is
        meaningful for a model whose state already covers the skipped prefix
        (same model continued, or a store-backed model hydrated elsewhere).
        """
        log = EventLog(tmp_path / "events.jsonl")
        log.extend(
            [
                _entity_event(E1, "V01", "2026-08-01T00:00:00Z"),
                _entity_event(E2, "V02", "2026-08-01T01:00:00Z"),
                _location_event(E1, LOC_A, "2026-07-15T00:00:00Z"),
                _location_event(E1, LOC_B, "2026-08-20T03:00:00Z"),
                _location_event(E2, LOC_A, "2026-08-21T03:00:00Z"),
            ]
        )
        events = log.read_all()

        model = ReadModel()
        projector = Projector(model)
        projector.replay(events[:2])
        checkpoint = tmp_path / "checkpoint.json"
        projector.save_checkpoint(checkpoint)
        assert model.checkpoint_sequence == 2

        after = projector.load_checkpoint(checkpoint)
        stats = projector.replay(events, after_sequence=after)
        assert stats.applied == 3  # only the suffix was folded in
        assert stats.skipped == 2  # the checkpointed prefix
        assert stats.duplicates == 0
        assert model.checkpoint_sequence == 5
        assert model.current_location(E1).location_uri == LOC_B
        assert model.entities_at(LOC_A) == {E2}

        # a second full pass with the checkpoint skips the prefix and dedups the rest
        stats = projector.replay(events, after_sequence=after)
        assert stats.applied == 0
        assert stats.skipped == 2 and stats.duplicates == 3

    def test_malformed_checkpoint_rejected(self, tmp_path):
        path = tmp_path / "checkpoint.json"
        path.write_text('{"last_sequence": "soon"}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="malformed checkpoint"):
            Projector(ReadModel()).load_checkpoint(path)

    def test_incremental_apply_keeps_reverse_index_current(self):
        """Streaming apply (no full replay) keeps entities_at correct."""
        model = ReadModel()
        projector = Projector(model)
        projector.apply(_entity_event(E1, "V01", "2026-08-01T00:00:00Z"))
        observed_a = _location_event(E1, LOC_A, "2026-07-15T00:00:00Z")
        projector.apply(observed_a)
        assert model.entities_at(LOC_A) == {E1}

        observed_b = _location_event(E1, LOC_B, "2026-08-20T03:00:00Z")
        projector.apply(observed_b)
        assert model.entities_at(LOC_A) == set()  # moved, index updated in place
        assert model.entities_at(LOC_B) == {E1}


class TestReadModelSnapshot:
    """Persisted model cache: round-trip must be state-identical."""

    def test_snapshot_round_trip(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        log.extend(
            [
                _entity_event(E1, "V01", "2026-08-01T00:00:00Z"),
                _entity_event(E2, "V02", "2026-08-01T01:00:00Z"),
                _location_event(E1, LOC_A, "2026-07-15T00:00:00Z"),
                _location_event(E1, LOC_B, "2026-08-20T03:00:00Z"),
                make_event(
                    "EntityMerged",
                    {
                        "survivor_id": E2,
                        "duplicate_id": E1,
                        "moved_aliases": [],
                        "moved_external_ids": [],
                        "reason": "r",
                    },
                ),
            ]
        )
        model, _stats = replay_log(log)
        snapshot = tmp_path / "model.pkl"
        model.save_snapshot(snapshot)

        restored = ReadModel.load_snapshot(snapshot)
        assert restored is not None
        assert restored.checkpoint_sequence == model.checkpoint_sequence
        assert restored.applied_event_count == model.applied_event_count
        assert restored.get_entity(E1) == model.get_entity(E1)
        assert restored.merged_into(E1) == model.merged_into(E1)
        assert restored.current_location(E2) == model.current_location(E2)
        assert restored.entities_at(LOC_B) == model.entities_at(LOC_B)
        assert restored.stats() == model.stats()

    def test_snapshot_supports_incremental_resume(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        log.extend([_entity_event(E1, "V01", "2026-08-01T00:00:00Z")])
        events = log.read_all()
        model = ReadModel()
        Projector(model).replay(events)
        snapshot = tmp_path / "model.pkl"
        model.save_snapshot(snapshot)

        # server restart: hydrate from the snapshot, apply only the suffix
        restored = ReadModel.load_snapshot(snapshot)
        assert restored is not None
        checkpoint = restored.checkpoint_sequence
        log.append(_location_event(E1, LOC_B, "2026-08-25T03:00:00Z"))
        Projector(restored).replay(log.read_all(), after_sequence=checkpoint)
        assert restored.current_location(E1).location_uri == LOC_B

        # snapshot again: the cycle is idempotent
        restored.save_snapshot(snapshot)
        again = ReadModel.load_snapshot(snapshot)
        assert again is not None
        assert again.checkpoint_sequence == restored.checkpoint_sequence
        assert again.current_location(E1) == restored.current_location(E1)

    def test_load_snapshot_missing_or_corrupt_degrades_to_none(self, tmp_path):
        assert ReadModel.load_snapshot(tmp_path / "missing.pkl") is None
        corrupt = tmp_path / "corrupt.pkl"
        corrupt.write_bytes(b"not a pickle")
        assert ReadModel.load_snapshot(corrupt) is None
