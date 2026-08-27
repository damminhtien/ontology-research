"""Shared fixtures for the semantic-core test suite."""

from __future__ import annotations

import sys
from pathlib import Path

# Make shared helpers in tools/ and the foundry package importable from tests.
REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "tools", REPO_ROOT):
    if _path.as_posix() not in sys.path:
        sys.path.insert(0, _path.as_posix())

import cq_runner
import pytest
from rdflib import RDF, RDFS, Graph, URIRef

CORE_ONTOLOGY = REPO_ROOT / "ontology" / "core" / "core.ttl"
SHAPES_FILE = REPO_ROOT / "shapes" / "core_shapes.ttl"
SAMPLE_DATA = REPO_ROOT / "benchmarks" / "datasets" / "sample_data.ttl"
QUERIES_DIR = REPO_ROOT / "benchmarks" / "queries"
EXPECTED_DIR = REPO_ROOT / "benchmarks" / "expected_results"

CORE = "https://ontology.example/core#"


def type_closure(graph: Graph, subject: URIRef) -> set[URIRef]:
    """All rdf:type classes of a subject, expanded transitively over rdfs:subClassOf."""
    direct = set(graph.objects(subject, RDF.type))
    closed: set[URIRef] = set(direct)
    frontier = list(direct)
    while frontier:
        cls = frontier.pop()
        for parent in graph.objects(cls, RDFS.subClassOf):
            parent = URIRef(parent) if not isinstance(parent, URIRef) else parent
            if parent not in closed:
                closed.add(parent)
                frontier.append(parent)
    return closed


@pytest.fixture(scope="session")
def core_graph() -> Graph:
    g = Graph()
    g.parse(CORE_ONTOLOGY.as_posix(), format="turtle")
    return g


@pytest.fixture(scope="session")
def benchmark_graph() -> Graph:
    """Every ontology module + dataset, via the shared CQ runner (single truth)."""
    return cq_runner.load_benchmark_graph()
