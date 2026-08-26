"""Unit tests for the semantic-core kernel (Phase 1 contract).

Enforces the kernel size budget (15-25 classes, 30-50 predicates),
full domain/range coverage, positive subclass reasoning,
and the negative 'role != classification' invariant.
"""

from __future__ import annotations

import re

import pytest
from conftest import CORE, CORE_ONTOLOGY, type_closure
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

core = Namespace(CORE)
DCT_VERSION = URIRef("http://purl.org/dc/terms/version")


def _all_classes(g: Graph) -> set:
    return set(g.subjects(RDF.type, OWL.Class))


def _all_object_properties(g: Graph) -> set:
    return set(g.subjects(RDF.type, OWL.ObjectProperty))


def _all_datatype_properties(g: Graph) -> set:
    return set(g.subjects(RDF.type, OWL.DatatypeProperty))


class TestKernelContract:
    def test_ontology_declares_semver_version(self, core_graph):
        versions = list(core_graph.objects(None, DCT_VERSION))
        assert versions, "ontology must declare dcterms:version"
        for v in versions:
            assert SEMVER_RE.match(str(v)), f"version {v} is not SemVer"

    def test_class_count_within_kernel_budget(self, core_graph):
        n = len(_all_classes(core_graph))
        assert 15 <= n <= 25, f"kernel must keep 15-25 classes, found {n}"

    def test_predicate_count_within_kernel_budget(self, core_graph):
        total = len(_all_object_properties(core_graph)) + len(_all_datatype_properties(core_graph))
        assert 30 <= total <= 50, f"kernel must keep 30-50 predicates, found {total}"

    def test_every_property_declares_domain_and_range(self, core_graph):
        props = _all_object_properties(core_graph) | _all_datatype_properties(core_graph)
        assert props, "kernel must define predicates"
        for p in sorted(props, key=str):
            assert (p, RDFS.domain, None) in core_graph, f"{p} missing rdfs:domain"
            assert (p, RDFS.range, None) in core_graph, f"{p} missing rdfs:range"

    def test_no_domain_terms_leak_into_core(self, core_graph):
        forbidden = {
            "Tank",
            "Radar",
            "Aircraft",
            "Company",
            "Province",
            "Missile",
            "Sensor",
            "VietnameseCountry",
        }
        labels = {str(o).lower() for o in core_graph.objects(None, RDFS.label)}
        for term in forbidden:
            assert term.lower() not in labels, f"domain concept '{term}' leaked into core"

    def test_core_does_not_import_anything(self, core_graph):
        imports = list(core_graph.objects(None, OWL.imports))
        assert not imports, "semantic kernel must not import other modules"


class TestReasoning:
    @pytest.fixture()
    def reasoning_graph(self) -> tuple[Graph, URIRef]:
        g = Graph()
        g.parse(CORE_ONTOLOGY.as_posix(), format="turtle")
        f16 = URIRef("https://data.example/entity/f16")
        g.add((f16, RDF.type, core.Platform))
        return g, f16

    def test_positive_subclass_inference(self, reasoning_graph):
        g, f16 = reasoning_graph
        expected_chain = [
            core.Artifact,
            core.PhysicalObject,
            core.Entity,
        ]
        closed = type_closure(g, f16)
        for cls in expected_chain:
            assert cls in closed, f"expected {cls} inferred for Platform instance"

    def test_role_playing_does_not_reclassify(self, reasoning_graph):
        g, _ = reasoning_graph
        person = URIRef("https://data.example/entity/person-a")
        role = URIRef("https://data.example/entity/role-commander")
        g.add((person, RDF.type, core.Person))
        g.add((person, core.hasRole, role))

        closed = type_closure(g, person)
        assert role not in closed, (
            "playing a Role must NOT infer rdf:type of the role (negative reasoning invariant)"
        )
