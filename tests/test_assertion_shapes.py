"""SHACL mapping tests for the Document → Assertion model (§4.2).

The assertion event contract now has an ontology presence:
ontology/middle/assertion.ttl defines the classes and property mappings,
shapes/assertion_shapes.ttl mirrors the write-path validation in
foundry/assertions.py, and foundry/assertions.py maps AssertionMade /
DocumentRegistered events to RDF. These tests verify the DAG invariant with
the new module, the happy-path mapping conformance, and the negative cases.
"""

from __future__ import annotations

import subprocess
import sys

from conftest import REPO_ROOT
from ontology_utils import materialize_type_closure
from pyshacl import validate
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.namespace import XSD

from foundry.assertions import (
    assertion_to_rdf,
    document_to_rdf,
    make_assertion,
    register_document,
    supersede_assertion,
)
from foundry.events import EventLog
from foundry.namespaces import ASSERTION_MIDDLE_NS, CORE_ONTOLOGY_NS

ASSERTION = ASSERTION_MIDDLE_NS
CORE = CORE_ONTOLOGY_NS
ENTITY = "urn:world:entity:" + "7" * 32
LOCATION = "urn:world:location:" + "8" * 32
DOC = "urn:doc:" + "9" * 32
INSTANT = "2026-08-01T00:00:00Z"


def _shapes() -> Graph:
    g = Graph()
    g.parse((REPO_ROOT / "shapes" / "assertion_shapes.ttl").as_posix(), format="turtle")
    return g


def _run_shacl(data: Graph) -> tuple[bool, str]:
    materialize_type_closure(data)
    conforms, _, results_text = validate(
        data_graph=data,
        shacl_graph=_shapes(),
        inference="none",
        advanced=True,
    )
    return conforms, results_text


def _seed_graph() -> Graph:
    g = Graph()
    g.parse((REPO_ROOT / "ontology" / "core" / "core.ttl").as_posix(), format="turtle")
    g.parse((REPO_ROOT / "ontology" / "middle" / "assertion.ttl").as_posix(), format="turtle")
    # the subject and location must exist as typed entities in the graph
    g.add((URIRef(ENTITY), RDF.type, URIRef(CORE + "Organization")))
    g.add((URIRef(LOCATION), RDF.type, URIRef(CORE + "Location")))
    return g


def _add_document(g: Graph) -> URIRef:
    """Add a conforming Document node, so a negative test isolates its own violation."""
    node = URIRef(DOC)
    g.add((node, RDF.type, URIRef(ASSERTION + "Document")))
    g.add((node, URIRef(CORE + "name"), Literal("Weekly report", datatype=XSD.string)))
    return node


def _add_assertion_skeleton(g: Graph, node: URIRef) -> URIRef:
    """Minimal conforming assertion except for the property under test."""
    g.add((node, RDF.type, URIRef(ASSERTION + "Assertion")))
    g.add((node, URIRef(CORE + "describes"), URIRef(ENTITY)))
    g.add((node, URIRef(CORE + "validFrom"), Literal(INSTANT, datatype=XSD.dateTime)))
    g.add((node, URIRef(CORE + "hasSource"), _add_document(g)))
    return node


def _mapped_log(tmp_path):
    """Document + assertion events on a real log, mapped later in tests."""
    log = EventLog(tmp_path / "events.jsonl")
    doc = register_document(
        log=log,
        uri="https://example.org/report-1",
        title="Weekly report",
        source_system="crawler",
    )
    made = make_assertion(
        log=log,
        subject_id=ENTITY,
        predicate="locatedAt",
        object_kind="location",
        object_value=LOCATION,
        valid_from=INSTANT,
        source_ids=[doc.payload["document_id"]],
        confidence=0.9,
        supersedes=None,
    )
    return log, doc, made


class TestModuleRegistration:
    def test_dag_check_passes_with_assertion_module(self):
        result = subprocess.run(
            [sys.executable, (REPO_ROOT / "tools" / "check_dependency_dag.py").as_posix()],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT.as_posix(),
        )
        assert result.returncode == 0, result.stdout + result.stderr


class TestShaclConformance:
    def test_happy_path_conforms(self, tmp_path):
        _log, doc, made = _mapped_log(tmp_path)
        g = _seed_graph()
        document_to_rdf(g, doc)
        assertion_to_rdf(g, made)
        conforms, report = _run_shacl(g)
        assert conforms, f"mapped assertion must conform:\n{report}"

    def test_predicate_is_mapped_as_a_relation_iri(self, tmp_path):
        """A bare core name on the write path becomes the property IRI in RDF."""
        _log, doc, made = _mapped_log(tmp_path)
        g = _seed_graph()
        document_to_rdf(g, doc)
        node = assertion_to_rdf(g, made)
        assert made.payload["predicate_iri"] == CORE + "locatedAt"
        assert (node, URIRef(ASSERTION + "predicate"), URIRef(CORE + "locatedAt")) in g

    def test_different_predicates_map_to_different_rdf(self, tmp_path):
        """Acceptance: locatedAt and memberOf are two statements, not one.

        Before the relation was carried as an IRI the mapping dropped it from
        the graph, so the two assertions over the same subject and object
        produced identical RDF.
        """
        log, doc, located = _mapped_log(tmp_path)
        member = make_assertion(
            log=log,
            subject_id=ENTITY,
            predicate="memberOf",
            object_kind="location",
            object_value=LOCATION,
            valid_from=INSTANT,
            source_ids=[doc.payload["document_id"]],
            confidence=0.9,  # same as the locatedAt assertion: only the relation differs
        )
        g = _seed_graph()
        document_to_rdf(g, doc)
        located_node = assertion_to_rdf(g, located)
        member_node = assertion_to_rdf(g, member)

        def statement(node: URIRef) -> set:
            return set(g.predicate_objects(node))

        assert statement(located_node) - statement(member_node) == {
            (URIRef(ASSERTION + "predicate"), URIRef(CORE + "locatedAt"))
        }
        assert statement(member_node) - statement(located_node) == {
            (URIRef(ASSERTION + "predicate"), URIRef(CORE + "memberOf"))
        }
        conforms, report = _run_shacl(g)
        assert conforms, f"both statements must conform together:\n{report}"

    def test_literal_object_conforms(self, tmp_path):
        _log, doc, _made = _mapped_log(tmp_path)
        made = make_assertion(
            log=_log,
            subject_id=ENTITY,
            predicate="hasNickname",
            object_kind="literal",
            object_value="Alpha",
            valid_from=INSTANT,
            source_ids=[doc.payload["document_id"]],
        )
        g = _seed_graph()
        document_to_rdf(g, doc)
        node = assertion_to_rdf(g, made)
        # a literal is a value on the assertion, never a name of the assertion
        assert (
            node,
            URIRef(ASSERTION + "literalValue"),
            Literal("Alpha", datatype=XSD.string),
        ) in g
        assert not list(g.objects(node, URIRef(CORE + "name")))
        assert (node, URIRef(ASSERTION + "predicate"), URIRef(CORE + "hasNickname")) in g
        conforms, report = _run_shacl(g)
        assert conforms, f"literal-object assertion must conform:\n{report}"

    def test_supersede_link_conforms(self, tmp_path):
        _log, doc, made = _mapped_log(tmp_path)
        second = make_assertion(
            log=_log,
            subject_id=ENTITY,
            predicate="locatedAt",
            object_kind="location",
            object_value=LOCATION,
            valid_from="2026-08-02T00:00:00Z",
            source_ids=[doc.payload["document_id"]],
            supersedes=made.payload["assertion_id"],
        )
        supersede_assertion(log=_log, assertion_id=made.payload["assertion_id"])
        g = _seed_graph()
        document_to_rdf(g, doc)
        assertion_to_rdf(g, made)
        assertion_to_rdf(g, second)
        conforms, report = _run_shacl(g)
        assert conforms, f"supersede chain must conform:\n{report}"

    def test_assertion_citing_unmapped_document_rejected(self, tmp_path):
        _log, _doc, made = _mapped_log(tmp_path)
        g = _seed_graph()
        assertion_to_rdf(g, made)  # hasSource cites a Document never mapped
        conforms, _report = _run_shacl(g)
        assert not conforms, "assertion citing an unmapped Document must be rejected"

    def test_confidence_outside_range_rejected(self):
        g = _seed_graph()
        node = _add_assertion_skeleton(g, URIRef("urn:assert:bad"))
        g.add((node, URIRef(ASSERTION + "predicate"), URIRef(CORE + "locatedAt")))
        g.add((node, URIRef(ASSERTION + "hasObject"), URIRef(LOCATION)))
        g.add((node, URIRef(CORE + "hasConfidence"), Literal("1.50", datatype=XSD.decimal)))
        conforms, _report = _run_shacl(g)
        assert not conforms, "confidence outside [0,1] must be rejected"

    def test_missing_subject_and_validfrom_rejected(self):
        g = _seed_graph()
        node = URIRef("urn:assert:bad")
        g.add((node, RDF.type, URIRef(ASSERTION + "Assertion")))
        g.add((node, URIRef(ASSERTION + "predicate"), URIRef(CORE + "locatedAt")))
        g.add((node, URIRef(ASSERTION + "hasObject"), URIRef(LOCATION)))
        g.add((node, URIRef(CORE + "hasSource"), _add_document(g)))
        conforms, _report = _run_shacl(g)
        assert not conforms, "assertion without subject and validFrom must be rejected"

    def test_missing_predicate_rejected(self):
        g = _seed_graph()
        node = _add_assertion_skeleton(g, URIRef("urn:assert:bad"))
        g.add((node, URIRef(ASSERTION + "hasObject"), URIRef(LOCATION)))
        conforms, report = _run_shacl(g)
        assert not conforms, "assertion without a relation IRI must be rejected"
        assert "relation IRI" in report

    def test_missing_object_rejected(self):
        g = _seed_graph()
        node = _add_assertion_skeleton(g, URIRef("urn:assert:bad"))
        g.add((node, URIRef(ASSERTION + "predicate"), URIRef(CORE + "locatedAt")))
        conforms, _report = _run_shacl(g)
        assert not conforms, "assertion stating no object must be rejected"

    def test_document_without_name_rejected(self):
        g = _seed_graph()
        g.add((URIRef(DOC), RDF.type, URIRef(ASSERTION + "Document")))
        conforms, _report = _run_shacl(g)
        assert not conforms, "document without a name must be rejected"
