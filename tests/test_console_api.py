"""Tests for the Ontology Console API (read-only contract)."""

from __future__ import annotations

import json

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
        assert core["version"] == "2.0.0"
        assert {"classes", "properties"} <= set(core.keys())
        for key in ("version_check", "stability", "events", "cq"):
            assert key in data


class TestOntologyEndpoints:
    def test_model_counts_match_kernel(self, client):
        model = client.get("/api/ontology/model").json()
        assert len(model["classes"]) == 28
        assert len(model["properties"]) == 68

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
        core_entries = data["modules"][
            "https://damminhtien.github.io/ontology-research/ontology/core"
        ]
        assert core_entries[0]["version"] == "2.0.0"

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
        assert core["declared_version"] == "2.0.0"
        assert core["latest_version"] == "2.0.0"

    def test_stability_reports_core_breaking_release(self, client):
        """The assertion/event split is a recorded MAJOR release, not a silent edit."""
        data = client.get("/api/stability").json()
        core = next(m for m in data["modules"] if m["module"] == "core")
        assert core["breaking"] >= 1
        expected = round(1 - core["breaking"] / core["releases"], 4)
        assert core["stability"] == expected
        assert core["ok"] is (core["stability"] >= core["threshold"])


class TestImpactEndpoint:
    def test_platform_impact_finds_application_use(self, client):
        data = client.get("/api/impact", params={"term": "Platform"}).json()
        assert data["score"] >= 1
        assert any("foundry" in f for f in data["applications"])

    def test_full_iri_accepted(self, client):
        data = client.get(
            "/api/impact",
            params={
                "term": "https://damminhtien.github.io/ontology-research/ontology/core#Platform"
            },
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

    def test_archived_only_log_remains_visible(self, tmp_path, monkeypatch, client):
        from foundry.console.api import projection
        from foundry.events import EventLog, make_event

        log_path = tmp_path / "events.jsonl"
        EventLog(log_path, max_segment_bytes=1).append(
            make_event(
                "EntityCreated",
                {
                    "entity_id": "urn:x:archived",
                    "entity_type": "Platform",
                    "name": "Archived Vessel",
                    "source_id": "s1",
                    "confidence": 1.0,
                },
            )
        )
        assert not log_path.exists()
        monkeypatch.setenv("FOUNDRY_EVENT_LOG", str(log_path))
        projection.reset_cache()

        stats = client.get("/api/monitor/events/stats").json()
        recent = client.get("/api/monitor/events/recent", params={"limit": 1}).json()
        projected = client.get("/api/projection").json()

        assert stats["exists"] is True and stats["total"] == 1
        assert recent["returned"] == 1 and recent["total"] == 1
        assert projected["exists"] is True and projected["entities"] == 1
        projection.reset_cache()

    def test_validation_conforms(self, client):
        data = client.get("/api/monitor/validation").json()
        assert data["conforms"] is True
        assert data["violation_count"] == 0

    def test_cq_all_pass(self, client):
        data = client.get("/api/monitor/cq").json()
        assert data["total"] == 41  # executable workload contract
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
                    "AssertionMade",
                    {
                        "assertion_id": "urn:assert:test:console-location",
                        "subject_id": "urn:x:patrol-01",
                        "subject_ref": None,
                        "predicate_iri": "https://damminhtien.github.io/ontology-research/ontology/core#locatedAt",
                        "object": {
                            "kind": "location",
                            "value": "https://data.example/entity/loc-cam-ranh",
                        },
                        "valid_from": "2026-08-20T03:00:00Z",
                        "valid_until": None,
                        "source_ids": ["s1", "s2"],
                        "evidence": [
                            {"source_id": "s1", "locator": None, "span": None},
                            {"source_id": "s2", "locator": None, "span": None},
                        ],
                        "confidence": 0.9,
                        "supersedes": None,
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
        assert data["current_location"]["source_ids"] == []

    def test_provenance_token_scopes_current_location(self, seeded_client, monkeypatch):
        monkeypatch.setenv(
            "FOUNDRY_PROVENANCE_TOKENS",
            json.dumps({"reader-token": ["urn:source:s1"], "other-token": ["urn:source:s2"]}),
        )
        response = seeded_client.get(
            "/api/projection/entities/urn:x:patrol-01",
            headers={"Authorization": "Bearer reader-token"},
        )
        assert response.status_code == 200
        assert response.json()["current_location"]["source_ids"] == ["urn:source:s1"]

        other = seeded_client.get(
            "/api/projection/entities/urn:x:patrol-01",
            headers={"Authorization": "Bearer other-token"},
        )
        assert other.json()["current_location"]["source_ids"] == ["urn:source:s2"]

    def test_wildcard_provenance_token_sees_all_sources(self, seeded_client, monkeypatch):
        monkeypatch.setenv("FOUNDRY_PROVENANCE_TOKENS", json.dumps({"reader-token": ["*"]}))
        response = seeded_client.get(
            "/api/projection/entities/urn:x:patrol-01",
            headers={"Authorization": "Bearer reader-token"},
        )
        assert response.json()["current_location"]["source_ids"] == [
            "urn:source:s1",
            "urn:source:s2",
        ]

    def test_provenance_token_scopes_temporal_location(self, seeded_client, monkeypatch):
        monkeypatch.setenv(
            "FOUNDRY_PROVENANCE_TOKENS", json.dumps({"reader-token": ["urn:source:s1"]})
        )
        response = seeded_client.get(
            "/api/projection/entities/urn:x:patrol-01/location/as-of",
            params={"at": "2026-08-21T00:00:00Z"},
            headers={"Authorization": "Bearer reader-token"},
        )
        assert response.status_code == 200
        assert response.json()["location"]["source_ids"] == ["urn:source:s1"]

    def test_invalid_provenance_token_is_rejected(self, seeded_client, monkeypatch):
        monkeypatch.setenv(
            "FOUNDRY_PROVENANCE_TOKENS", json.dumps({"reader-token": ["urn:source:s1"]})
        )
        response = seeded_client.get(
            "/api/projection/entities/urn:x:patrol-01",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401

    def test_projection_resumes_incrementally_from_snapshot(self, seeded_client, tmp_path):
        """B6 follow-up: log growth folds in the suffix, never a full re-replay."""
        from foundry.console.api import projection
        from foundry.events import EventLog, make_event

        seeded_client.get("/api/projection")  # primes the in-process + disk cache
        snapshot, meta = projection._snapshot_paths(projection.event_log_path())
        assert snapshot.exists() and meta.exists()

        # append a new event to the seeded log, then bump its mtime so the
        # in-process cache is invalidated and the resume path must run
        log_path = projection.event_log_path()
        EventLog(log_path).append(
            make_event(
                "EntityCreated",
                {
                    "entity_id": "urn:x:patrol-02",
                    "entity_type": "Platform",
                    "name": "Patrol Vessel 02",
                    "source_id": "s1",
                    "confidence": 1.0,
                },
            )
        )
        stat = log_path.stat()
        import os

        os.utime(log_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        projection.reset_cache()

        data = seeded_client.get("/api/projection").json()
        assert data["entities"] == 2  # suffix applied on top of the snapshot

        # and the hydrate-from-disk path serves the same state after reset
        projection.reset_cache()
        again = seeded_client.get("/api/projection").json()
        assert again["entities"] == 2

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

    def test_recent_events_redact_ungranted_provenance(self, seeded_client, monkeypatch):
        monkeypatch.setenv(
            "FOUNDRY_PROVENANCE_TOKENS", json.dumps({"reader-token": ["urn:source:s1"]})
        )
        anonymous = seeded_client.get("/api/monitor/events/recent", params={"limit": 20})
        anonymous_events = anonymous.json()["events"]
        location_event = next(
            event for event in anonymous_events if event["event_type"] == "AssertionMade"
        )
        entity_event = next(
            event for event in anonymous_events if event["event_type"] == "EntityCreated"
        )
        assert location_event["payload"]["source_ids"] == []
        assert "source_id" not in entity_event["payload"]

        authorized = seeded_client.get(
            "/api/monitor/events/recent",
            params={"limit": 20},
            headers={"Authorization": "Bearer reader-token"},
        )
        visible_location = next(
            event for event in authorized.json()["events"] if event["event_type"] == "AssertionMade"
        )
        assert visible_location["payload"]["source_ids"] == ["urn:source:s1"]

    def test_recent_events_hide_ungranted_document_metadata(self, seeded_client, monkeypatch):
        from foundry.console.api.monitor import event_log_path
        from foundry.events import EventLog, make_event

        EventLog(event_log_path()).append(
            make_event(
                "DocumentRegistered",
                {
                    "document_id": "urn:doc:private",
                    "uri": "https://private.example/report",
                    "title": "Private report",
                    "source_system": "internal",
                    "content_ref": None,
                    "registered_at": "2026-08-20T00:00:00Z",
                },
            )
        )
        monkeypatch.setenv(
            "FOUNDRY_PROVENANCE_TOKENS",
            json.dumps({"reader-token": ["urn:doc:private"], "other-token": ["urn:source:s1"]}),
        )

        anonymous = seeded_client.get("/api/monitor/events/recent", params={"limit": 20}).json()
        assert all(event["event_type"] != "DocumentRegistered" for event in anonymous["events"])

        other_scope = seeded_client.get(
            "/api/monitor/events/recent",
            params={"limit": 20},
            headers={"Authorization": "Bearer other-token"},
        ).json()
        assert all(event["event_type"] != "DocumentRegistered" for event in other_scope["events"])

        authorized = seeded_client.get(
            "/api/monitor/events/recent",
            params={"limit": 20},
            headers={"Authorization": "Bearer reader-token"},
        ).json()
        document = next(
            event for event in authorized["events"] if event["event_type"] == "DocumentRegistered"
        )
        assert document["payload"]["title"] == "Private report"

    def test_malformed_provenance_config_fails_closed(self, seeded_client, monkeypatch):
        monkeypatch.setenv("FOUNDRY_PROVENANCE_TOKENS", "not-json")
        response = seeded_client.get("/api/monitor/events/recent", params={"limit": 20})
        assert response.status_code == 503

    def test_asof_rejects_bad_timestamp(self, seeded_client):
        response = seeded_client.get(
            "/api/projection/entities/urn:x:patrol-01/location/as-of",
            params={"at": "not-a-date"},
        )
        assert response.status_code == 400
