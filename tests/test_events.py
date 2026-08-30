"""Tests for the append-only event log and event contract."""

from __future__ import annotations

import json

import pytest

from foundry.events import (
    SCHEMA_VERSION,
    EventLog,
    event_from_dict,
    event_to_dict,
    make_event,
)


class TestEventContract:
    def test_make_event_rejects_unknown_type(self):
        with pytest.raises(ValueError, match="unknown event type"):
            make_event("EntityDeleted", {})

    def test_make_event_assigns_id_and_timestamp(self):
        event = make_event("EntityCreated", {"entity_id": "urn:x:1"})
        assert event.event_id
        assert event.schema_version == SCHEMA_VERSION
        assert event.occurred_at.endswith("Z")

    def test_round_trip_serialization(self):
        original = make_event("LocationObserved", {"entity_id": "urn:x:1", "confidence": None})
        restored = event_from_dict(json.loads(json.dumps(event_to_dict(original))))
        assert restored == original

    def test_from_dict_rejects_missing_fields(self):
        with pytest.raises(ValueError, match="missing field"):
            event_from_dict({"schema_version": SCHEMA_VERSION})

    def test_from_dict_rejects_invalid_schema_version(self):
        with pytest.raises(ValueError, match="invalid schema_version"):
            event_from_dict({"event_id": "abc"})

    def test_from_dict_rejects_newer_schema_version(self):
        record = event_to_dict(make_event("EntityCreated", {}))
        record["schema_version"] = SCHEMA_VERSION + 1
        with pytest.raises(ValueError, match="newer than supported"):
            event_from_dict(record)

    def test_from_dict_rejects_upcast_gap(self):
        record = event_to_dict(make_event("EntityCreated", {}))
        record["schema_version"] = SCHEMA_VERSION - 2  # no path from v0
        with pytest.raises(ValueError, match="no upcast path"):
            event_from_dict(record)

    def test_valid_at_round_trips(self):
        event = make_event(
            "LocationObserved",
            {"entity_id": "urn:x:1", "location_uri": "urn:world:location:x"},
            valid_at="2026-07-01T00:00:00Z",
        )
        assert event.valid_at == "2026-07-01T00:00:00Z"
        restored = event_from_dict(json.loads(json.dumps(event_to_dict(event))))
        assert restored == event

    def test_valid_at_defaults_to_none(self):
        assert make_event("EntityCreated", {}).valid_at is None


class TestAppendOnlyLog:
    def test_append_and_replay_round_trip(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        events = [
            make_event("EntityCreated", {"entity_id": f"urn:x:{i}", "name": f"e{i}"})
            for i in range(3)
        ]
        log.extend(events)

        replayed = log.read_all()
        assert [e.event_id for e in replayed] == [e.event_id for e in events]

    def test_read_all_empty_when_no_file(self, tmp_path):
        assert EventLog(tmp_path / "missing.jsonl").read_all() == []

    def test_corrupt_line_fails_loudly(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.write_text('{"broken": true}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="invalid schema_version"):
            EventLog(path).read_all()

    def test_log_is_append_only_by_contract(self, tmp_path):
        """The API surface must not expose update/delete operations."""
        public_api = {name for name in dir(EventLog) if not name.startswith("_")}
        forbidden = {"update", "delete", "truncate", "rewrite", "remove", "pop"}
        assert not public_api & forbidden


class TestSequenceStamping:
    def test_append_stamps_sequential_sequences(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        for i in range(3):
            log.append(make_event("EntityCreated", {"entity_id": f"urn:x:{i}"}))
        replayed = log.read_all()
        assert [e.sequence for e in replayed] == [1, 2, 3]

    def test_extend_continues_sequence_across_instances(self, tmp_path):
        path = tmp_path / "events.jsonl"
        EventLog(path).extend([make_event("EntityCreated", {"n": 1}) for _ in range(2)])
        EventLog(path).extend([make_event("EntityCreated", {"n": 3}) for _ in range(2)])
        assert [e.sequence for e in EventLog(path).read_all()] == [1, 2, 3, 4]

    def test_discontinuity_fails_loudly(self, tmp_path):
        """A record claiming a sequence other than its position is corruption."""
        path = tmp_path / "events.jsonl"
        log = EventLog(path)
        log.extend([make_event("EntityCreated", {"n": i}) for i in range(3)])
        lines = path.read_text(encoding="utf-8").splitlines()
        doctored = json.loads(lines[1])
        doctored["sequence"] = 99  # in-place rewrite simulating tampering
        lines[1] = json.dumps(doctored, ensure_ascii=False, sort_keys=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="sequence discontinuity"):
            log.read_all()


class TestUpcasters:
    def test_legacy_v1_record_upcasts_on_replay(self, tmp_path):
        """A pre-v2 log line (no valid_at/sequence) still replays correctly."""
        path = tmp_path / "events.jsonl"
        legacy = {
            "event_id": "deadbeef" * 4,
            "event_type": "EntityCreated",
            "schema_version": 1,
            "occurred_at": "2026-08-01T00:00:00Z",
            "payload": {"entity_id": "urn:x:legacy", "name": "Legacy"},
        }
        path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        events = EventLog(path).read_all()
        assert len(events) == 1
        assert events[0].schema_version == SCHEMA_VERSION
        assert events[0].valid_at is None
        assert events[0].sequence is None  # falls back to physical position
        assert events[0].payload["entity_id"] == "urn:x:legacy"

    def test_mixed_legacy_and_current_log_replays_in_order(self, tmp_path):
        path = tmp_path / "events.jsonl"
        EventLog(path).append(make_event("EntityCreated", {"n": "current-1"}))
        legacy = {
            "event_id": "cafebabe" * 4,
            "event_type": "LocationObserved",
            "schema_version": 1,
            "occurred_at": "2026-08-01T00:00:00Z",
            "payload": {"entity_id": "urn:x:legacy", "location_uri": "urn:world:location:l"},
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(legacy) + "\n")
        EventLog(path).append(make_event("EntityCreated", {"n": "current-2"}))
        replayed = EventLog(path).read_all()
        assert [e.payload.get("n") for e in replayed] == ["current-1", None, "current-2"]
        assert [e.schema_version for e in replayed] == [2, 2, 2]

    def test_upcasters_table_covers_all_historical_versions(self):
        from foundry.events import UPCASTERS

        # every version below the current one must have a forward path,
        # otherwise old logs silently become unreadable
        assert {1} <= set(UPCASTERS)
        assert SCHEMA_VERSION - 1 in UPCASTERS
