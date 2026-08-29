"""Regression tests for the Wikidata measurement harness (offline, fake fetcher)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, (REPO_ROOT / "tools").as_posix())

import measure_wikidata  # noqa: E402

from foundry import wikidata as wd  # noqa: E402


def _fake_fetcher(url: str, timeout: float = 30.0) -> dict:
    """Return two org rows (vi + en labels) for any SPARQL query."""
    return {
        "results": {
            "bindings": [
                {
                    "item": {"value": "http://www.wikidata.org/entity/Q9991"},
                    "type": {"value": "http://www.wikidata.org/entity/Q43229"},
                    "labelVi": {"value": "Công ty A"},
                    "labelEn": {"value": "Company A"},
                },
                {
                    "item": {"value": "http://www.wikidata.org/entity/Q9992"},
                    "type": {"value": "http://www.wikidata.org/entity/Q4830453"},
                    "labelVi": {"value": "Vinamilk"},
                },
            ]
        }
    }


class TestRunBatch:
    def test_measures_all_kpi_fields(self, tmp_path: Path) -> None:
        result = measure_wikidata.run_batch("Q43229", 10, tmp_path, fetcher=_fake_fetcher)

        assert result["records"] == 2
        assert result["mapped"] == 2
        assert result["mapped_rate"] == 1.0
        assert result["accepted"] == 2
        assert result["new_entities"] == 2
        assert result["rejected"] == 0
        assert result["unresolved_rate"] == 0.0
        assert result["events"] == 2
        assert result["ingest_events_per_sec"] > 0
        # lake actually received both events
        assert result["lake_bytes"] > 0
        assert result["lake_bytes_per_event"] > 0
        assert result["json_bytes_per_event"] > 0
        # no KPI field is a placeholder
        for key in ("fetch_seconds", "ingest_seconds", "lake_seconds"):
            assert isinstance(result[key], float)

    def test_lake_dir_contains_parquet_and_manifest(self, tmp_path: Path) -> None:
        measure_wikidata.run_batch("Q43229", 10, tmp_path, fetcher=_fake_fetcher)
        parquet_files = list((tmp_path / "lake").rglob("*.parquet"))
        assert len(parquet_files) == 1
        manifest = json.loads((tmp_path / "lake" / "manifest.json").read_text())
        assert manifest["files"][0]["rows"] == 2


class TestCountClass:
    def test_get_json_retries_then_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []

        def boom(req, timeout):
            calls.append(1)
            raise OSError("down")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        monkeypatch.setattr("time.sleep", lambda _s: None)
        with pytest.raises(wd.WikidataError):
            measure_wikidata._get_json("http://example.test/x", timeout=0.1, retries=1)
        assert len(calls) == 2
