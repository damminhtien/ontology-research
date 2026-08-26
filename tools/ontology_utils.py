"""Shared RDF utilities used by both the CLI validators and the test suite."""

from __future__ import annotations

import json
from pathlib import Path

from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.namespace import OWL


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


def find_module_files(roots: tuple[str, ...] = ("ontology",)) -> list[Path]:
    """Return all Turtle ontology module files under the given directory roots."""
    base = Path(__file__).resolve().parent.parent
    files: list[Path] = []
    for root in roots:
        root_path = base / root
        if root_path.exists():
            files.extend(sorted(p for p in root_path.rglob("*.ttl") if p.is_file()))
    return files


def _first_literal(graph: Graph, subject: URIRef, predicate: URIRef) -> str:
    values = sorted(str(o) for o in graph.objects(subject, predicate))
    return values[0] if values else ""


def describe_classes(graph: Graph) -> dict[str, dict]:
    """Describe every OWL class declared in ``graph``.

    Returns a mapping of class IRI to a descriptor with keys ``label``,
    ``comment``, ``parents`` (direct superclass IRIs) and ``properties``
    (populated by :func:`attach_properties`).
    """
    classes: dict[str, dict] = {}
    for cls in graph.subjects(RDF.type, OWL.Class):
        if not isinstance(cls, URIRef):
            continue
        classes[str(cls)] = {
            "label": _first_literal(graph, cls, RDFS.label),
            "comment": _first_literal(graph, cls, RDFS.comment),
            "parents": sorted(
                str(p) for p in graph.objects(cls, RDFS.subClassOf) if isinstance(p, URIRef)
            ),
            "properties": [],
        }
    return classes


def describe_properties(graph: Graph) -> list[dict]:
    """Describe every object/data property declared in ``graph``."""
    properties: list[dict] = []
    seen: set[str] = set()
    for prop_type, kind in ((OWL.ObjectProperty, "object"), (OWL.DatatypeProperty, "data")):
        for prop in graph.subjects(RDF.type, prop_type):
            if not isinstance(prop, URIRef) or str(prop) in seen:
                continue
            seen.add(str(prop))
            properties.append(
                {
                    "iri": str(prop),
                    "kind": kind,
                    "label": _first_literal(graph, prop, RDFS.label),
                    "domain": str(next(iter(graph.objects(prop, RDFS.domain)), "")),
                    "range": str(next(iter(graph.objects(prop, RDFS.range)), "")),
                }
            )
    return sorted(properties, key=lambda p: p["iri"])


def attach_properties(classes: dict[str, dict], properties: list[dict]) -> None:
    """Group property descriptors under their domain class in-place."""
    by_iri = classes
    for prop in properties:
        entry = by_iri.get(prop["domain"])
        if entry is not None:
            entry["properties"].append(
                {
                    "name": local_name(prop["iri"]),
                    "kind": prop["kind"],
                    "range": prop["range"],
                    "label": prop["label"],
                }
            )


def local_name(iri: str) -> str:
    """Return the local part of an IRI (after the last '#' or '/')."""
    return iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def dump_json(data: object) -> str:
    """Stable JSON serialization used by generated artifacts."""
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
