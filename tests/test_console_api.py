"""Tests for the Ontology Console API (read-only contract)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from foundry.console.app import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


class TestShellAndOverview:
    def test_index_serves_spa_shell(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Ontology Console" in response.text

    def test_overview_aggregates(self, client):
        data = client.get("/api/overview").json()
        assert data["modules"], "expected at least the core module"
        core = next(m for m in data["modules"] if m["name"] == "core")
        assert core["version"] == "0.1.0"
        assert {"classes", "properties"} <= set(core.keys())
        for key in ("version_check", "stability", "events", "cq"):
            assert key in data


class TestOntologyEndpoints:
    def test_model_counts_match_kernel(self, client):
        model = client.get("/api/ontology/model").json()
        assert len(model["classes"]) == 20
        assert len(model["properties"]) == 33

    def test_tree_roots_at_entity(self, client):
        tree = client.get("/api/ontology/tree").json()["tree"]
        root_names = {node["name"] for node in tree}
        assert "Entity" in root_names

    def test_search_finds_platform(self, client):
        results = client.get("/api/ontology/search", params={"q": "platform"}).json()
        names = {c["name"] for c in results["classes"]}
        assert "Platform" in names

    def test_search_requires_query(self, client):
        assert client.get("/api/ontology/search").status_code == 422

    def test_term_detail_class(self, client):
        detail = client.get("/api/ontology/terms/Organization").json()
        assert detail["kind"] == "class"
        prop_names = {p["name"] for p in detail["properties"]}
        assert "operates" in prop_names
        assert {"modules", "queries", "applications"} <= set(detail["blast_radius"])

    def test_unknown_term_is_404(self, client):
        assert client.get("/api/ontology/terms/DoesNotExist").status_code == 404


class TestRegistryEndpoints:
    def test_releases_grouped_by_module(self, client):
        data = client.get("/api/releases").json()
        assert data["total"] >= 1
        core_entries = data["modules"]["https://ontology.example/core"]
        assert core_entries[0]["version"] == "0.1.0"

    def test_pending_with_no_changes_is_clean(self, client):
        data = client.get("/api/releases/core/pending").json()
        assert data["has_baseline"] is True
        assert data["changes"] == []
        assert data["severity"] == "NONE"
        assert data["suggested_version"] is None

    def test_diff_between_missing_versions_is_404(self, client):
        response = client.get(
            "/api/releases/core/diff",
            params={"from_version": "9.0.0", "to_version": "9.0.1"},
        )
        assert response.status_code == 404

    def test_versions_check_passes_on_clean_tree(self, client):
        data = client.get("/api/versions/check").json()
        assert data["passed"] is True
        core = next(m for m in data["modules"] if m["module"] == "core")
        assert core["declared_version"] == "0.1.0"
        assert core["latest_version"] == "0.1.0"

    def test_stability_core_meets_threshold(self, client):
        data = client.get("/api/stability").json()
        core = next(m for m in data["modules"] if m["module"] == "core")
        assert core["ok"] is True and core["stability"] == 1.0


class TestImpactEndpoint:
    def test_platform_impact_finds_application_use(self, client):
        data = client.get("/api/impact", params={"term": "Platform"}).json()
        assert data["score"] >= 1
        assert any("foundry" in f for f in data["applications"])

    def test_full_iri_accepted(self, client):
        data = client.get(
            "/api/impact",
            params={"term": "https://ontology.example/core#Platform"},
        ).json()
        assert data["term"] == "Platform"


class TestMonitorEndpoints:
    def test_event_stats_when_log_missing(self, tmp_path, monkeypatch, client):
        monkeypatch.setenv("FOUNDRY_EVENT_LOG", str(tmp_path / "none.jsonl"))
        data = client.get("/api/monitor/events/stats").json()
        assert data["exists"] is False and data["total"] == 0

    def test_event_stats_and_recent(self, tmp_path, monkeypatch, client):
        from foundry.events import EventLog, make_event

        log_path = tmp_path / "events.jsonl"
        log = EventLog(log_path)
        log.append(make_event("EntityCreated", {"entity_id": "urn:x:1"}))
        log.append(make_event("EntityCreated", {"entity_id": "urn:x:2"}))
        monkeypatch.setenv("FOUNDRY_EVENT_LOG", str(log_path))

        stats = client.get("/api/monitor/events/stats").json()
        assert stats["total"] == 2 and stats["by_type"]["EntityCreated"] == 2

        recent = client.get("/api/monitor/events/recent", params={"limit": 1}).json()
        assert recent["returned"] == 1

    def test_validation_conforms(self, client):
        data = client.get("/api/monitor/validation").json()
        assert data["conforms"] is True
        assert data["violation_count"] == 0

    def test_cq_all_pass(self, client):
        data = client.get("/api/monitor/cq").json()
        assert data["total"] == 6
        assert data["failed"] == 0


class TestProjectionEndpoints:
    """Read-model API: point lookup and temporal queries without SPARQL."""

    @pytest.fixture()
    def seeded_client(self, tmp_path, monkeypatch):
        from foundry.console.api import projection
        from foundry.events import EventLog, make_event

        log_path = tmp_path / "events.jsonl"
        log = EventLog(log_path)
        log.extend(
            [
                make_event(
                    "EntityCreated",
                    {
                        "entity_id": "urn:x:patrol-01",
                        "entity_type": "Platform",
                        "name": "Patrol Vessel 01",
                        "source_id": "s1",
                        "confidence": 1.0,
                    },
                ),
                make_event(
                    "LocationObserved",
                    {
                        "entity_id": "urn:x:patrol-01",
                        "location_uri": "https://data.example/entity/loc-cam-ranh",
                        "valid_from": "2026-08-20T03:00:00Z",
                        "source_ids": ["s1"],
                        "confidence": 0.9,
                    },
                ),
            ]
        )
        monkeypatch.setenv("FOUNDRY_EVENT_LOG", str(log_path))
        projection.reset_cache()
        return TestClient(app)

    def test_projection_stats(self, seeded_client):
        data = seeded_client.get("/api/projection").json()
        assert data["exists"] is True
        assert data["entities"] == 1
        assert data["with_location"] == 1
        assert data["last_event_time"] is not None

    def test_entity_by_id_with_current_location(self, seeded_client):
        data = seeded_client.get("/api/projection/entities/urn:x:patrol-01").json()
        assert data["entity"]["name"] == "Patrol Vessel 01"
        assert data["current_location"]["location_uri"].endswith("loc-cam-ranh")

    def test_unknown_entity_404(self, seeded_client):
        assert seeded_client.get("/api/projection/entities/urn:x:missing").status_code == 404

    def test_lookup_by_name(self, seeded_client):
        data = seeded_client.get(
            "/api/projection/lookup", params={"name": "patrol vessel 01"}
        ).json()
        assert data["matches"][0]["entity_id"] == "urn:x:patrol-01"

    def test_temporal_asof(self, seeded_client):
        data = seeded_client.get(
            "/api/projection/entities/urn:x:patrol-01/location/as-of",
            params={"at": "2026-08-21T00:00:00Z"},
        ).json()
        assert data["location"]["location_uri"].endswith("loc-cam-ranh")

        before = seeded_client.get(
            "/api/projection/entities/urn:x:patrol-01/location/as-of",
            params={"at": "2026-01-01T00:00:00Z"},
        ).json()
        assert before["location"] is None

    def test_asof_rejects_bad_timestamp(self, seeded_client):
        response = seeded_client.get(
            "/api/projection/entities/urn:x:patrol-01/location/as-of",
            params={"at": "not-a-date"},
        )
        assert response.status_code == 400
