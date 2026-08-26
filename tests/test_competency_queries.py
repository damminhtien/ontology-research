"""Regression harness for competency queries (Phase 0 contract).

For every JSON spec in benchmarks/expected_results/, run the paired SPARQL
query from benchmarks/queries/ against the seed dataset and compare the full
result set (variables + ordered rows) with the ground truth.

This pins down ANSWER CORRECTNESS. Latency SLOs are measured separately once
the production read store exists (see requirements/performance_slo.md).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from conftest import EXPECTED_DIR, QUERIES_DIR
from rdflib.plugins.sparql import prepareQuery


def _spec_files() -> list[Path]:
    specs = sorted(EXPECTED_DIR.glob("*.json"))
    assert specs, "no expected-results specs found — benchmark contract is empty"
    return specs


def _normalize_cell(value) -> object:
    """Normalize one result cell to a comparable JSON-friendly value."""
    python_value = value.toPython()
    if isinstance(python_value, bool):
        return python_value
    if isinstance(python_value, int):
        return int(python_value)
    if isinstance(python_value, datetime):
        # xsd:dateTime arrives as an aware datetime; canonicalize back to
        # the RDF lexical form with a trailing 'Z' for UTC.
        iso = python_value.isoformat()
        return iso.replace("+00:00", "Z")
    return str(value)


@pytest.mark.parametrize("spec_path", _spec_files(), ids=lambda p: p.stem)
def test_competency_query_matches_ground_truth(spec_path, benchmark_graph):
    spec = json.loads(spec_path.read_text())

    query_file = QUERIES_DIR / spec["query_file"]
    assert query_file.is_file(), f"missing query file {query_file}"
    query_text = query_file.read_text()

    prepared = prepareQuery(query_text)
    result = benchmark_graph.query(prepared)

    actual_vars = [str(v) for v in result.vars]
    assert actual_vars == spec["variables"], (
        f"{spec['id']}: projected variables {actual_vars} != expected {spec['variables']}"
    )

    actual_rows: list[list] = []
    for binding in result:
        row = []
        for var in spec["variables"]:
            value = binding[var]
            row.append(_normalize_cell(value) if value is not None else None)
        actual_rows.append(row)

    expected_normalized = [
        [cell if isinstance(cell, int) else str(cell) for cell in r] for r in spec["rows"]
    ]

    assert actual_rows == expected_normalized, (
        f"{spec['id']} ({spec.get('group', '?')}): "
        f"\n  expected: {expected_normalized}\n  actual:   {actual_rows}"
    )
