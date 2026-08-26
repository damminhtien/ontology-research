"""Shared runner for competency-query regression checks.

Single source of truth used by the pytest suite (tests/test_competency_queries.py)
and by the console monitoring API (foundry/console/api/monitor.py).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from rdflib import Graph
from rdflib.plugins.sparql import prepareQuery

REPO_ROOT = Path(__file__).resolve().parent.parent
QUERIES_DIR = REPO_ROOT / "benchmarks" / "queries"
EXPECTED_DIR = REPO_ROOT / "benchmarks" / "expected_results"


def _normalize_cell(value) -> object:
    """Normalize one result cell to a JSON-comparable value.

    Counts arrive as ints; datetimes are canonicalized back to the xsd lexical
    form with a trailing 'Z'; everything else keeps its string form.
    """
    python_value = value.toPython()
    if isinstance(python_value, bool):
        return python_value
    if isinstance(python_value, int):
        return int(python_value)
    if isinstance(python_value, datetime):
        return python_value.isoformat().replace("+00:00", "Z")
    return str(value)


def load_benchmark_graph(ontology: Path | None = None, data: Path | None = None) -> Graph:
    """Load the ontology + seed dataset graph the CQs run against."""
    ontology = ontology or REPO_ROOT / "ontology" / "core" / "core.ttl"
    data = data or REPO_ROOT / "benchmarks" / "datasets" / "sample_data.ttl"
    graph = Graph()
    graph.parse(ontology.as_posix(), format="turtle")
    graph.parse(data.as_posix(), format="turtle")
    return graph


def run_query_spec(graph: Graph, spec: dict) -> dict:
    """Run one competency query spec and compare against its ground truth."""
    result: dict = {
        "id": spec.get("id"),
        "group": spec.get("group"),
        "query_file": spec.get("query_file"),
        "passed": False,
        "expected_rows": len(spec.get("rows", [])),
        "actual_rows": [],
        "error": None,
    }
    try:
        query_text = (QUERIES_DIR / spec["query_file"]).read_text(encoding="utf-8")
        binding_result = graph.query(prepareQuery(query_text))

        variables = [str(v) for v in binding_result.vars]
        if variables != spec["variables"]:
            result["error"] = f"projected variables {variables} != expected {spec['variables']}"
            return result

        actual_rows: list[list] = []
        for binding in binding_result:
            row = [
                _normalize_cell(binding[var]) if binding[var] is not None else None
                for var in spec["variables"]
            ]
            actual_rows.append(row)

        expected = [
            [cell if isinstance(cell, int) else str(cell) for cell in row] for row in spec["rows"]
        ]
        result["actual_rows"] = actual_rows
        result["passed"] = actual_rows == expected
        if not result["passed"]:
            result["error"] = f"rows mismatch: expected {expected}, got {actual_rows}"
        return result
    except Exception as exc:  # surface any failure as a failed check, not a crash
        result["error"] = str(exc)
        return result


def run_competency_queries(graph: Graph | None = None) -> list[dict]:
    """Run every registered competency query; returns one result per spec."""
    graph = graph if graph is not None else load_benchmark_graph()
    specs = sorted(EXPECTED_DIR.glob("*.json"))
    return [run_query_spec(graph, json.loads(spec.read_text(encoding="utf-8"))) for spec in specs]
