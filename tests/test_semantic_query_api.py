"""HTTP contract for the optional safe semantic query route."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from foundry.console.api import semantic
from foundry.console.app import create_app
from foundry.semantic_query import (
    DEFAULT_OLLAMA_MODEL,
    REPO_ROOT,
    KeywordIntentSelector,
    LlmIntentSelector,
    OllamaChatCompletion,
)


def _client() -> TestClient:
    return TestClient(create_app(semantic_intent_selector=KeywordIntentSelector()))


def test_console_defaults_to_local_qwen_selector(monkeypatch):
    for name in (
        "FOUNDRY_SEMANTIC_QUERY_MODEL",
        "OLLAMA_MODEL",
        "FOUNDRY_OLLAMA_BASE_URL",
        "OLLAMA_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    app = create_app()
    selector = app.state.semantic_intent_selector

    assert isinstance(selector, LlmIntentSelector)
    assert isinstance(selector._complete, OllamaChatCompletion)
    assert selector._complete.model == DEFAULT_OLLAMA_MODEL
    assert selector._complete.base_url == "http://127.0.0.1:11434"


def test_query_catalog_is_available_without_semantic_data():
    client = _client()

    response = client.get("/api/v1/semantic/templates")

    assert response.status_code == 200
    assert len(response.json()["templates"]) == 3


def test_query_requires_explicit_semantic_projection(monkeypatch):
    monkeypatch.delenv("FOUNDRY_SEMANTIC_QUERY_DATA", raising=False)
    semantic.reset_cache()
    client = _client()

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
    client = _client()

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
    client = _client()

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
    client = _client()
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
