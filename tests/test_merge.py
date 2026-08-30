"""Tests for under-merge repair: EntityMerged corrections and projections."""

from __future__ import annotations

from dataclasses import replace

import pytest

from foundry.events import EventLog, make_event
from foundry.identity import IdentityService
from foundry.lake import persist_events
from foundry.merge import merge_entities, rebuild_identity
from foundry.projector import replay_log
from foundry.readmodel import ReadModel, parse_instant

E1 = "urn:world:entity:" + "a" * 32
E2 = "urn:world:entity:" + "b" * 32
LOC_A = "https://data.example/entity/loc-da-nang"
LOC_B = "https://data.example/entity/loc-cam-ranh"


def _undermerged_service() -> tuple[IdentityService, str, str]:
    """Two canonical entities for one real school (two trusted QIDs, ADR-0006).

    Distinct name variants so neither exact alias nor external id hit; the
    trusted QIDs are authoritative, so each resolve creates its own entity
    (the documented under-merge outcome of ADR-0006).
    """
    svc = IdentityService()
    survivor = svc.resolve(
        name="Trường Đại học Bách khoa",
        external_source="wikidata",
        external_id="Q1000001",
        entity_type="Organization",
    ).canonical_id
    duplicate = svc.resolve(
        name="ĐH Bách khoa Hà Nội",
        external_source="wikidata",
        external_id="Q2000002",
        entity_type="Organization",
    ).canonical_id
    assert survivor != duplicate  # under-merge by design
    svc.register(
        entity_id=duplicate,
        entity_type="Organization",
        aliases=["Bach Khoa Hanoi", "Hanoi University of Science and Technology"],
    )
    return svc, survivor, duplicate


class TestIdentityMerge:
    def test_merge_moves_bindings_to_survivor(self):
        svc, survivor, duplicate = _undermerged_service()
        outcome = svc.merge_entities(survivor, duplicate)

        assert "Bach Khoa Hanoi" in outcome.moved_aliases
        assert ("wikidata", "Q2000002") in outcome.moved_external_ids
        _, aliases, external_ids = svc.identity(duplicate)
        assert aliases == frozenset() and external_ids == {}
        resolved = svc.resolve(name="Bach Khoa Hanoi", entity_type="Organization")
        assert resolved.canonical_id == survivor
        assert (
            svc.resolve(
                external_source="wikidata",
                external_id="Q2000002",
                entity_type="Organization",
            ).canonical_id
            == survivor
        )
        assert svc.merged_into(duplicate) == survivor
        assert svc.merged_into(survivor) == ""

    def test_merge_rejects_invalid_inputs(self):
        svc, survivor, duplicate = _undermerged_service()
        with pytest.raises(ValueError, match="into itself"):
            svc.merge_entities(survivor, survivor)
        with pytest.raises(ValueError, match="unknown canonical id"):
            svc.merge_entities(survivor, "urn:world:entity:missing")
        person = svc.resolve(name="Some Person", entity_type="Person").canonical_id
        with pytest.raises(ValueError, match="type conflict"):
            svc.merge_entities(person, duplicate)
        svc.merge_entities(survivor, duplicate)
        with pytest.raises(ValueError, match="already been merged"):
            svc.merge_entities(survivor, duplicate)

    def test_merge_chain_chases_to_final_survivor(self):
        svc, survivor, duplicate = _undermerged_service()
        third = svc.resolve(
            name="Institute of Technology Hanoi",
            external_source="wikidata",
            external_id="Q3000003",
            entity_type="Organization",
        ).canonical_id
        svc.merge_entities(third, duplicate)
        svc.merge_entities(survivor, third)
        assert svc.merged_into(duplicate) == survivor
        assert svc.merged_into(third) == survivor

    def test_fuzzy_index_consistent_after_unbind(self):
        svc, survivor, duplicate = _undermerged_service()
        before = sorted(svc._fuzzy_candidates("bach khoa hanoi"))
        svc.merge_entities(survivor, duplicate)
        after = sorted(svc._fuzzy_candidates("bach khoa hanoi"))
        assert before != []  # sanity: the alias was a candidate before the merge
        assert after == [(survivor, before[0][1])]


class TestMergeEvent:
    def test_merge_appends_single_event_with_full_payload(self, tmp_path):
        svc, survivor, duplicate = _undermerged_service()
        log = EventLog(tmp_path / "events.jsonl")
        result = merge_entities(
            identity=svc,
            log=log,
            survivor_id=survivor,
            duplicate_id=duplicate,
            reason="duplicate QID pair",
        )

        events = log.read_all()
        assert len(events) == 1
        assert events[0].event_type == "EntityMerged"
        assert result.event.event_id == events[0].event_id
        payload = events[0].payload
        assert payload["survivor_id"] == survivor
        assert payload["duplicate_id"] == duplicate
        assert payload["reason"] == "duplicate QID pair"
        assert "Bach Khoa Hanoi" in payload["moved_aliases"]
        assert {"source": "wikidata", "external_id": "Q2000002"} in payload["moved_external_ids"]

    def test_merge_propagates_rejection_without_writing(self, tmp_path):
        svc, survivor, _ = _undermerged_service()
        log = EventLog(tmp_path / "events.jsonl")
        with pytest.raises(ValueError, match="unknown canonical id"):
            merge_entities(
                identity=svc,
                log=log,
                survivor_id=survivor,
                duplicate_id="urn:world:entity:missing",
            )
        assert log.read_all() == []


class TestRebuildIdentity:
    def test_rebuild_restores_aliases_and_merges(self, tmp_path):
        svc, survivor, duplicate = _undermerged_service()
        log = EventLog(tmp_path / "events.jsonl")
        log.append(
            make_event(
                "EntityCreated",
                {
                    "entity_id": survivor,
                    "entity_type": "Organization",
                    "name": "Trường Đại học Bách khoa",
                    "name_aliases": [],
                    "external_ids": [{"source": "wikidata", "external_id": "Q1000001"}],
                    "source_id": "s",
                    "confidence": 1.0,
                },
            )
        )
        log.append(
            make_event(
                "EntityCreated",
                {
                    "entity_id": duplicate,
                    "entity_type": "Organization",
                    "name": "ĐH Bách khoa Hà Nội",
                    "name_aliases": ["Bach Khoa Hanoi"],
                    "external_ids": [{"source": "wikidata", "external_id": "Q2000002"}],
                    "source_id": "s",
                    "confidence": 1.0,
                },
            )
        )
        merge_entities(identity=svc, log=log, survivor_id=survivor, duplicate_id=duplicate)

        rebuilt = rebuild_identity(log)
        resolved = rebuilt.resolve(name="Bach Khoa Hanoi", entity_type="Organization")
        assert resolved.canonical_id == survivor
        assert rebuilt.merged_into(duplicate) == survivor
        with pytest.raises(ValueError, match="already been merged"):
            rebuilt.merge_entities(survivor, duplicate)

    def test_rebuild_restores_external_id_bindings(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        log.append(
            make_event(
                "EntityCreated",
                {
                    "entity_id": E1,
                    "entity_type": "Organization",
                    "name": "Hội Chữ thập đỏ Việt Nam",
                    "name_aliases": ["Red Cross of Viet Nam"],
                    "external_ids": [{"source": "wikidata", "external_id": "Q10832632"}],
                    "source_id": "s",
                    "confidence": 1.0,
                },
            )
        )
        rebuilt = rebuild_identity(log)
        # exact external-id hit after a restart, resolving to the same entity
        resolution = rebuilt.resolve(
            external_source="wikidata",
            external_id="Q10832632",
            entity_type="Organization",
        )
        assert resolution.canonical_id == E1
        assert not resolution.is_new

    def test_rebuild_tolerates_legacy_payloads_without_external_ids(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        log.append(
            make_event(
                "EntityCreated",
                {
                    "entity_id": E1,
                    "entity_type": "Platform",
                    "name": "Legacy Vessel",
                    "name_aliases": [],
                    "source_id": "s",
                    "confidence": 1.0,
                },
            )
        )
        rebuilt = rebuild_identity(log)
        assert rebuilt.resolve(name="Legacy Vessel", entity_type="Platform").canonical_id == E1


def _sequenced_event(seq: int, event_type: str, payload: dict):
    """Event with a deterministic timestamp so replay order is fixed.

    ``make_event`` stamps wall-clock time; events created in the same second
    would otherwise be ordered by their random event ids.
    """
    event = make_event(event_type, payload)
    return replace(event, occurred_at=f"2026-08-30T00:00:{seq:02d}+00:00")


def _append_sequence(log: EventLog, events: list[tuple[str, dict]]) -> None:
    for i, (event_type, payload) in enumerate(events):
        log.append(_sequenced_event(i, event_type, payload))


EC1 = {
    "entity_id": E1,
    "entity_type": "Platform",
    "name": "Vessel Alpha",
    "source_id": "s",
    "confidence": 1.0,
}
EC2 = {
    "entity_id": E2,
    "entity_type": "Platform",
    "name": "Vessel Alpha (dup)",
    "source_id": "s",
    "confidence": 1.0,
}
MERGE_E1_E2 = {
    "survivor_id": E1,
    "duplicate_id": E2,
    "moved_aliases": [],
    "moved_external_ids": [],
    "reason": "duplicate",
}


LOC_A_EVENT = {
    "entity_id": E2,
    "location_uri": LOC_A,
    "valid_from": "2026-07-01T00:00:00Z",
    "source_ids": ["s"],
    "confidence": 0.9,
}
LOC_B_EVENT = {
    "entity_id": E2,
    "location_uri": LOC_B,
    "valid_from": "2026-08-01T00:00:00Z",
    "source_ids": ["s"],
    "confidence": 0.9,
}


class TestProjectorMerge:
    @pytest.fixture()
    def model_with_merge(self, tmp_path) -> ReadModel:
        log = EventLog(tmp_path / "events.jsonl")
        _append_sequence(
            log,
            [
                ("EntityCreated", dict(EC1)),
                ("EntityCreated", dict(EC2)),
                ("LocationObserved", dict(LOC_A_EVENT)),
                ("LocationObserved", dict(LOC_B_EVENT)),
                ("EntityMerged", dict(MERGE_E1_E2)),
            ],
        )
        model, _stats = replay_log(log)
        return model

    def test_duplicate_folded_into_survivor(self, model_with_merge):
        model = model_with_merge
        assert model.get_entity(E2) is None
        assert model.find_by_name("Vessel Alpha (dup)") == []
        assert model.merged_into(E2) == E1
        assert model.merged_into(E1) is None
        # location history moved wholesale; temporal lookups survive the merge
        assert model.current_location(E1).location_uri == LOC_B
        assert model.location_as_of(E1, parse_instant("2026-07-15T00:00:00Z")).location_uri == LOC_A
        assert model.entities_at(LOC_B) == {E1}

    def test_merge_replay_is_deterministic_and_idempotent(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        _append_sequence(
            log,
            [
                ("EntityCreated", dict(EC1)),
                ("EntityCreated", dict(EC2)),
                ("EntityMerged", dict(MERGE_E1_E2)),
            ],
        )
        model_a, stats_a = replay_log(log)
        model_b, stats_b = replay_log(log)
        assert stats_a == stats_b
        for model in (model_a, model_b):
            assert model.get_entity(E2) is None
            assert model.merged_into(E2) == E1
        assert model_a.current_location(E1) == model_b.current_location(E1)


class TestLakeAcceptsMerge:
    def test_persist_merge_event(self, tmp_path):
        event = _sequenced_event(
            0,
            "EntityMerged",
            {
                "survivor_id": E1,
                "duplicate_id": E2,
                "moved_aliases": ["x"],
                "moved_external_ids": [],
                "reason": "r",
            },
        )
        files = persist_events([event], tmp_path / "lake")
        assert sum(f.rows for f in files) == 1
