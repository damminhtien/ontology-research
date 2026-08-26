"""Shared RDF utilities used by both the CLI validators and the test suite."""

from __future__ import annotations

from rdflib import RDF, RDFS, Graph, URIRef


def materialize_type_closure(graph: Graph) -> int:
    """Expand asserted rdf:type triples transitively over rdfs:subClassOf.

    Adds `(subject, rdf:type, superclass)` triples in place so that SHACL
    `sh:class` constraints accept subclass instances without requiring full
    OWL reasoning. Only URIRef superclasses are followed (blank-node class
    expressions such as owl:restrictions are ignored).

    Returns the number of triples added.
    """
    direct_parents: dict[URIRef, set[URIRef]] = {}
    for sub, sup in graph.subject_objects(RDFS.subClassOf):
        if isinstance(sub, URIRef) and isinstance(sup, URIRef):
            direct_parents.setdefault(sub, set()).add(sup)

    def all_supers(cls: URIRef, seen: frozenset[URIRef]) -> set[URIRef]:
        found: set[URIRef] = set()
        for parent in direct_parents.get(cls, ()):
            if parent in seen:
                continue
            found.add(parent)
            found |= all_supers(parent, seen | {parent})
        return found

    added: list[tuple] = []
    for subject, cls in list(graph.subject_objects(RDF.type)):
        if not isinstance(subject, URIRef):
            continue
        for sup in all_supers(cls, frozenset({cls})):
            added.append((subject, RDF.type, sup))

    for triple in added:
        graph.add(triple)
    return len(added)
