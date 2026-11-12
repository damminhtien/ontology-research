"""Canonical source and inline evidence references."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from urllib.parse import quote, urlparse

SOURCE_URN_PREFIX = "urn:source:"


def normalize_source_id(value: str) -> str:
    """Return a stable absolute IRI for a source identifier.

    Existing absolute identifiers retain their IRI. Short source-system names
    become percent-encoded ``urn:source:`` identifiers so every lane emits the
    same RDF term for the same source reference.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source_id must be a non-empty string")
    source_id = value.strip()
    if urlparse(source_id).scheme:
        return source_id
    return SOURCE_URN_PREFIX + quote(source_id, safe="-._~")


def normalize_source_ids(values: Iterable[str]) -> tuple[str, ...]:
    """Normalize and deduplicate source references while preserving order."""
    return tuple(dict.fromkeys(normalize_source_id(value) for value in values))


def normalize_evidence(
    source_ids: Iterable[str],
    evidence: Iterable[Mapping[str, object]] | None = None,
) -> tuple[tuple[str, ...], tuple[dict[str, object], ...]]:
    """Normalize inline evidence references and their source IDs.

    An evidence item is a source ID plus an optional source-local locator or
    character/page span. It is data attached to the assertion, not an ontology
    class. Sources introduced by evidence are included in ``source_ids``.
    """
    sources = list(normalize_source_ids(source_ids))
    entries: list[dict[str, object]] = []
    for raw in evidence or ():
        source_id = normalize_source_id(str(raw.get("source_id", "")))
        if source_id not in sources:
            sources.append(source_id)
        locator = raw.get("locator")
        span = raw.get("span")
        if locator is not None and not isinstance(locator, str):
            raise ValueError("evidence locator must be a string")
        if span is not None:
            if not isinstance(span, Mapping):
                raise ValueError("evidence span must be an object")
            start, end = span.get("start"), span.get("end")
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
                raise ValueError("evidence span requires integer start <= end")
            unit = span.get("unit", "character")
            if not isinstance(unit, str) or not unit.strip():
                raise ValueError("evidence span unit must be a non-empty string")
            span = {"start": start, "end": end, "unit": unit.strip()}
        entry = {"source_id": source_id, "locator": locator, "span": span}
        if entry not in entries:
            entries.append(entry)
    if not entries:
        entries = [{"source_id": source_id, "locator": None, "span": None} for source_id in sources]
    return tuple(sources), tuple(entries)


def add_sources_to_rdf(graph, subject, source_ids: Iterable[str]) -> None:
    """Add normalized ``core:hasSource`` links and named Source resources."""
    from rdflib import Literal, URIRef
    from rdflib.namespace import RDF, XSD

    from foundry.namespaces import CORE_ONTOLOGY_NS

    has_source = URIRef(CORE_ONTOLOGY_NS + "hasSource")
    source_class = URIRef(CORE_ONTOLOGY_NS + "Source")
    source_name = URIRef(CORE_ONTOLOGY_NS + "name")
    for source_id in normalize_source_ids(source_ids):
        source = URIRef(source_id)
        graph.add((subject, has_source, source))
        graph.add((source, RDF.type, source_class))
        graph.add((source, source_name, Literal(source_id, datatype=XSD.string)))
