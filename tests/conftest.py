"""Shared fixtures for the semantic-core test suite."""

from __future__ import annotations

import sys
from pathlib import Path

# Make shared helpers in tools/ importable from the test suite.
TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if TOOLS_DIR.as_posix() not in sys.path:
    sys.path.insert(0, TOOLS_DIR.as_posix())

import pytest
from rdflib import RDF, RDFS, Graph, URIRef

REPO_ROOT = Path(__file__).resolve().parents[1]
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
    """Ontology + seed dataset loaded together (no inference materialized)."""
    g = Graph()
    g.parse(CORE_ONTOLOGY.as_posix(), format="turtle")
    g.parse(SAMPLE_DATA.as_posix(), format="turtle")
    return g
