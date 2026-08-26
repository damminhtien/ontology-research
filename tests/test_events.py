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
            event_from_dict({"event_id": "abc"})

    def test_from_dict_rejects_wrong_schema_version(self):
        record = event_to_dict(make_event("EntityCreated", {}))
        record["schema_version"] = 99
        with pytest.raises(ValueError, match="unsupported schema version"):
            event_from_dict(record)


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
        with pytest.raises(ValueError, match="missing field"):
            EventLog(path).read_all()

    def test_log_is_append_only_by_contract(self, tmp_path):
        """The API surface must not expose update/delete operations."""
        public_api = {name for name in dir(EventLog) if not name.startswith("_")}
        forbidden = {"update", "delete", "truncate", "rewrite", "remove", "pop"}
        assert not public_api & forbidden
