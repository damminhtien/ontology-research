"""Reference lane + QID-cursor paging tests (architecture §4.6).

The lane stores versioned typed Parquet snapshots of external reference data;
it is manifest-authoritative, atomically published, and never a source of
truth — ingestion converts lane rows into the same normalized records the
live fetch produces, and facts still pass identity + SHACL into the log.
All tests run offline through the injectable fetcher.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import CORE_ONTOLOGY, SHAPES_FILE

from foundry import wikidata as wd
from foundry.events import EventLog
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline
from foundry.reference import (
    MANIFEST_NAME,
    ReferenceError,
    ReferenceRecord,
    latest_snapshot_id,
    read_manifest,
    read_snapshot,
    write_snapshot,
)

CLASS = "Q43229"


def _row(qid: str, vi: str = "", en: str = "", *, cls: str = CLASS) -> ReferenceRecord:
    return ReferenceRecord(
        qid=qid,
        name_vi=vi,
        name_en=en,
        type_qids=(),
        class_qid=cls,
        fetched_at="2026-09-11T00:00:00Z",
    )


def _binding(item: str, vi: str, en: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "item": {"type": "uri", "value": f"http://www.wikidata.org/entity/{item}"},
        "labelVi": {"type": "literal", "value": vi},
    }
    if en is not None:
        out["labelEn"] = {"type": "literal", "value": en}
    return out


def _paged_fetcher(
    pages: dict[str, list[dict[str, Any]]],
) -> Callable[[str, float], dict[str, Any]]:
    """Serve one canned page per cursor value (cursor='' serves pages[''])."""

    def fetch(url: str, timeout: float) -> dict[str, Any]:
        # the cursor is embedded in the URL-encoded SPARQL query text
        for cursor, bindings in pages.items():
            if f"%22{urllib.parse.quote(cursor, safe='')}%22" in url:
                pages.pop(cursor)  # each page is served once
                return {"results": {"bindings": bindings}}
        raise AssertionError(f"unexpected fetch: {url}")

    return fetch


class TestCursorPaging:
    def test_pages_tile_without_overlap(self):
        page1 = [_binding(f"Q{i}", f"Ten {i}", f"Name {i}") for i in range(1, 4)]
        page2 = [_binding(f"Q{i}", f"Ten {i}") for i in range(4, 6)]  # short = exhausted
        rows = list(
            wd.iter_entities(
                page_size=3,
                fetcher=_paged_fetcher({"": page1, f"{wd.WIKIDATA_ENTITY_IRI}Q3": page2}),
            )
        )
        assert [r.qid for r in rows] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
        assert all(r.class_qid == CLASS for r in rows)

    def test_max_records_stops_mid_page(self):
        page1 = [_binding(f"Q{i}", f"Ten {i}") for i in range(1, 4)]
        rows = list(
            wd.iter_entities(page_size=3, max_records=2, fetcher=_paged_fetcher({"": page1}))
        )
        assert [r.qid for r in rows] == ["Q1", "Q2"]

    def test_cursor_query_pins_order_and_filter(self):
        assert "ORDER BY ?item" in wd.CURSOR_QUERY_TEMPLATE
        assert 'FILTER(STR(?item) > "%(cursor)s")' in wd.CURSOR_QUERY_TEMPLATE

    def test_rows_without_usable_name_are_skipped(self):
        page1 = [_binding("Q1", ""), _binding("Q2", "Ten 2", "Name 2")]
        rows = list(wd.iter_entities(page_size=2, fetcher=_paged_fetcher({"": page1})))
        assert [r.qid for r in rows] == ["Q2"]

    def test_endpoint_failure_raises(self):
        def boom(url: str, timeout: float) -> dict[str, Any]:
            raise wd.WikidataError("down")

        with pytest.raises(wd.WikidataError, match="down"):
            list(wd.iter_entities(page_size=2, retries=0, backoff=0, fetcher=boom))


class TestSnapshotRoundTrip:
    def test_write_read_round_trip_typed(self, tmp_path: Path):
        info = write_snapshot(
            [_row("Q1", "Tên một", "Name one"), _row("Q2", "", "Name two")],
            root=tmp_path,
            class_qid=CLASS,
        )
        assert info.rows == 2
        rows = read_snapshot(tmp_path, CLASS, info.id)
        assert [(r.qid, r.name_vi, r.name_en) for r in rows] == [
            ("Q1", "Tên một", "Name one"),
            ("Q2", "", "Name two"),
        ]
        assert rows[0].type_qids == () and rows[0].fetched_at == "2026-09-11T00:00:00Z"

    def test_latest_switches_and_versions_are_listed(self, tmp_path: Path):
        first = write_snapshot([_row("Q1", "Một")], root=tmp_path, class_qid=CLASS)
        second = write_snapshot(
            [_row("Q1", "Một"), _row("Q2", "Hai")], root=tmp_path, class_qid=CLASS
        )
        assert latest_snapshot_id(tmp_path, CLASS) == second.id
        manifest = read_manifest(tmp_path, CLASS)
        assert [s["id"] for s in manifest["snapshots"]] == [first.id, second.id]
        # versioned reads stay exact
        assert [r.qid for r in read_snapshot(tmp_path, CLASS, first.id)] == ["Q1"]
        assert [r.qid for r in read_snapshot(tmp_path, CLASS)] == ["Q1", "Q2"]

    def test_read_is_manifest_authoritative(self, tmp_path: Path):
        info = write_snapshot([_row("Q1", "Một")], root=tmp_path, class_qid=CLASS)
        # an orphaned file in the snapshot dir must stay invisible
        orphan = tmp_path / info.path.replace("part-00000.parquet", "rogue.parquet")
        orphan.write_bytes(b"bogus")
        assert [r.qid for r in read_snapshot(tmp_path, CLASS)] == ["Q1"]

    def test_unknown_snapshot_id_rejected(self, tmp_path: Path):
        write_snapshot([_row("Q1", "Một")], root=tmp_path, class_qid=CLASS)
        with pytest.raises(ReferenceError, match="not registered"):
            read_snapshot(tmp_path, CLASS, "snap-nope")

    def test_missing_manifest_rejected(self, tmp_path: Path):
        with pytest.raises(ReferenceError, match="no reference manifest"):
            read_snapshot(tmp_path, CLASS)

    def test_corrupt_manifest_rejected(self, tmp_path: Path):
        class_dir = tmp_path / "wikidata" / CLASS
        class_dir.mkdir(parents=True)
        (class_dir / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        with pytest.raises(ReferenceError, match="corrupt"):
            read_manifest(tmp_path, CLASS)

    def test_empty_fetch_refuses_to_publish(self, tmp_path: Path):
        with pytest.raises(ReferenceError, match="empty snapshot"):
            write_snapshot([], root=tmp_path, class_qid=CLASS)

    def test_manifest_published_without_tmp_leftover(self, tmp_path: Path):
        write_snapshot([_row("Q1", "Một")], root=tmp_path, class_qid=CLASS)
        class_dir = tmp_path / "wikidata" / CLASS
        assert not (class_dir / (MANIFEST_NAME + ".tmp")).exists()
        payload = json.loads((class_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        assert payload["latest"] == payload["snapshots"][-1]["id"]
        assert payload["schema_version"] == 1


class TestLaneToPipeline:
    def test_records_from_reference_prefers_vi_en_as_alias(self):
        records = wd.records_from_reference(
            [_row("Q1", "Tên một", "Name one"), _row("Q2", "", "Name two")]
        )
        assert [r.name for r in records] == ["Tên một", "Name two"]
        assert records[0].aliases == ("Name one",)
        assert records[1].aliases == ()
        assert [r.entity_type for r in records] == ["Organization", "Organization"]

    def test_unusable_rows_are_dropped(self):
        records = wd.records_from_reference([_row("Q1"), _row("Q2", "Tên hai")])
        assert [r.qid for r in records] == ["Q2"]

    def test_lane_snapshot_ingests_through_the_pipeline(self, tmp_path: Path):
        log_path = tmp_path / "events.jsonl"
        lane_root = tmp_path / "lane"
        info = write_snapshot(
            [_row("Q9", "Tên chín", "Name nine")], root=lane_root, class_qid=CLASS
        )
        records = wd.records_from_reference(read_snapshot(lane_root, CLASS, info.id))
        pipeline = IngestionPipeline(
            identity=IdentityService(),
            log=EventLog(log_path),
            ontology_path=CORE_ONTOLOGY,
            shapes_path=SHAPES_FILE,
        )
        stats, receipts, events = wd.ingest_records(pipeline, records)
        assert stats.accepted == 1 and stats.new_entities == 1
        assert events and events[0].event_type == "EntityCreated"
        assert receipts[0].accepted
