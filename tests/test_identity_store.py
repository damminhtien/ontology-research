"""IdentityStore boundary: registry state swaps without policy changes.

docs/architecture.md §4.5 — ``IdentityService`` owns resolution *policy*;
registry *state* lives behind the ``IdentityStore`` interface so a SQLite or
graph backend can replace the in-memory default without touching resolution
logic. These tests prove the boundary two ways: a recording backend observes
every mutation routed through it, and a full operation sequence produces
identical observable outcomes on the default and a custom backend.
"""

from __future__ import annotations

import pytest

from foundry.events import (
    EVENT_TYPE_ENTITY_CREATED,
    EVENT_TYPE_ENTITY_MERGED,
    EVENT_TYPE_EXTERNAL_ID_BOUND,
    EventLog,
    make_event,
)
from foundry.identity import (
    IdentityService,
    InMemoryIdentityStore,
)
from foundry.merge import rebuild_identity

SURVIVOR = "urn:world:entity:" + "a" * 32
DUPLICATE = "urn:world:entity:" + "b" * 32


class RecordingStore(InMemoryIdentityStore):
    """Spy backend: records every mutation routed through the boundary."""

    def __init__(self) -> None:
        """Start empty with an empty call journal."""
        super().__init__()
        self.calls: list[str] = []

    def upsert(self, entity_id: str, entity_type: str) -> None:
        self.calls.append(f"upsert:{entity_id}")
        super().upsert(entity_id, entity_type)

    def bind_alias(self, entity_id: str, alias: str) -> None:
        self.calls.append(f"bind_alias:{entity_id}")
        super().bind_alias(entity_id, alias)

    def unbind_alias(self, alias: str, entity_id: str) -> None:
        self.calls.append(f"unbind_alias:{entity_id}")
        super().unbind_alias(alias, entity_id)

    def bind_external(self, entity_id: str, source: str, external_id: str) -> None:
        self.calls.append(f"bind_external:{entity_id}:{source}::{external_id}")
        super().bind_external(entity_id, source, external_id)

    def unbind_external(self, source: str, external_id: str, entity_id: str) -> None:
        self.calls.append(f"unbind_external:{entity_id}")
        super().unbind_external(source, external_id, entity_id)

    def detach_bindings(
        self, entity_id: str
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        self.calls.append(f"detach:{entity_id}")
        return super().detach_bindings(entity_id)

    def record_merge(self, duplicate_id: str, survivor_id: str) -> None:
        self.calls.append(f"merge:{duplicate_id}->{survivor_id}")
        super().record_merge(duplicate_id, survivor_id)

    def clear_merge(self, duplicate_id: str) -> None:
        self.calls.append(f"unmerge:{duplicate_id}")
        super().clear_merge(duplicate_id)


def _exercise(svc: IdentityService) -> dict[str, object]:
    """Run a lifecycle covering mint, lookup, merge, third-party reclaim, split."""
    out: dict[str, object] = {}
    a = svc.resolve(name="Alpha Unit", entity_type="Platform").canonical_id
    b = svc.resolve(name="Omega Wing", entity_type="Platform").canonical_id
    out["resolve_external"] = svc.resolve(
        name="Omega Wing Two", entity_type="Platform", external_source="wikidata", external_id="Q99"
    )
    svc.add_external_id(b, "wikidata", "Q2")
    out["lookup_external"] = svc.lookup(external_source="wikidata", external_id="Q2")
    out["lookup_alias"] = svc.lookup(name="alpha unit", entity_type="Platform")
    out["merge"] = svc.merge_entities(a, b)
    # a third party re-claims the external id while b is merged away
    svc.add_external_id(b, "wikidata", "Q2")
    out["split"] = svc.split_entity(
        a, b, out["merge"].moved_aliases, out["merge"].moved_external_ids
    )
    out["len"] = len(svc)
    out["merged_into_b"] = svc.merged_into(b)
    out["identity_a"] = svc.identity(a)
    out["identity_b"] = svc.identity(b)
    out["fuzzy_review"] = svc.resolve(name="Alpha Unitt", entity_type="Platform")
    # same surface name, different type: legitimate multimap collision
    sensor_id = svc.resolve(name="Alpha Unit", entity_type="Sensor").canonical_id
    out["ambiguous"] = svc.lookup(name="Alpha Unit")

    # normalize randomly minted ids to stable role names for comparison
    role_of = {
        a: "alpha",
        b: "omega",
        out["resolve_external"].canonical_id: "omega_ext",
        sensor_id: "sensor",
    }

    def norm_res(res: object) -> tuple[str, str, float, bool, tuple[str, ...]]:
        """Normalize a ``Resolution`` to role names."""
        return (
            role_of.get(res.canonical_id, res.canonical_id),  # type: ignore[attr-defined]
            res.method,  # type: ignore[attr-defined]
            res.confidence,  # type: ignore[attr-defined]
            res.is_new,  # type: ignore[attr-defined]
            tuple(sorted(role_of.get(cid, cid) for cid in res.candidates)),  # type: ignore[attr-defined]
        )

    def norm_lookup(res: object) -> tuple[str, str, tuple[str, ...]]:
        """Normalize a ``LookupResult`` to role names."""
        return (
            role_of.get(res.canonical_id, res.canonical_id),  # type: ignore[attr-defined]
            res.method,  # type: ignore[attr-defined]
            tuple(sorted(role_of.get(cid, cid) for cid in res.candidates)),  # type: ignore[attr-defined]
        )

    out["resolve_external"] = norm_res(out["resolve_external"])
    out["fuzzy_review"] = norm_res(out["fuzzy_review"])
    out["lookup_external"] = norm_lookup(out["lookup_external"])
    out["lookup_alias"] = norm_lookup(out["lookup_alias"])
    out["ambiguous"] = norm_lookup(out["ambiguous"])
    return out


def test_default_backend_is_in_memory():
    svc = IdentityService()
    assert isinstance(svc.store, InMemoryIdentityStore)


def test_minting_routes_through_the_store():
    store = RecordingStore()
    svc = IdentityService(store)
    resolved = svc.resolve(name="USS Gerald R. Ford", entity_type="Platform")
    assert resolved.is_new
    assert f"upsert:{resolved.canonical_id}" in store.calls
    assert f"bind_alias:{resolved.canonical_id}" in store.calls


def test_merge_and_split_route_through_the_store():
    store = RecordingStore()
    svc = IdentityService(store)
    a = svc.resolve(name="Alpha", entity_type="Platform").canonical_id
    b = svc.resolve(name="Beta", entity_type="Platform").canonical_id
    svc.add_external_id(b, "wikidata", "Q1")
    outcome = svc.merge_entities(a, b)
    assert f"merge:{b}->{a}" in store.calls
    assert f"detach:{b}" in store.calls
    assert store.merge_redirects() == {b: a}
    svc.split_entity(a, b, outcome.moved_aliases, outcome.moved_external_ids)
    assert f"unmerge:{b}" in store.calls
    assert store.merge_redirects() == {}


def test_failed_policy_checks_leave_the_store_untouched():
    store = RecordingStore()
    svc = IdentityService(store)
    a = svc.resolve(name="Alpha", entity_type="Platform").canonical_id
    before = list(store.calls)
    with pytest.raises(ValueError, match="type conflict"):
        svc.register(entity_id=a, entity_type="Organization")
    with pytest.raises(ValueError, match="unknown canonical id"):
        svc.merge_entities(a, "urn:world:entity:missing")
    with pytest.raises(ValueError, match="cannot merge"):
        svc.merge_entities(a, a)
    with pytest.raises(ValueError, match="unknown canonical id"):
        svc.add_external_id("urn:world:entity:missing", "wikidata", "Q1")
    assert store.calls == before


def test_custom_backend_produces_identical_outcomes():
    default = _exercise(IdentityService())
    custom = _exercise(IdentityService(RecordingStore()))
    assert default == custom
    # semantics: fuzzy review proposes only the alpha entity; the ambiguous
    # lookup lists both same-name claimants (platform + sensor)
    assert default["fuzzy_review"][4] == ("alpha",)
    assert sorted(default["ambiguous"][2]) == ["alpha", "sensor"]


def test_rebuild_identity_uses_the_in_memory_backend(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append(
        make_event(
            EVENT_TYPE_ENTITY_CREATED,
            {
                "entity_id": SURVIVOR,
                "entity_type": "Platform",
                "name": "Alpha Unit",
                "source_id": "s",
                "confidence": 1.0,
            },
        )
    )
    log.append(
        make_event(
            EVENT_TYPE_ENTITY_CREATED,
            {
                "entity_id": DUPLICATE,
                "entity_type": "Platform",
                "name": "Beta Unit",
                "source_id": "s",
                "confidence": 1.0,
            },
        )
    )
    log.append(
        make_event(
            EVENT_TYPE_EXTERNAL_ID_BOUND,
            {"entity_id": DUPLICATE, "source": "wikidata", "external_id": "Q2"},
        )
    )
    log.append(
        make_event(
            EVENT_TYPE_ENTITY_MERGED,
            {
                "survivor_id": SURVIVOR,
                "duplicate_id": DUPLICATE,
                "moved_aliases": ["Beta Unit"],
                "moved_external_ids": [["wikidata", "Q2"]],
                "reason": "under-merge repair",
            },
        )
    )

    rebuilt = rebuild_identity(log)
    assert isinstance(rebuilt.store, InMemoryIdentityStore)

    # replaying the same mutations through the service lands on the same state
    manual = IdentityService()
    manual.register(entity_id=SURVIVOR, entity_type="Platform", aliases=["Alpha Unit"])
    manual.register(entity_id=DUPLICATE, entity_type="Platform", aliases=["Beta Unit"])
    manual.add_external_id(DUPLICATE, "wikidata", "Q2")
    manual.merge_entities(SURVIVOR, DUPLICATE)
    assert rebuilt.identity(SURVIVOR) == manual.identity(SURVIVOR)
    assert rebuilt.identity(DUPLICATE) == manual.identity(DUPLICATE)
    assert rebuilt.merged_into(DUPLICATE) == SURVIVOR
    assert len(rebuilt) == len(manual)
