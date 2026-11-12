"""RDF projection for first-class identity identifiers."""

from __future__ import annotations

import hashlib

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from foundry.events import SemanticEvent
from foundry.namespaces import CORE_ONTOLOGY_NS, IDENTITY_MIDDLE_NS
from foundry.provenance import add_sources_to_rdf


def identifier_to_rdf(graph: Graph, *, entity_id: str, scheme: str, value: str) -> URIRef:
    """Add one stable Identifier resource linked to its canonical entity."""
    if not scheme.strip() or not value.strip():
        raise ValueError("identifier scheme and value must be non-empty")
    digest = hashlib.sha256(f"{scheme}\0{value}".encode()).hexdigest()
    identifier = URIRef(f"urn:identifier:{digest}")
    graph.add((identifier, RDF.type, URIRef(IDENTITY_MIDDLE_NS + "Identifier")))
    graph.add((identifier, URIRef(IDENTITY_MIDDLE_NS + "identifies"), URIRef(entity_id)))
    graph.add(
        (
            identifier,
            URIRef(IDENTITY_MIDDLE_NS + "identifierScheme"),
            Literal(scheme, datatype=XSD.string),
        )
    )
    graph.add(
        (
            identifier,
            URIRef(IDENTITY_MIDDLE_NS + "identifierValue"),
            Literal(value, datatype=XSD.string),
        )
    )
    return identifier


def entity_identity_to_rdf(graph: Graph, event: SemanticEvent) -> tuple[URIRef, ...]:
    """Map entity identity events and return the Identifier nodes they add."""
    if event.event_type == "EntityCreated":
        entity_id = event.payload["entity_id"]
        entity = URIRef(entity_id)
        graph.add((entity, RDF.type, URIRef(CORE_ONTOLOGY_NS + event.payload["entity_type"])))
        graph.add(
            (
                entity,
                URIRef(CORE_ONTOLOGY_NS + "name"),
                Literal(event.payload["name"], datatype=XSD.string),
            )
        )
        for alias in event.payload.get("name_aliases") or ():
            graph.add(
                (
                    entity,
                    URIRef(CORE_ONTOLOGY_NS + "alias"),
                    Literal(alias, datatype=XSD.string),
                )
            )
        if event.payload.get("source_id"):
            add_sources_to_rdf(graph, entity, [event.payload["source_id"]])
        bindings = event.payload.get("external_ids") or ()
        return tuple(
            identifier_to_rdf(
                graph,
                entity_id=entity_id,
                scheme=binding["source"],
                value=binding["external_id"],
            )
            for binding in bindings
        )
    if event.event_type == "ExternalIdBound":
        return (
            identifier_to_rdf(
                graph,
                entity_id=event.payload["entity_id"],
                scheme=event.payload["source"],
                value=event.payload["external_id"],
            ),
        )
    raise ValueError(f"unsupported identity event {event.event_type!r}")
