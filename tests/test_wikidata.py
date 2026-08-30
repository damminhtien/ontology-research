"""Tests for the Wikidata ingestion bridge — fully offline via fake fetchers."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from foundry import wikidata as wd
from foundry.events import EventLog
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline

ONTOLOGY = Path(__file__).resolve().parent.parent / "ontology" / "core" / "core.ttl"
SHAPES = Path(__file__).resolve().parent.parent / "shapes" / "core_shapes.ttl"


def sparql_response(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a minimal SPARQL JSON result from raw binding rows."""
    return {"head": {"vars": list(rows[0].keys()) if rows else []}, "results": {"bindings": rows}}


def binding(item: str, type_: str, vi: str, en: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "item": {"type": "uri", "value": f"http://www.wikidata.org/entity/{item}"},
        "type": {"type": "uri", "value": f"http://www.wikidata.org/entity/{type_}"},
        "labelVi": {"type": "literal", "value": vi, "xml:lang": "vi"},
    }
    if en is not None:
        row["labelEn"] = {"type": "literal", "value": en, "xml:lang": "en"}
    return row


def fake_fetcher(payload: dict[str, Any]) -> Callable[[str, float], dict[str, Any]]:
    def fetch(url: str, timeout: float) -> dict[str, Any]:
        return payload

    return fetch


def make_pipeline(tmp_path: Path) -> IngestionPipeline:
    return IngestionPipeline(
        identity=IdentityService(),
        log=EventLog(tmp_path / "events.jsonl"),
        ontology_path=ONTOLOGY,
        shapes_path=SHAPES,
    )


class TestFetchEntities:
    def test_normalizes_and_merges_multiple_p31_rows(self) -> None:
        payload = sparql_response(
            [
                binding("Q1", "Q43229", "Tổ chức A", "Org A"),
                binding("Q1", "Q4830453", "Tổ chức A", "Org A"),
                binding("Q2", "Q79913", "Tổ chức B"),
            ]
        )
        records = wd.fetch_entities(fetcher=fake_fetcher(payload))
        assert len(records) == 2
        first = next(r for r in records if r.qid == "Q1")
        assert first.name == "Tổ chức A"
        assert first.entity_type == "Organization"
        assert first.aliases == ("Org A",)
        # merged rows keep both classes
        assert set(first.type_qids) == {"Q43229", "Q4830453"}

    def test_vi_label_preferred_en_fallback(self) -> None:
        no_vi = {
            "item": {"value": "http://www.wikidata.org/entity/Q3"},
            "type": {"value": "http://www.wikidata.org/entity/Q43229"},
            "labelEn": {"value": "Only English"},
        }
        records = wd.fetch_entities(fetcher=fake_fetcher(sparql_response([no_vi])))
        assert len(records) == 1
        assert records[0].name == "Only English"

    def test_rows_without_usable_name_are_dropped(self) -> None:
        blank = {"item": {"value": "http://www.wikidata.org/entity/Q4"}, "type": {"value": "X"}}
        records = wd.fetch_entities(fetcher=fake_fetcher(sparql_response([blank])))
        assert records == []


class TestIngestRecords:
    def test_new_entity_ingested_with_external_id(self, tmp_path: Path) -> None:
        pipeline = make_pipeline(tmp_path)
        records = [
            wd.WikidataRecord(
                qid="Q9", name="Công ty X", entity_type="Organization", aliases=("Company X",)
            )
        ]
        stats, receipts, events = wd.ingest_records(pipeline, records)
        assert stats.total == 1
        assert stats.accepted == 1
        assert stats.new_entities == 1
        assert stats.merged == 0
        assert stats.rejected == 0
        assert stats.unresolved_rate == 0.0
        assert len(events) == 1
        assert receipts[0].accepted
        # external_id stored so a re-run resolves to the same canonical entity
        assert events[0].payload.get("external_id") == "Q9" or "Q9" in str(events[0].payload)

    def test_second_run_merges_instead_of_creating_new(self, tmp_path: Path) -> None:
        pipeline = make_pipeline(tmp_path)
        records = [wd.WikidataRecord(qid="Q10", name="Tổ chức Y", entity_type="Organization")]
        wd.ingest_records(pipeline, records)
        stats2, _receipts2, events2 = wd.ingest_records(pipeline, records)
        assert stats2.accepted == 1
        assert stats2.new_entities == 0
        assert stats2.merged == 1
        # merge only links the external_id to the existing canonical entity:
        # no new EntityCreated event (dedup event type is future contract work)
        assert events2 == []

    def test_unknown_type_is_skipped_not_rejected(self, tmp_path: Path) -> None:
        pipeline = make_pipeline(tmp_path)
        records = [wd.WikidataRecord(qid="Q11", name="Planet Z", entity_type=None)]
        stats, receipts, events = wd.ingest_records(pipeline, records)
        assert stats.total == 1
        assert stats.skipped_no_type == 1
        assert stats.accepted == 0
        assert stats.rejected == 0
        assert receipts == [] and events == []

    def test_review_gate_counts_as_rejected_with_reason(self, tmp_path: Path) -> None:
        pipeline = make_pipeline(tmp_path)
        # No external_id shared and no alias overlap -> fuzzy match requires review
        records = [
            wd.WikidataRecord(qid="Q12", name="Alpha Patrol Unit Two", entity_type="Organization")
        ]
        wd.ingest_records(
            pipeline,
            [
                wd.WikidataRecord(
                    qid="Q13", name="Alpha Patrol Unit One", entity_type="Organization"
                )
            ],
        )
        stats, receipts, _events = wd.ingest_records(pipeline, records)
        assert stats.rejected == 1
        assert stats.unresolved_rate == 1.0  # per-run: 1 of 1 went to review
        assert receipts[0].accepted is False
        assert receipts[0].reason  # structured reason, not silent drop
        assert stats.rejection_reasons


def test_query_limits_items_not_ancestor_type_rows():
    """Regression: the query must not join ``?item P31/P279* ?type``.

    That join multiplied rows per item so LIMIT was consumed by duplicate
    ancestor-type rows (3000 rows -> 32 entities) and heavy closure queries
    timed out on the endpoint (HTTP 504). Entity type is instead derived from
    the queried class itself.
    """
    query = wd.DEFAULT_QUERY_TEMPLATE % {"class": "Q43229", "limit": 5000}
    assert "LIMIT 5000" in query
    assert "?item wdt:P31/wdt:P279* ?type" not in query
    assert "?labelVi" in query and '?labelVi) = "vi"' in query


def test_rows_without_type_fall_back_to_queried_class_type():
    """Regression: rows without an explicit type still map via class_qid."""
    payload = sparql_response(
        [
            {
                "item": {"value": "http://www.wikidata.org/entity/Q77"},
                "labelVi": {"value": "Tổ chức Không loại"},
                "labelEn": {"value": "Typeless Org"},
            }
        ]
    )
    records = wd.fetch_entities(fetcher=fake_fetcher(payload))
    assert len(records) == 1
    assert records[0].entity_type == "Organization"  # from class_qid=Q43229
    assert records[0].aliases == ("Typeless Org",)


def test_unmapped_class_leaves_type_unresolved():
    """Rows for a class with no core-type mapping are still skipped, not guessed."""
    payload = sparql_response(
        [
            {
                "item": {"value": "http://www.wikidata.org/entity/Q78"},
                "labelVi": {"value": "Hành tinh"},
            }
        ]
    )
    records = wd.fetch_entities(class_qid="Q6999", fetcher=fake_fetcher(payload))
    assert len(records) == 1
    assert records[0].entity_type is None


def test_military_unit_qid_mapping_is_the_real_item():
    """Regression: Q1767992 is a temple, military unit is Q176799."""
    assert wd.QID_TO_ENTITY_TYPE.get("Q176799") == "Organization"
    assert "Q1767992" not in wd.QID_TO_ENTITY_TYPE
