"""Regression tests for predicate, provenance and identity semantics."""

from __future__ import annotations

from conftest import REPO_ROOT
from ontology_utils import materialize_type_closure
from pyshacl import validate
from rdflib import RDF, Graph, URIRef
from rdflib.namespace import OWL, RDFS

from foundry.assertions import is_known_predicate, predicate_compatibility
from foundry.events import make_event
from foundry.identity_rdf import entity_identity_to_rdf, identifier_to_rdf
from foundry.namespaces import CORE_ONTOLOGY_NS, IDENTITY_MIDDLE_NS
from foundry.provenance import normalize_evidence, normalize_source_id


def _ontology() -> Graph:
    graph = Graph()
    for path in (
        REPO_ROOT / "ontology/core/core.ttl",
        REPO_ROOT / "ontology/middle/identity.ttl",
        REPO_ROOT / "ontology/middle/assertion.ttl",
    ):
        graph.parse(path.as_posix(), format="turtle")
    return graph


class TestPredicateCompatibility:
    def test_location_predicate_accepts_only_compatible_ranges(self):
        graph = _ontology()
        located_at = CORE_ONTOLOGY_NS + "locatedAt"
        assert (
            predicate_compatibility(
                graph,
                located_at,
                subject_class_iri=CORE_ONTOLOGY_NS + "Platform",
                object_kind="location",
                object_class_iri=CORE_ONTOLOGY_NS + "Location",
            )
            is None
        )
        assert "range" in predicate_compatibility(
            graph,
            located_at,
            subject_class_iri=CORE_ONTOLOGY_NS + "Platform",
            object_kind="entity",
            object_class_iri=CORE_ONTOLOGY_NS + "Organization",
        )

    def test_membership_predicate_rejects_wrong_subject_or_object(self):
        graph = _ontology()
        member_of = CORE_ONTOLOGY_NS + "memberOf"
        assert "domain" in predicate_compatibility(
            graph,
            member_of,
            subject_class_iri=CORE_ONTOLOGY_NS + "Platform",
            object_kind="entity",
            object_class_iri=CORE_ONTOLOGY_NS + "Organization",
        )
        assert "range" in predicate_compatibility(
            graph,
            member_of,
            subject_class_iri=CORE_ONTOLOGY_NS + "Organization",
            object_kind="location",
            object_class_iri=CORE_ONTOLOGY_NS + "Location",
        )

    def test_language_tagged_literal_does_not_match_xsd_string_range(self):
        graph = _ontology()
        result = predicate_compatibility(
            graph,
            CORE_ONTOLOGY_NS + "name",
            subject_class_iri=CORE_ONTOLOGY_NS + "Person",
            object_kind="literal",
            literal_language="vi",
        )
        assert result is not None
        assert "range" in result

    def test_unregistered_incomplete_and_projection_predicates_fail_closed(self):
        graph = _ontology()
        incomplete = URIRef("https://example.org/vocab#incomplete")
        graph.add((incomplete, RDF.type, OWL.ObjectProperty))
        graph.add((incomplete, RDFS.domain, URIRef(CORE_ONTOLOGY_NS + "Entity")))
        assert not is_known_predicate(graph, str(incomplete))
        assert not is_known_predicate(graph, CORE_ONTOLOGY_NS + "currentLocation")
        assert not is_known_predicate(graph, "https://example.org/vocab#unknown")


class TestCanonicalProvenance:
    def test_source_ids_and_inline_evidence_share_one_canonical_source(self):
        normalized = normalize_source_id(" AIS feed ")
        sources, evidence = normalize_evidence(
            ["AIS feed"],
            [
                {
                    "source_id": "urn:source:AIS%20feed",
                    "locator": "record=42",
                    "span": {"start": 4, "end": 9},
                }
            ],
        )
        assert normalized == "urn:source:AIS%20feed"
        assert sources == (normalized,)
        assert evidence == (
            {
                "source_id": normalized,
                "locator": "record=42",
                "span": {"start": 4, "end": 9, "unit": "character"},
            },
        )


class TestFirstClassIdentifiers:
    def test_entity_external_ids_project_to_identifier_resources(self):
        entity_id = "urn:world:entity:1"
        event = make_event(
            "EntityCreated",
            {
                "entity_id": entity_id,
                "entity_type": "Organization",
                "name": "Coast Guard Region 7",
                "name_aliases": [],
                "external_ids": [{"source": "wikidata", "external_id": "Q700"}],
                "source_id": "registry-import",
                "confidence": 1.0,
            },
        )
        graph = Graph()
        graph.parse((REPO_ROOT / "ontology/core/core.ttl").as_posix(), format="turtle")
        graph.parse((REPO_ROOT / "ontology/middle/identity.ttl").as_posix(), format="turtle")
        identifiers = entity_identity_to_rdf(graph, event)
        identifiers_again = entity_identity_to_rdf(Graph(), event)
        assert identifiers == identifiers_again
        assert len(identifiers) == 1
        identifier = identifiers[0]
        assert (identifier, RDF.type, URIRef(IDENTITY_MIDDLE_NS + "Identifier")) in graph
        assert (identifier, URIRef(IDENTITY_MIDDLE_NS + "identifies"), URIRef(entity_id)) in graph
        assert (
            str(next(graph.objects(identifier, URIRef(IDENTITY_MIDDLE_NS + "identifierScheme"))))
            == "wikidata"
        )
        assert (
            str(next(graph.objects(identifier, URIRef(IDENTITY_MIDDLE_NS + "identifierValue"))))
            == "Q700"
        )
        materialize_type_closure(graph)
        shapes = Graph()
        shapes.parse((REPO_ROOT / "shapes/identity_shapes.ttl").as_posix(), format="turtle")
        conforms, _, report = validate(graph, shapes, inference="none", advanced=True)
        assert conforms, report

    def test_identifier_iri_includes_its_scheme(self):
        graph = Graph()
        first = identifier_to_rdf(graph, entity_id="urn:entity:1", scheme="registry-a", value="42")
        second = identifier_to_rdf(graph, entity_id="urn:entity:2", scheme="registry-b", value="42")
        assert first != second
