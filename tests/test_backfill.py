"""Tests for the external-id backfill tool (pre-v2 log migration)."""

from __future__ import annotations

import sys

import backfill_external_ids as bf

from foundry.events import EventLog
from foundry.merge import rebuild_identity


def _entity_event(entity_id: str, name: str, source_id: str) -> dict:
    return {
        "entity_id": entity_id,
        "entity_type": "Organization",
        "name": name,
        "name_aliases": [],
        "source_id": source_id,
        "confidence": 1.0,
    }


class TestCollectMissingBindings:
    def test_derives_bindings_from_source_id_convention(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        log.append(
            bf.make_event("EntityCreated", _entity_event("urn:x:1", "Org A", "wikidata:Q100"))
        )
        log.append(
            bf.make_event("EntityCreated", _entity_event("urn:x:2", "Org B", "wikidata:Q200"))
        )
        log.append(
            bf.make_event(
                "EntityCreated",
                _entity_event("urn:x:3", "Seed Org", "https://data.example/source/seed"),
            )
        )

        events = bf.collect_missing_binding_events(log)
        assert len(events) == 2
        assert {e.payload["external_id"] for e in events} == {"Q100", "Q200"}
        assert {e.payload["entity_id"] for e in events} == {"urn:x:1", "urn:x:2"}

    def test_skips_already_bound_ids(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        # explicit payload binding, as written by the current pipeline
        payload = _entity_event("urn:x:1", "Org A", "wikidata:Q100")
        payload["external_ids"] = [{"source": "wikidata", "external_id": "Q100"}]
        log.append(bf.make_event("EntityCreated", payload))

        assert bf.collect_missing_binding_events(log) == []

    def test_backfill_is_idempotent_after_append(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        log = EventLog(log_path)
        log.append(
            bf.make_event("EntityCreated", _entity_event("urn:x:1", "Org A", "wikidata:Q100"))
        )

        first = bf.collect_missing_binding_events(log)
        assert len(first) == 1
        log.extend(first)
        assert bf.collect_missing_binding_events(EventLog(log_path)) == []

        # and the rebuilt registry resolves the QID after the restart
        rebuilt = rebuild_identity(EventLog(log_path))
        hit = rebuilt.lookup(
            external_source="wikidata", external_id="Q100", entity_type="Organization"
        )
        assert hit.canonical_id == "urn:x:1"


class TestMain:
    def test_main_appends_and_reports(self, tmp_path, capsys):
        log_path = tmp_path / "events.jsonl"
        log = EventLog(log_path)
        log.append(
            bf.make_event("EntityCreated", _entity_event("urn:x:1", "Org A", "wikidata:Q100"))
        )

        argv = ["--log", str(log_path), "--no-lake"]
        original = sys.argv
        sys.argv = ["backfill_external_ids.py", *argv]
        try:
            assert bf.main() == 0
        finally:
            sys.argv = original
        out = capsys.readouterr().out
        assert "1 missing binding(s)" in out
        assert len(EventLog(log_path).read_all()) == 2

    def test_main_missing_log_fails(self, tmp_path, capsys):
        original = sys.argv
        sys.argv = ["backfill_external_ids.py", "--log", str(tmp_path / "nope.jsonl")]
        try:
            assert bf.main() == 1
        finally:
            sys.argv = original
