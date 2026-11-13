"""Atomicity and rebuild contracts for the optional Oxigraph store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pyoxigraph")

from foundry import oxigraph_snapshot
from foundry.assertions import make_assertion
from foundry.events import EventLog, make_event
from foundry.oxigraph_snapshot import (
    build_snapshot,
    current_snapshot,
    open_current_store,
)

CORE = "https://damminhtien.github.io/ontology-research/ontology/core#"
ASSERTION = "https://damminhtien.github.io/ontology-research/ontology/middle/assertion#"
ENTITY = "urn:test:oxigraph-snapshot:entity"


def _event_log(path: Path) -> EventLog:
    """Create a source log with entity identity and blank-node evidence."""
    log = EventLog(path)
    log.append(
        make_event(
            "EntityCreated",
            {
                "entity_id": ENTITY,
                "entity_type": "Organization",
                "name": "Snapshot Organization",
            },
        )
    )
    make_assertion(
        log=log,
        subject_id=ENTITY,
        predicate_iri=f"{CORE}name",
        object_kind="literal",
        object_value="Snapshot Organization",
        valid_from="2026-09-01T00:00:00Z",
        source_ids=["https://data.example/source/snapshot"],
        evidence=[
            {
                "source_id": "https://data.example/source/snapshot",
                "locator": "page=1",
                "span": {"start": 2, "end": 9, "unit": "character"},
            }
        ],
    )
    return log


def _name_rows(snapshot_root: Path) -> list[str]:
    """Read the projected entity name from the current read-only store."""
    store = open_current_store(snapshot_root)
    result = store.query(f"SELECT ?name WHERE {{ <{ENTITY}> <{CORE}name> ?name }}")
    return [row[0].value for row in result]


def test_snapshot_manifest_reopen_and_full_rebuild_are_consistent(tmp_path: Path):
    """A successful build records source metadata and survives reopening/rebuild."""
    log_path = tmp_path / "events.jsonl"
    _event_log(log_path)
    snapshot_root = tmp_path / "snapshots"

    first = build_snapshot(log_path, snapshot_root)
    first_manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    assert current_snapshot(snapshot_root) == first
    assert first_manifest["source"]["event_count"] == 2
    assert first_manifest["source"]["last_sequence"] == 2
    assert first_manifest["source"]["sha256"]
    assert first_manifest["projection"]["triple_count"] > 0
    assert _name_rows(snapshot_root) == ["Snapshot Organization"]
    evidence_store = open_current_store(snapshot_root)
    evidence = evidence_store.query(
        "SELECT ?start ?end WHERE { "
        f"?assertion <{ASSERTION}evidence> ?item . "
        f"?item <{ASSERTION}spanStart> ?start ; <{ASSERTION}spanEnd> ?end }}"
    )
    assert [(row[0].value, row[1].value) for row in evidence] == [("2", "9")]

    second = build_snapshot(log_path, snapshot_root)
    assert second != first
    assert current_snapshot(snapshot_root) == second
    assert _name_rows(snapshot_root) == ["Snapshot Organization"]


def test_failed_rebuild_keeps_previous_snapshot_current(tmp_path: Path, monkeypatch):
    """An interrupted projection cannot replace the last published snapshot."""
    log_path = tmp_path / "events.jsonl"
    _event_log(log_path)
    snapshot_root = tmp_path / "snapshots"
    previous = build_snapshot(log_path, snapshot_root)

    def fail_projection(*_args, **_kwargs):
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(oxigraph_snapshot, "iter_projected_quads", fail_projection)
    with pytest.raises(RuntimeError, match="injected projection failure"):
        build_snapshot(log_path, snapshot_root)

    assert current_snapshot(snapshot_root) == previous
    assert _name_rows(snapshot_root) == ["Snapshot Organization"]
    assert not list(snapshot_root.glob(".candidate-*"))


def test_source_change_during_projection_discards_candidate(tmp_path: Path, monkeypatch):
    """A source append between projection passes does not publish a stale build."""
    log_path = tmp_path / "events.jsonl"
    log = _event_log(log_path)
    snapshot_root = tmp_path / "snapshots"
    previous = build_snapshot(log_path, snapshot_root)
    original = oxigraph_snapshot.iter_projected_quads

    def append_after_projection(*args, **kwargs):
        yield from original(*args, **kwargs)
        log.append(
            make_event(
                "EntityCreated",
                {
                    "entity_id": "urn:test:oxigraph-snapshot:later",
                    "entity_type": "Organization",
                    "name": "Later Organization",
                },
            )
        )

    monkeypatch.setattr(oxigraph_snapshot, "iter_projected_quads", append_after_projection)
    with pytest.raises(RuntimeError, match="event log changed"):
        build_snapshot(log_path, snapshot_root)

    assert current_snapshot(snapshot_root) == previous
    assert _name_rows(snapshot_root) == ["Snapshot Organization"]
