"""HTTP contract for the optional safe semantic query route."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from foundry.console.api import semantic
from foundry.console.app import app
from foundry.semantic_query import REPO_ROOT


def test_query_catalog_is_available_without_semantic_data():
    client = TestClient(app)

    response = client.get("/api/v1/semantic/templates")

    assert response.status_code == 200
    assert len(response.json()["templates"]) == 3


def test_query_requires_explicit_semantic_projection(monkeypatch):
    monkeypatch.delenv("FOUNDRY_SEMANTIC_QUERY_DATA", raising=False)
    semantic.reset_cache()
    client = TestClient(app)

    response = client.post(
        "/api/v1/semantic/query",
        json={
            "question": "Where was https://data.example/entity/patrol-03 at 2026-08-05T00:00:00Z?"
        },
    )

    assert response.status_code == 503
    assert "FOUNDRY_SEMANTIC_QUERY_DATA" in response.json()["detail"]


def test_query_runs_registered_template_on_configured_rdf(monkeypatch):
    data = (REPO_ROOT / "benchmarks" / "datasets" / "semantic_workload.ttl").as_posix()
    monkeypatch.setenv("FOUNDRY_SEMANTIC_QUERY_DATA", data)
    semantic.reset_cache()
    client = TestClient(app)

    response = client.post(
        "/api/v1/semantic/query",
        json={
            "question": "Where was https://data.example/entity/patrol-03 at 2026-08-05T00:00:00Z?"
        },
    )
    semantic.reset_cache()

    assert response.status_code == 200
    assert response.json()["template_id"] == "CQ-018"
    assert response.json()["rows"][0][0]["value"] == "Phu Quoc"


def test_query_endpoint_rejects_client_supplied_sparql(monkeypatch):
    monkeypatch.setenv(
        "FOUNDRY_SEMANTIC_QUERY_DATA",
        (REPO_ROOT / "benchmarks" / "datasets" / "semantic_workload.ttl").as_posix(),
    )
    semantic.reset_cache()
    client = TestClient(app)

    response = client.post(
        "/api/v1/semantic/query",
        json={"question": "Where?", "sparql": "DROP ALL"},
    )
    semantic.reset_cache()

    assert response.status_code == 422


def test_provenance_query_obeys_source_scope(monkeypatch):
    monkeypatch.setenv(
        "FOUNDRY_SEMANTIC_QUERY_DATA",
        (REPO_ROOT / "benchmarks" / "datasets" / "semantic_workload.ttl").as_posix(),
    )
    monkeypatch.setenv(
        "FOUNDRY_PROVENANCE_TOKENS",
        json.dumps({"authorized-test-token": ["https://data.example/source/report-2026-08-09"]}),
    )
    semantic.reset_cache()
    client = TestClient(app)
    payload = {"question": "Which sources support https://data.example/entity/assert-17-corrected?"}

    anonymous = client.post("/api/v1/semantic/query", json=payload)
    authorized = client.post(
        "/api/v1/semantic/query",
        json=payload,
        headers={"Authorization": "Bearer authorized-test-token"},
    )
    semantic.reset_cache()

    assert anonymous.status_code == 200
    assert anonymous.json()["rows"] == []
    assert anonymous.json()["redacted_source_count"] == 1
    assert authorized.status_code == 200
    assert authorized.json()["rows"][0][0]["value"] == "Operations report 2026-08-09"
