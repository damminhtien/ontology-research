"""Backend-independent operational query contracts and safety limits."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from foundry.namespaces import CORE_LOCATED_AT
from foundry.query_service import QueryService
from foundry.readmodel import ReadModel

AT = datetime(2026, 8, 21, tzinfo=UTC)
RELATION = "urn:relation:connectedTo"


@pytest.fixture()
def query_service() -> QueryService:
    model = ReadModel()
    for entity_id, name, aliases in (
        ("urn:entity:a", "Alpha", ["A One"]),
        ("urn:entity:b", "Bravo", ["A One"]),
        ("urn:entity:c", "Charlie", []),
        ("urn:entity:d", "Delta", []),
    ):
        model.upsert_entity(entity_id, "Platform", name, AT, aliases)

    def assertion(
        assertion_id: str,
        subject: str,
        predicate: str,
        kind: str,
        value: str,
        valid_until: str | None = None,
    ):
        model.add_assertion(
            {
                "assertion_id": assertion_id,
                "subject_id": subject,
                "predicate_iri": predicate,
                "object": {"kind": kind, "value": value},
                "valid_from": "2026-08-01T00:00:00Z",
                "valid_until": valid_until,
                "recorded_at": "2026-08-02T00:00:00Z",
                "source_ids": ["urn:source:fixture"],
                "evidence": [
                    {
                        "source_id": "urn:source:fixture",
                        "locator": "page 4",
                        "span": {"start": 2, "end": 8, "unit": "char", "exact_text": "evidence"},
                    }
                ],
                "evidence_quality": "primary",
                "status": "asserted",
            }
        )

    assertion("urn:assert:a-b", "urn:entity:a", RELATION, "entity", "urn:entity:b")
    assertion("urn:assert:a-d", "urn:entity:a", RELATION, "entity", "urn:entity:d")
    assertion(
        "urn:assert:a-expired",
        "urn:entity:a",
        RELATION,
        "entity",
        "urn:entity:c",
        "2026-08-21T00:00:00Z",
    )
    assertion("urn:assert:b-c", "urn:entity:b", RELATION, "entity", "urn:entity:c")
    assertion(
        "urn:assert:a-location",
        "urn:entity:a",
        CORE_LOCATED_AT,
        "location",
        "urn:location:1",
    )
    model.add_track(
        {
            "track_id": "urn:track:a-1",
            "entity_id": "urn:entity:a",
            "observation_ids": ["urn:observation:a-1"],
            "source_ids": ["urn:source:fixture"],
        }
    )
    return QueryService(model)


def test_entity_lookup_alias_pagination_and_sorting(query_service):
    page = query_service.entity_search("a one", limit=1, sort_by="name")

    assert [entity.entity_id for entity in page.items] == ["urn:entity:a"]
    assert page.total == 2
    assert page.has_more is True

    second_alias_page = query_service.entity_search("a one", limit=1, offset=1, sort_by="name")
    assert [entity.entity_id for entity in second_alias_page.items] == ["urn:entity:b"]
    descending = query_service.entity_search("a one", limit=1, sort_by="name", descending=True)
    assert [entity.entity_id for entity in descending.items] == ["urn:entity:b"]

    second_page = query_service.entity_search("alpha", offset=1)
    assert second_page.items == ()
    assert second_page.total == 1


def test_as_of_relationships_and_bounded_traversal(query_service):
    direct = query_service.relationships_as_of("urn:entity:a", AT, predicate_iri=RELATION)
    assert {row["object"]["value"] for row in direct.items} == {
        "urn:entity:b",
        "urn:entity:d",
    }

    paths = query_service.traverse_relationships(
        "urn:entity:a", AT, max_hops=2, predicate_iri=RELATION
    )
    assert any(
        path.nodes == ("urn:entity:a", "urn:entity:b", "urn:entity:c") for path in paths.items
    )
    assert all(path.hops <= 2 for path in paths.items)


def test_provenance_contradictions_and_location_occupancy(query_service):
    provenance = query_service.assertion_provenance("urn:assert:a-b")
    assert provenance["source_ids"] == ["urn:source:fixture"]
    assert provenance["evidence"][0]["span"]["exact_text"] == "evidence"

    conflict = query_service.contradictions("urn:entity:a", RELATION, AT)
    assert conflict.total == 1
    assert {row["object"]["value"] for row in conflict.items[0]["alternatives"]} == {
        "urn:entity:b",
        "urn:entity:d",
    }

    occupied = query_service.entities_at_location("urn:location:1", AT)
    assert occupied.items == ("urn:entity:a",)


def test_track_provenance_and_entity_track_pages(query_service):
    track = query_service.track_provenance("urn:track:a-1")
    assert track["observation_ids"] == ["urn:observation:a-1"]
    assert track["source_ids"] == ["urn:source:fixture"]

    page = query_service.tracks_for_entity("urn:entity:a")
    assert [item["track_id"] for item in page.items] == ["urn:track:a-1"]


def test_query_bounds_reject_unbounded_or_ambiguous_requests(query_service):
    with pytest.raises(ValueError, match="limit"):
        query_service.entity_search("Alpha", limit=101)
    with pytest.raises(ValueError, match="max_hops"):
        query_service.traverse_relationships("urn:entity:a", AT, max_hops=4)
    with pytest.raises(ValueError, match="timeout_ms"):
        query_service.entity_lookup("urn:entity:a", timeout_ms=0)
    with pytest.raises(ValueError, match="predicate_iri"):
        query_service.relationships_as_of("urn:entity:a", AT, predicate_iri="connectedTo")
    with pytest.raises(ValueError, match="timezone-aware"):
        query_service.relationships_as_of("urn:entity:a", datetime(2026, 8, 21))
