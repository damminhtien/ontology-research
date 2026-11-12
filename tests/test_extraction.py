"""Tests for unstructured-document extraction (Phase 2 — LLM chỉ đề xuất)."""

from __future__ import annotations

import json

import pytest
from conftest import CORE_ONTOLOGY, SHAPES_FILE

from foundry.events import EventLog
from foundry.extraction import (
    CAP_EXTRACTED_CONFIDENCE,
    CandidateFact,
    LlmExtractor,
    PatternExtractor,
)
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline
from foundry.namespaces import CORE_LOCATED_AT, CORE_MEMBER_OF

VN_TEXT = (
    "Cảnh sát biển Việt Nam đặt tại Đà Nẵng từ 2026-05-01. "
    "Vietnam Coast Guard is part of Bộ Quốc phòng Việt Nam. "
    "Bộ Quốc phòng Việt Nam có trụ sở tại Hà Nội."
)


@pytest.fixture()
def pipeline(tmp_path) -> IngestionPipeline:
    pipeline = IngestionPipeline(
        identity=IdentityService(),
        log=EventLog(tmp_path / "events.jsonl"),
        ontology_path=CORE_ONTOLOGY,
        shapes_path=SHAPES_FILE,
    )
    pipeline.ingest_entity(
        name="Cảnh sát biển Việt Nam",
        entity_type="Organization",
        aliases=["Vietnam Coast Guard"],
        source_id="s1",
    )
    pipeline.ingest_entity(
        name="Bộ Quốc phòng Việt Nam",
        entity_type="Organization",
        aliases=["Ministry of National Defence"],
        source_id="s2",
    )
    return pipeline


class TestPatternExtractor:
    def test_extracts_vietnamese_located_at_with_date(self):
        candidates = PatternExtractor().extract("Đơn vị X đặt tại Đà Nẵng từ 2026-05-01.")
        assert len(candidates) == 1
        assert candidates[0].subject_name == "Đơn vị X"
        assert candidates[0].predicate_iri == CORE_LOCATED_AT
        assert candidates[0].object_value == "Đà Nẵng"
        assert candidates[0].valid_from == "2026-05-01T00:00:00Z"
        assert candidates[0].confidence == CAP_EXTRACTED_CONFIDENCE

    def test_extracts_english_part_of(self):
        candidates = PatternExtractor().extract("Alpha Unit is part of Region 7 Command.")
        assert len(candidates) == 1
        assert candidates[0].predicate_iri == CORE_MEMBER_OF
        assert candidates[0].object_kind == "entity"
        assert candidates[0].object_value == "Region 7 Command"

    def test_extracts_multiple_patterns_from_one_text(self):
        candidates = PatternExtractor().extract(VN_TEXT)
        predicates = [c.predicate_iri for c in candidates]
        assert predicates.count(CORE_LOCATED_AT) == 2
        assert predicates.count(CORE_MEMBER_OF) == 1

    def test_no_candidates_from_plain_text(self):
        assert PatternExtractor().extract("Không có gì để trích xuất ở đây.") == []


class TestLlmExtractor:
    def test_requires_completion_callable(self):
        with pytest.raises(ValueError, match="completion callable"):
            LlmExtractor()

    def test_parses_json_candidates_and_caps_confidence(self):
        completion = json.dumps(
            [
                {
                    "subject_name": "Org A",
                    "predicate_iri": CORE_LOCATED_AT,
                    "object_kind": "location",
                    "object_value": "Đà Nẵng",
                    "valid_from": "2026-08-01T00:00:00Z",
                    "confidence": 0.99,  # must be capped
                },
                {"subject_name": "", "predicate_iri": CORE_LOCATED_AT, "object_value": "x"},
            ]
        )
        candidates = LlmExtractor(complete=lambda _text: completion).extract("text")
        assert len(candidates) == 1  # the incomplete candidate is dropped
        assert candidates[0].confidence == CAP_EXTRACTED_CONFIDENCE

    def test_rejects_non_json_completion(self):
        with pytest.raises(ValueError, match="not a JSON candidate list"):
            LlmExtractor(complete=lambda _text: "tôi nghĩ rằng...").extract("text")

    def test_rejects_non_array_body(self):
        with pytest.raises(ValueError, match="JSON array"):
            LlmExtractor(complete=lambda _text: '{"subject_name": "x"}').extract("text")


class TestIngestDocument:
    def test_unknown_location_name_is_queued_without_a_fake_location(self, pipeline):
        result = pipeline.ingest_document(
            uri="https://example.org/report-1",
            title="Báo cáo tuần",
            source_system="crawler",
            text="Cảnh sát biển Việt Nam đặt tại Đà Nẵng từ 2026-05-01.",
        )
        assert result.candidates == 1
        assert result.asserted == 0 and result.queued == 1
        assert not result.assertion_results
        assert [event.event_type for event in pipeline._log.read_all()][
            -1
        ] == "ResolutionReviewQueued"

    def test_entity_object_resolved_via_alias(self, pipeline):
        result = pipeline.ingest_document(
            uri="https://example.org/report-2",
            title="Cấu trúc",
            source_system="crawler",
            text="Vietnam Coast Guard is part of Bộ Quốc phòng Việt Nam từ 2026-06-01.",
        )
        # The exact alias resolves to a canonical entity before assertion.
        assert result.asserted == 1
        payload = result.assertion_results[0].event.payload
        assert payload["object"]["kind"] == "entity"
        assert (
            payload["object"]["value"]
            == pipeline._identity.lookup(
                name="Bộ Quốc phòng Việt Nam", entity_type="Organization"
            ).canonical_id
        )

    def test_unknown_subject_queued_and_never_minted(self, pipeline):
        before = len(pipeline._identity)
        result = pipeline.ingest_document(
            uri="https://example.org/report-3",
            title="Tin chưa xác minh",
            source_system="crawler",
            text="Lực lượng bí ẩn X đặt tại Nơi bí mật từ 2026-07-01.",
        )
        assert result.candidates == 1
        assert result.queued == 1 and result.asserted == 0
        # no entity was minted from the extractor proposal
        assert len(pipeline._identity) == before

    def test_undated_candidates_are_skipped(self, pipeline):
        result = pipeline.ingest_document(
            uri="https://example.org/report-4",
            title="Không mốc thời gian",
            source_system="crawler",
            text="Cảnh sát biển Việt Nam đặt tại Đà Nẵng.",
        )
        assert result.candidates == 1
        assert result.skipped == 1 and result.asserted == 0

    def test_ledger_records_assertions_with_document_source(self, pipeline):
        from foundry.assertions import load_assertions

        class FixedExtractor:
            def extract(self, _text):
                return [
                    CandidateFact(
                        subject_name="Cảnh sát biển Việt Nam",
                        predicate_iri=CORE_LOCATED_AT,
                        object_kind="location",
                        object_value="urn:world:location:da-nang",
                        valid_from="2026-05-01T00:00:00Z",
                        confidence=CAP_EXTRACTED_CONFIDENCE,
                    )
                ]

        result = pipeline.ingest_document(
            uri="https://example.org/report-1",
            title="Báo cáo tuần",
            source_system="crawler",
            text="source with a canonical location identifier",
            extractor=FixedExtractor(),
        )
        ledger = load_assertions(pipeline._log)
        entries = list(ledger.values())
        assert len(entries) == 1
        assert entries[0]["source_ids"] == [result.document_id]
        assert entries[0]["status"] == "asserted"
