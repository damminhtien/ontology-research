"""Shared runner for competency-query regression checks.

Single source of truth used by the pytest suite (tests/test_competency_queries.py)
and by the console monitoring API (foundry/console/api/monitor.py).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ontology_utils import find_module_files
from rdflib import Graph, Literal, URIRef
from rdflib.plugins.sparql import prepareQuery

REPO_ROOT = Path(__file__).resolve().parent.parent
QUERIES_DIR = REPO_ROOT / "benchmarks" / "queries"
EXPECTED_DIR = REPO_ROOT / "benchmarks" / "expected_results"


class SemanticQueryAdapter(Protocol):
    """Minimal SELECT surface shared by competency-query backends."""

    def select(self, query_text: str, bindings: dict[str, object]) -> tuple[list[str], list[list]]:
        """Execute one prepared SELECT and return ordered variables and rows."""
        ...


class RDFLibQueryAdapter:
    """RDFLib implementation of the shared competency-query contract."""

    def __init__(self, graph: Graph) -> None:
        """Use one loaded graph for all competency SELECT queries."""
        self.graph = graph

    def select(self, query_text: str, bindings: dict[str, object]) -> tuple[list[str], list[list]]:
        """Run SELECT over the supplied graph with typed initial bindings."""
        result = self.graph.query(prepareQuery(query_text), initBindings=bindings)
        variables = [str(variable) for variable in result.vars]
        rows = [
            [
                _normalize_cell(row[variable]) if row[variable] is not None else None
                for variable in result.vars
            ]
            for row in result
        ]
        return variables, rows


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


def load_benchmark_graph(
    ontology: list[Path] | None = None, data: list[Path] | None = None
) -> Graph:
    """Load the full graph the CQs run against.

    Defaults to every registered ontology module (core, middle, domain) plus
    every dataset under benchmarks/datasets. The CQ suite covers the complete
    domain model built on the kernel.
    """
    ontology_paths = ontology if ontology is not None else find_module_files()
    data_paths = (
        data if data is not None else sorted((REPO_ROOT / "benchmarks" / "datasets").glob("*.ttl"))
    )
    graph = Graph()
    for path in ontology_paths:
        graph.parse(path.as_posix(), format="turtle")
    for path in data_paths:
        graph.parse(path.as_posix(), format="turtle")
    return graph


def _query_bindings(spec: dict) -> dict[str, object]:
    """Convert compact JSON IRI/literal bindings into rdflib terms."""
    bindings: dict[str, object] = {}
    for variable, value in (spec.get("bindings") or {}).items():
        if not isinstance(value, dict):
            raise ValueError(f"binding {variable!r} must declare iri or literal")
        if "iri" in value:
            bindings[variable] = URIRef(value["iri"])
        elif "literal" in value:
            bindings[variable] = Literal(
                value["literal"],
                datatype=URIRef(value["datatype"]) if value.get("datatype") else None,
                lang=value.get("lang"),
            )
        else:
            raise ValueError(f"binding {variable!r} must declare iri or literal")
    return bindings


def run_query_spec(
    adapter: Graph | SemanticQueryAdapter,
    spec: dict,
    *,
    queries_dir: Path = QUERIES_DIR,
) -> dict:
    """Run one competency query spec and compare against its ground truth."""
    result: dict = {
        "id": spec.get("id"),
        "group": spec.get("group"),
        "query_file": spec.get("query_file"),
        "passed": False,
        "expected_rows": 0,
        "actual_rows": [],
        "error": None,
    }
    try:
        result["expected_rows"] = len(spec["rows"])
        query_text = (queries_dir / spec["query_file"]).read_text(encoding="utf-8")
        query_adapter = RDFLibQueryAdapter(adapter) if isinstance(adapter, Graph) else adapter
        variables, actual_rows = query_adapter.select(query_text, _query_bindings(spec))
        if variables != spec["variables"]:
            result["error"] = f"projected variables {variables} != expected {spec['variables']}"
            return result

        expected = [
            [cell if cell is None or isinstance(cell, (bool, int)) else str(cell) for cell in row]
            for row in spec["rows"]
        ]
        result["actual_rows"] = actual_rows
        result["passed"] = actual_rows == expected
        if not result["passed"]:
            result["error"] = f"rows mismatch: expected {expected}, got {actual_rows}"
        return result
    except Exception as exc:  # surface any failure as a failed check, not a crash
        result["error"] = str(exc)
        return result


def run_competency_queries(
    graph: Graph | None = None,
    *,
    adapter: SemanticQueryAdapter | None = None,
) -> list[dict]:
    """Run every registered competency query; returns one result per spec."""
    if graph is not None and adapter is not None:
        raise ValueError("provide graph or adapter, not both")
    query_adapter = adapter or RDFLibQueryAdapter(
        graph if graph is not None else load_benchmark_graph()
    )
    specs = sorted(EXPECTED_DIR.glob("*.json"))
    return [
        run_query_spec(query_adapter, json.loads(spec.read_text(encoding="utf-8")))
        for spec in specs
    ]
