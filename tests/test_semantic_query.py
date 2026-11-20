"""Regression contract for the allowlisted Vietnam semantic query planner."""

from __future__ import annotations

from foundry.console.api.provenance_access import ProvenanceScope
from foundry.semantic_query import IntentSelection, LlmIntentSelector, SemanticQueryService


def test_temporal_location_question_runs_parameterized_cq(benchmark_graph):
    service = SemanticQueryService(benchmark_graph)

    result = service.query(
        "Where was https://data.example/entity/patrol-03 at 2026-08-05T00:00:00Z?"
    )

    assert result["status"] == "matched"
    assert result["template_id"] == "CQ-018"
    assert result["variables"] == ["placeName"]
    assert result["rows"][0][0]["value"] == "Phu Quoc"
    assert result["bindings"]["instant"] == "2026-08-05T00:00:00Z"
    assert "LIMIT 26 OFFSET 0" in result["sparql"]


def test_vietnamese_membership_question_runs_existing_cq(benchmark_graph):
    service = SemanticQueryService(benchmark_graph)

    result = service.query("Lịch sử thành viên của https://data.example/entity/person-17")

    assert result["template_id"] == "CQ-047"
    assert [row[0]["value"] for row in result["rows"]] == [
        "Coast Guard Region 7",
        "Coast Guard Region 8",
    ]


def test_evidence_query_hides_sources_without_scope(benchmark_graph):
    service = SemanticQueryService(benchmark_graph)

    result = service.query("Which sources support https://data.example/entity/assert-17-corrected?")

    assert result["template_id"] == "CQ-027"
    assert result["rows"] == []
    assert result["redacted_source_count"] == 1
    assert "FILTER(false)" in result["sparql"]
    assert "LIMIT 26 OFFSET 0" in result["sparql"]


def test_evidence_query_returns_sources_with_matching_scope(benchmark_graph):
    service = SemanticQueryService(benchmark_graph)

    result = service.query(
        "Which sources support https://data.example/entity/assert-17-corrected?",
        source_scope=ProvenanceScope(
            source_ids=frozenset({"https://data.example/source/report-2026-08-09"})
        ),
    )

    assert result["template_id"] == "CQ-027"
    assert result["rows"][0][0]["value"] == "Operations report 2026-08-09"
    assert result["redacted_source_count"] == 0


def test_missing_required_slots_returns_clarification(benchmark_graph):
    result = SemanticQueryService(benchmark_graph).query("Where was the vessel?")

    assert result["status"] == "needs_input"
    assert set(result["missing_slots"]) == {
        "canonical_entity_or_assertion_iri",
        "timezone_aware_instant",
    }
    assert "sparql" not in result


def test_naive_datetime_requires_timezone(benchmark_graph):
    result = SemanticQueryService(benchmark_graph).query(
        "Where was https://data.example/entity/patrol-03 at 2026-08-05T00:00:00?"
    )

    assert result["status"] == "needs_input"
    assert result["missing_slots"] == ["timezone_aware_instant"]


def test_unrecognized_question_does_not_execute_sparql(benchmark_graph):
    result = SemanticQueryService(benchmark_graph).query("DROP ALL DATA")

    assert result["status"] == "unsupported"
    assert "sparql" not in result


def test_ambiguous_selector_requests_clarification(benchmark_graph):
    class AmbiguousSelector:
        name = "test"

        def select(self, _question, _templates):
            return IntentSelection(("CQ-018", "CQ-047"), self.name)

    result = SemanticQueryService(benchmark_graph, selector=AmbiguousSelector()).query("question")

    assert result["status"] == "ambiguous"
    assert {item["id"] for item in result["candidates"]} == {"CQ-018", "CQ-047"}
    assert "sparql" not in result


def test_model_selector_can_only_choose_registered_query_ids(benchmark_graph):
    selector = LlmIntentSelector(lambda _prompt: '{"template_id":"DROP"}')
    result = SemanticQueryService(benchmark_graph, selector=selector).query(
        "Where was https://data.example/entity/patrol-03 at 2026-08-05T00:00:00Z?"
    )

    assert result["status"] == "unsupported"
    assert result["planner"] == "llm"
    assert "sparql" not in result


def test_model_selector_returns_a_safe_template_not_model_query(benchmark_graph):
    selector = LlmIntentSelector(lambda _prompt: '{"template_id":"CQ-018"}')
    result = SemanticQueryService(benchmark_graph, selector=selector).query(
        "Find it: https://data.example/entity/patrol-03 at 2026-08-05T00:00:00Z"
    )

    assert result["status"] == "matched"
    assert result["planner"] == "llm"
    assert result["template_id"] == "CQ-018"
    assert "SERVICE" not in result["sparql"]


def test_query_bounds_and_pagination_are_validated(benchmark_graph):
    service = SemanticQueryService(benchmark_graph)
    question = "Membership history for https://data.example/entity/person-17"

    page = service.query(question, limit=1, offset=1)
    assert page["rows"][0][0]["value"] == "Coast Guard Region 8"
    assert page["has_more"] is False

    try:
        service.query(question, limit=101)
    except ValueError as exc:
        assert "limit" in str(exc)
    else:
        raise AssertionError("limit above the configured maximum was accepted")
