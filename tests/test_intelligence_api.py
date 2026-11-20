"""Regression coverage for analyst-facing OSINT resources."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from foundry.console.api import intelligence, projection
from foundry.console.app import app
from foundry.events import EventLog, make_event

OPERATED_BY = "https://example.test/vocab/operatedBy"


@pytest.fixture()
def seeded_client(tmp_path, monkeypatch):
    """Create a small synthetic identity and assertion history."""
    log_path = tmp_path / "intelligence.jsonl"
    log = EventLog(log_path)
    entities = [
        ("urn:test:vessel-alpha-a", "Platform", "Vessel Alpha", "111000111"),
        ("urn:test:vessel-alpha-b", "Platform", "Vessel Alpha", "222000222"),
        ("urn:test:operator-one", "Organization", "Example Operator One", ""),
        ("urn:test:operator-two", "Organization", "Example Operator Two", ""),
    ]
    events = [
        make_event(
            "EntityCreated",
            {
                "entity_id": entity_id,
                "entity_type": entity_type,
                "name": name,
                "name_aliases": ["Alpha"],
                "external_ids": (
                    [{"source": "ais-mmsi", "external_id": external_id}] if external_id else []
                ),
            },
        )
        for entity_id, entity_type, name, external_id in entities
    ]
    events.extend(
        [
            make_event(
                "AssertionMade",
                {
                    "assertion_id": "urn:test:assertion:operator-old",
                    "subject_id": "urn:test:vessel-alpha-a",
                    "predicate_iri": OPERATED_BY,
                    "object": {"kind": "entity", "value": "urn:test:operator-one"},
                    "valid_from": "2025-01-01T00:00:00Z",
                    "recorded_at": "2025-01-02T00:00:00Z",
                    "source_ids": ["source-a"],
                    "evidence": [
                        {
                            "source_id": "source-a",
                            "locator": "https://example.test/report/1#page=2",
                            "span": {
                                "start": 31,
                                "end": 51,
                                "exact_text": "Example Operator One",
                            },
                        }
                    ],
                },
            ),
            make_event(
                "AssertionMade",
                {
                    "assertion_id": "urn:test:assertion:operator-current",
                    "subject_id": "urn:test:vessel-alpha-a",
                    "predicate_iri": OPERATED_BY,
                    "object": {"kind": "entity", "value": "urn:test:operator-one"},
                    "valid_from": "2026-01-01T00:00:00Z",
                    "recorded_at": "2026-01-02T00:00:00Z",
                    "source_ids": ["source-a"],
                    "evidence": [
                        {
                            "source_id": "source-a",
                            "locator": "https://example.test/report/2#page=1",
                            "span": {
                                "start": 12,
                                "end": 28,
                                "exact_text": "Operator One Ltd",
                            },
                        }
                    ],
                    "supersedes": "urn:test:assertion:operator-old",
                },
            ),
            make_event(
                "AssertionMade",
                {
                    "assertion_id": "urn:test:assertion:operator-conflict",
                    "subject_id": "urn:test:vessel-alpha-a",
                    "predicate_iri": OPERATED_BY,
                    "object": {"kind": "entity", "value": "urn:test:operator-two"},
                    "valid_from": "2026-01-01T00:00:00Z",
                    "recorded_at": "2026-01-02T00:00:00Z",
                    "source_ids": ["source-b"],
                    "evidence": [
                        {
                            "source_id": "source-b",
                            "locator": "https://example.test/news/3",
                            "span": {
                                "start": 0,
                                "end": 26,
                                "exact_text": "Operator Two manages Alpha",
                            },
                        }
                    ],
                },
            ),
            make_event(
                "EntityCannotLinkAsserted",
                {
                    "left_id": "urn:test:vessel-alpha-a",
                    "right_id": "urn:test:vessel-alpha-b",
                    "evidence": {
                        "evidence_id": "urn:test:evidence:separate-vessels",
                        "source": "urn:source:source-a",
                        "basis": "different-hull-numbers",
                        "recorded_by": "fixture",
                        "description": "Synthetic cannot-link fixture.",
                    },
                },
            ),
        ]
    )
    log.extend(events)
    monkeypatch.setenv("FOUNDRY_EVENT_LOG", str(log_path))
    monkeypatch.delenv("FOUNDRY_DATABASE_URL", raising=False)
    monkeypatch.delenv("FOUNDRY_PROVENANCE_TOKENS", raising=False)
    projection.reset_cache()
    intelligence._identity_cache = None
    intelligence._identity_cache_key = None
    try:
        yield TestClient(app), log
    finally:
        projection.reset_cache()
        intelligence._identity_cache = None
        intelligence._identity_cache_key = None


def test_entity_search_and_profile_show_distinct_same_name_entities(seeded_client):
    client, _ = seeded_client
    shell = client.get("/").text
    assert 'data-view="intelligence"' in shell

    result = client.get("/api/v1/entities", params={"q": "Vessel Alpha"}).json()

    assert {item["entity_id"] for item in result["entities"]} == {
        "urn:test:vessel-alpha-a",
        "urn:test:vessel-alpha-b",
    }
    assert result["total"] == 2

    profile = client.get("/api/v1/entities/urn:test:vessel-alpha-a").json()["entity"]
    assert profile["name"] == "Vessel Alpha"
    assert profile["aliases"] == ["Alpha", "Vessel Alpha"]
    assert profile["external_ids"] == {"ais-mmsi": ["111000111"]}

    identifiers = client.get("/api/v1/entities/urn:test:vessel-alpha-b/identifiers").json()
    assert identifiers["external_ids"][0]["values"] == ["222000222"]
    assert identifiers["external_ids"][0]["unique_authoritative"] is False


def test_relations_timeline_and_conflicts_are_time_aware(seeded_client):
    client, _ = seeded_client
    at = "2026-07-01T00:00:00Z"
    relations = client.get(
        "/api/v1/entities/urn:test:vessel-alpha-a/relations",
        params={"at": at, "depth": 1, "limit": 10},
    ).json()
    assert {item["object"]["value"] for item in relations["relations"]} == {
        "urn:test:operator-one",
        "urn:test:operator-two",
    }
    traversed = client.get(
        "/api/v1/entities/urn:test:vessel-alpha-a/relations",
        params={"at": at, "depth": 2, "limit": 10},
    ).json()
    assert traversed["depth"] == 2
    assert all(len(path["nodes"]) <= 3 for path in traversed["paths"])

    timeline = client.get(
        "/api/v1/entities/urn:test:vessel-alpha-a/timeline",
        params={"include_superseded": "true", "limit": 10},
    ).json()
    assert [item["assertion_id"] for item in timeline["assertions"][:2]] == [
        "urn:test:assertion:operator-current",
        "urn:test:assertion:operator-conflict",
    ]
    old = next(
        item for item in timeline["assertions"] if item["assertion_id"].endswith("operator-old")
    )
    assert old["status"] == "superseded"
    assert old["valid_from"] == "2025-01-01T00:00:00Z"
    assert old["recorded_at"] == "2025-01-02T00:00:00Z"

    conflicts = client.get(
        "/api/v1/entities/urn:test:vessel-alpha-a/conflicts", params={"at": at}
    ).json()
    assert len(conflicts["conflicts"]) == 1
    assert conflicts["conflicts"][0]["predicate_iri"] == OPERATED_BY
    assert conflicts["conflicts"][0]["review_status"] == "unreviewed"
    assert len(conflicts["conflicts"][0]["alternatives"]) == 2


def test_evidence_scope_and_identity_review_do_not_write(seeded_client, monkeypatch):
    client, log = seeded_client
    before = log.last_sequence
    monkeypatch.setenv(
        "FOUNDRY_PROVENANCE_TOKENS",
        json.dumps({"review-token": ["urn:source:source-a"]}),
    )
    evidence = client.get(
        "/api/v1/assertions/urn:test:assertion:operator-current/evidence",
        headers={"Authorization": "Bearer review-token"},
    ).json()
    assert evidence["evidence"][0]["span"]["exact_text"] == "Operator One Ltd"

    hidden = client.get("/api/v1/assertions/urn:test:assertion:operator-current/evidence").json()
    assert hidden["evidence"] == []
    assert hidden["redacted_evidence_count"] == 1

    candidate = client.post(
        "/api/v1/identity/candidates",
        json={
            "name": "Vessel Alpha",
            "entity_type": "Platform",
            "external_namespace": "ais-mmsi",
            "external_id": "333000333",
        },
    ).json()
    assert candidate["decision"] == "REVIEW"
    assert candidate["candidate_score_semantics"].endswith(
        "not a probability that the facts are true"
    )
    assert {item["entity_id"] for item in candidate["candidates"]} == {
        "urn:test:vessel-alpha-a",
        "urn:test:vessel-alpha-b",
    }
    assert candidate["writes_performed"] is False

    pair = client.post(
        "/api/v1/identity/candidates",
        json={
            "entity_id": "urn:test:vessel-alpha-a",
            "candidate_entity_id": "urn:test:vessel-alpha-b",
        },
    ).json()
    assert pair["decision"] == "CONFLICT"
    assert pair["cannot_link_evidence"] is None
    assert pair["cannot_link_evidence_redacted"] is True

    pair_authorized = client.post(
        "/api/v1/identity/candidates",
        headers={"Authorization": "Bearer review-token"},
        json={
            "entity_id": "urn:test:vessel-alpha-a",
            "candidate_entity_id": "urn:test:vessel-alpha-b",
        },
    ).json()
    assert pair_authorized["cannot_link_evidence"]["basis"] == "different-hull-numbers"
    assert pair_authorized["cannot_link_evidence_redacted"] is False
    assert log.last_sequence == before


def test_query_bounds_and_unknown_resources(seeded_client):
    client, _ = seeded_client
    assert (
        client.get(
            "/api/v1/entities/urn:test:vessel-alpha-a/relations", params={"depth": 4}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/entities/urn:test:vessel-alpha-a/timeline", params={"limit": 101}
        ).status_code
        == 422
    )
    assert client.get("/api/v1/assertions/urn:test:missing/evidence").status_code == 404
    assert client.get("/api/v1/entities/urn:test:missing/timeline").status_code == 404
    assert client.post("/api/v1/identity/candidates", json={"name": "x"}).status_code == 422
