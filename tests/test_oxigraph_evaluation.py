"""Contracts for the isolated Oxigraph/Jena evaluation helpers."""

from __future__ import annotations

import pytest

pytest.importorskip("pyoxigraph")

from evaluate_oxigraph import _query_statistics, _sparql_json_rows


def test_sparql_json_rows_normalizes_literals_uris_and_unbound_values():
    """Jena result JSON uses the same normalized cells as the Oxigraph adapter."""
    result = {
        "head": {"vars": ["entity", "name", "at", "optional"]},
        "results": {
            "bindings": [
                {
                    "entity": {"type": "uri", "value": "urn:entity:1"},
                    "name": {"type": "literal", "value": "Example"},
                    "at": {
                        "type": "literal",
                        "datatype": "http://www.w3.org/2001/XMLSchema#dateTime",
                        "value": "2026-11-15T12:00:00+00:00",
                    },
                }
            ]
        },
    }

    variables, rows = _sparql_json_rows(result)

    assert variables == ["entity", "name", "at", "optional"]
    assert rows == [["urn:entity:1", "Example", "2026-11-15T12:00:00Z", None]]


def test_query_statistics_report_nearest_rank_percentiles_and_samples():
    """Reported warm percentiles use the documented nearest-rank convention."""
    assert _query_statistics([1.0, 2.0, 3.0, 4.0, 5.0], warmups=2) == {
        "sample_count": 5,
        "warmup_count": 2,
        "p50_ms": 3.0,
        "p95_ms": 5.0,
        "p99_ms": 5.0,
    }
