"""Tests for the console admin API (write ops, auth, audit trail — Phase 5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import CORE_ONTOLOGY, SHAPES_FILE

from foundry.console.app import create_app
from foundry.events import EventLog
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline

TOKEN = "secret-admin-token"


def _seed_log(path: Path) -> tuple[str, str]:
    """Two distinct canonical entities (a plausible under-merge pair)."""
    pipeline = IngestionPipeline(
        identity=IdentityService(),
        log=EventLog(path),
        ontology_path=CORE_ONTOLOGY,
        shapes_path=SHAPES_FILE,
    )
    first = pipeline.ingest_entity(
        name="Org A",
        entity_type="Organization",
        external_source="wikidata",
        external_id="Q1",
        source_id="wikidata:Q1",
    )
    second = pipeline.ingest_entity(
        name="Org B",
        entity_type="Organization",
        external_source="wikidata",
        external_id="Q2",
        source_id="wikidata:Q2",
    )
    return first.canonical_id, second.canonical_id


@pytest.fixture()
def client(tmp_path, monkeypatch):
    log_path = tmp_path / "production.jsonl"
    survivor, duplicate = _seed_log(log_path)
    monkeypatch.setenv("FOUNDRY_CONSOLE_TOKEN", TOKEN)
    monkeypatch.setenv("FOUNDRY_ADMIN_LOG", str(log_path))
    monkeypatch.delenv("FOUNDRY_LAKE_ROOT", raising=False)
    from fastapi.testclient import TestClient

    return TestClient(create_app()), tmp_path, survivor, duplicate


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


class TestAdminAuth:
    def test_disabled_without_token_config(self, client, monkeypatch):
        client_under, _tmp, _s, _d = client
        monkeypatch.delenv("FOUNDRY_CONSOLE_TOKEN")
        res = client_under.post("/api/admin/review")
        assert res.status_code == 503

    def test_missing_or_wrong_token_401(self, client):
        _client_under, _tmp, _s, _d = client
        c, _tmp2, _s2, _d2 = client
        assert c.post("/api/admin/review").status_code == 401
        bad = {"Authorization": "Bearer wrong"}
        assert c.post("/api/admin/review", headers=bad).status_code == 401

    def test_correct_token_passes(self, client):
        client_under, _tmp, _s, _d = client
        res = client_under.post("/api/admin/review", headers=_auth())
        assert res.status_code == 200
        assert res.json()["count"] == 0


class TestAdminMergeAudit:
    def test_merge_writes_audited_event_and_lake_row(self, client):
        client_under, tmp, survivor, duplicate = client
        res = client_under.post(
            "/api/admin/merge",
            headers=_auth(),
            json={
                "survivor_id": survivor,
                "duplicate_id": duplicate,
                "reason": "dup qid",
                "actor": "tien",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "merged" and body["actor"] == "tien"
        assert body["lake_rows"] == 1

        # audit trail: EntityMerged on the log with the actor recorded
        log_path = tmp / "production.jsonl"
        events = EventLog(log_path).read_all()
        merged = [e for e in events if e.event_type == "EntityMerged"]
        assert len(merged) == 1
        assert merged[0].payload["actor"] == "tien"

    def test_merge_rejection_is_422_without_writes(self, client):
        client_under, tmp, survivor, _dup = client
        res = client_under.post(
            "/api/admin/merge",
            headers=_auth(),
            json={"survivor_id": survivor, "duplicate_id": survivor, "reason": "self"},
        )
        assert res.status_code == 422
        events = EventLog(tmp / "production.jsonl").read_all()
        assert all(e.event_type != "EntityMerged" for e in events)


class TestAdminSplitAndReview:
    def test_split_undoes_console_merge(self, client):
        _client_under, tmp, survivor, duplicate = client
        c, _tmp2, _s2, _d2 = client
        c.post(
            "/api/admin/merge",
            headers=_auth(),
            json={
                "survivor_id": survivor,
                "duplicate_id": duplicate,
                "reason": "r",
                "actor": "tien",
            },
        )
        res = c.post(
            "/api/admin/split",
            headers=_auth(),
            json={
                "survivor_id": survivor,
                "duplicate_id": duplicate,
                "reason": "undo",
                "actor": "tien",
            },
        )
        assert res.status_code == 200
        events = EventLog(tmp / "production.jsonl").read_all()
        assert [e.event_type for e in events][-1] == "EntitySplit"
        assert events[-1].payload["actor"] == "tien"

    def test_review_queue_lists_queued_facts(self, client, monkeypatch):
        from foundry.merge import rebuild_identity

        client_under, tmp, _survivor, _dup = client
        # a pipeline whose identity is rebuilt from the seeded log (like the
        # admin endpoints do) — otherwise the registry would be empty here
        log_path = tmp / "production.jsonl"
        pipeline = IngestionPipeline(
            identity=rebuild_identity(EventLog(log_path)),
            log=EventLog(log_path),
            ontology_path=CORE_ONTOLOGY,
            shapes_path=SHAPES_FILE,
        )
        # alias collision: one surface name, two same-type entities
        colliding = "urn:world:entity:" + "e" * 32
        pipeline._identity.register(
            entity_id=colliding, entity_type="Organization", aliases=["Org A"]
        )
        pipeline.ingest_location_observation(
            entity_name="Org A",
            entity_type="Organization",
            location_uri="urn:world:location:x",
            valid_from="2026-09-01T00:00:00Z",
            source_ids=["s"],
        )
        res = client_under.post("/api/admin/review", headers=_auth())
        body = res.json()
        assert body["count"] == 1
        assert body["items"][0]["reference_name"] == "Org A"
