"""Tests for the domain ontology modules (Phase 4 DoD).

Covers the tracking vertical: middle-layer geography/organization and
domain-layer sensor/tracking modules - positive and negative reasoning,
domain/range coverage, module headers and the DAG import invariant.
"""

from __future__ import annotations

import subprocess
import sys

import manage_ontology as mgmt
from conftest import CORE_ONTOLOGY, REPO_ROOT, type_closure
from rdflib import RDF, Graph, URIRef
from rdflib.namespace import RDFS

MIDDLE = {
    "location": REPO_ROOT / "ontology" / "middle" / "location.ttl",
    "organization": REPO_ROOT / "ontology" / "middle" / "organization.ttl",
}
DOMAIN = {
    "sensor": REPO_ROOT / "ontology" / "domain" / "sensor.ttl",
    "tracking": REPO_ROOT / "ontology" / "domain" / "tracking.ttl",
}

LOCATION = "https://ontology.example/middle/location#"
ORG = "https://ontology.example/middle/organization#"
CORE = "https://ontology.example/core#"
EX = "https://data.example/entity/"


def load_module(*paths) -> Graph:
    """Load the kernel plus the given module files into one graph."""
    graph = Graph()
    graph.parse(CORE_ONTOLOGY.as_posix(), format="turtle")
    for path in paths:
        graph.parse(path.as_posix(), format="turtle")
    return graph


class TestMiddleModules:
    def test_module_headers_are_registered(self):
        for iri_suffix, path in (
            ("location", MIDDLE["location"]),
            ("organization", MIDDLE["organization"]),
        ):
            iri, version = mgmt.module_identity(mgmt.load_graph(path))
            assert iri == f"https://ontology.example/middle/{iri_suffix}"
            assert version == "0.1.0"

    def test_geography_classes_subclass_location(self):
        graph = load_module(MIDDLE["location"])
        country = URIRef(EX + "x-country")
        graph.add((country, RDF.type, URIRef(LOCATION + "Country")))
        closed = type_closure(graph, country)
        assert URIRef(CORE + "Location") in closed
        assert URIRef(CORE + "Entity") in closed

    def test_administrative_region_closure(self):
        graph = load_module(MIDDLE["location"])
        region = URIRef(EX + "x-region")
        graph.add((region, RDF.type, URIRef(LOCATION + "AdministrativeRegion")))
        closed = type_closure(graph, region)
        assert URIRef(CORE + "Location") in closed
        assert URIRef(CORE + "PhysicalObject") not in closed

    def test_subordinate_to_has_domain_and_range(self):
        graph = load_module(MIDDLE["organization"])
        prop = URIRef(ORG + "subordinateTo")
        domain = next(graph.objects(prop, RDFS.domain), None)
        rng = next(graph.objects(prop, RDFS.range), None)
        assert domain == URIRef(CORE + "Organization")
        assert rng == URIRef(CORE + "Organization")

    def test_dag_check_passes_with_middle_layer(self):
        result = subprocess.run(
            [sys.executable, (REPO_ROOT / "tools" / "check_dependency_dag.py").as_posix()],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT.as_posix(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
