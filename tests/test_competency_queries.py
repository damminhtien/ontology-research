"""Regression harness for competency queries (Phase 0 contract).

Delegates execution to tools/cq_runner.py - the same runner the console
monitoring API uses - and asserts each query matches its ground truth.

This pins down ANSWER CORRECTNESS. Latency SLOs are measured separately once
the production read store exists (see requirements/performance_slo.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import cq_runner
import pytest
from conftest import EXPECTED_DIR, QUERIES_DIR


def _spec_files() -> list[Path]:
    specs = sorted(EXPECTED_DIR.glob("*.json"))
    assert specs, "no expected-results specs found — benchmark contract is empty"
    return specs


@pytest.mark.parametrize("spec_path", _spec_files(), ids=lambda p: p.stem)
def test_competency_query_matches_ground_truth(spec_path, benchmark_graph):
    spec = json.loads(spec_path.read_text())

    query_file = QUERIES_DIR / spec["query_file"]
    assert query_file.is_file(), f"missing query file {query_file}"

    result = cq_runner.run_query_spec(benchmark_graph, spec)
    assert result["passed"], (
        f"{spec['id']} ({spec.get('group', '?')}): {result['error']}\n"
        f"actual rows: {result['actual_rows']}"
    )
