"""Document and Assertion model (docs/architecture.md §4.2).

Separates "the fact that we learned something" (an event) from "a statement
about the world" (an assertion — an ``assertion:Assertion``, i.e. an
``InformationObject``, never an event). Every assertion is a first-class record
with its own stable id, generic provenance and bi-temporal timestamps:

    assertion_id (urn:assert:<hex>)   stable handle for correct/retract
    subject_id                        canonical entity the assertion is about
    predicate_iri                     absolute relation IRI (core:locatedAt)
    object                            {kind: entity|location|literal, value, ...}
    valid_from / valid_to             valid time (when it holds in the world)
    source_ids[]                      provenance (documents / source records)
    confidence                        optional 0..1

``predicate_iri`` is what the RDF mapping needs: the relation is a node in the
graph, so ``locatedAt`` and ``memberOf`` about the same subject and object are
two distinct statements instead of one. The write path takes an *absolute* IRI
only — a bare local name is rejected, never resolved
(:func:`foundry.namespaces.require_absolute_iri`), because resolving one would
mint a relation the ontology may never have declared. Historical v2 records
that carried a bare name are mapped by the log upcaster through an explicit
table (``foundry.namespaces.LEGACY_PREDICATE_IRIS``).

A literal object keeps its type: the payload carries ``datatype_iri`` or
``language`` (exactly one non-null; ``xsd:string`` by default), so ``"250"``
does not silently become a string in RDF and a Vietnamese label keeps its tag.

Validation is split in two layers: :func:`build_assertion` checks *syntax*
(absolute IRIs, well-formed object, timestamp, confidence) and the ingestion
pipeline checks the *semantic* contract (``is_known_predicate`` against the
registered ontology); SHACL then enforces the structural RDF constraints.

Corrections never rewrite anything (ADR-0002): superseding an assertion emits
an ``AssertionSuperseded`` event that references the original id, and the read
model keeps the full assertion ledger with statuses. ``LocationObserved``-style
events keep working; the assertion layer is the target model new fact types
should build on.

Ontology presence: ``ontology/middle/assertion.ttl`` declares the classes and
properties, ``shapes/assertion_shapes.ttl`` mirrors the write-path validation,
and :func:`assertion_to_rdf` maps the events onto that model.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, XSD

from foundry.events import (
    EVENT_TYPE_ASSERTION_MADE,
    EVENT_TYPE_ASSERTION_SUPERSEDED,
    EVENT_TYPE_DOCUMENT_REGISTERED,
    EventLog,
    SemanticEvent,
    make_event,
    utc_now_iso,
)
from foundry.identity import IdentityService
from foundry.namespaces import (
    ASSERTION_MIDDLE_NS,
    CORE_ONTOLOGY_NS,
    DEFAULT_LITERAL_DATATYPE,
    new_assertion_id,
    new_document_id,
    require_absolute_iri,
)
from foundry.readmodel import parse_instant

OBJECT_KINDS = ("entity", "location", "literal")


def register_document(
    *,
    log: EventLog,
    uri: str,
    title: str,
    source_system: str,
    content_ref: str | None = None,
) -> SemanticEvent:
    """Record a provenance source document.

    Raises:
        ValueError: On empty uri, title or source_system.
    """
    if not uri.strip():
        raise ValueError("document uri must be non-empty")
    if not title.strip():
        raise ValueError("document title must be non-empty")
    if not source_system.strip():
        raise ValueError("source_system must be non-empty")
    event = make_event(
        EVENT_TYPE_DOCUMENT_REGISTERED,
        {
            "document_id": new_document_id(),
            "uri": uri,
            "title": title,
            "source_system": source_system,
            "content_ref": content_ref,
            "registered_at": utc_now_iso(),
        },
    )
    log.append(event)
    return event


def _find_assertion_event(log: EventLog, assertion_id: str) -> SemanticEvent | None:
    for event in log.read_all():
        if (
            event.event_type == EVENT_TYPE_ASSERTION_MADE
            and event.payload.get("assertion_id") == assertion_id
        ):
            return event
    return None


def is_superseded(log: EventLog, assertion_id: str) -> bool:
    """True when the log records an ``AssertionSuperseded`` for this id."""
    return any(
        event.event_type == EVENT_TYPE_ASSERTION_SUPERSEDED
        and event.payload.get("assertion_id") == assertion_id
        for event in log.read_all()
    )


def build_assertion(
    *,
    subject_id: str,
    predicate_iri: str,
    object_kind: str,
    object_value: str,
    valid_from: str,
    source_ids: list[str],
    confidence: float | None = None,
    supersedes: str | None = None,
    literal_datatype_iri: str | None = None,
    literal_language: str | None = None,
    identity: IdentityService | None = None,
    known_assertions: Collection[str],
) -> SemanticEvent:
    """Validate one statement and build its ``AssertionMade`` event.

    Pure constructor — nothing is appended. The ingestion pipeline uses it to
    run the SHACL gate against the built event *before* anything reaches the
    append-only log (:func:`make_assertion` is this plus the append).

    Syntax only: this function never loads an ontology graph. Whether the
    predicate is a relation the registered model actually declares is a
    semantic check the pipeline layer owns (``is_known_predicate``).

    Args:
        subject_id: Canonical entity the statement is about.
        predicate_iri: Absolute relation IRI (``core:locatedAt``); a bare
            local name is rejected — see
            :func:`foundry.namespaces.require_absolute_iri`.
        object_kind: One of ``entity``, ``location``, ``literal``.
        object_value: The object's id IRI, or the literal's lexical form.
        valid_from: Valid time (when the statement holds in the world).
        source_ids: Provenance references (non-empty).
        confidence: Optional 0..1.
        supersedes: Assertion id this statement replaces.
        literal_datatype_iri: Datatype of a literal object. Mutually exclusive
            with ``literal_language``; defaults to ``xsd:string``. Forbidden
            for entity/location objects.
        literal_language: BCP 47 language tag of a literal object
            (``"vi"``). Forbidden for entity/location objects.
        identity: Optional registry; when given, the subject must be known.
        known_assertions: Assertion ids already recorded; a ``supersedes``
            target must be among them.

    Raises:
        ValueError: On malformed input (relative predicate IRI, unknown object
            kind, a datatype/language tag on an IRI object, both a datatype and
            a language tag on a literal, a malformed timestamp, confidence
            outside ``[0, 1]``), an unknown subject, or a ``supersedes`` target
            that is not in ``known_assertions``.
    """
    if not source_ids:
        raise ValueError("at least one source_id is required")
    relation_iri = require_absolute_iri(predicate_iri, "predicate_iri")
    if object_kind not in OBJECT_KINDS:
        raise ValueError(f"object_kind must be one of {list(OBJECT_KINDS)}, got {object_kind!r}")
    if not object_value.strip():
        raise ValueError("object_value must be non-empty")
    object_field = _literal_object(
        object_kind,
        object_value,
        datatype_iri=literal_datatype_iri,
        language=literal_language,
    )
    parse_instant(valid_from)  # validates the xsd:dateTime form
    if confidence is not None and not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence {confidence} outside [0, 1]")
    if identity is not None and not identity.knows(subject_id):
        raise ValueError(f"unknown subject {subject_id}; resolve identity first")
    if supersedes is not None and supersedes not in known_assertions:
        raise ValueError(f"supersedes target {supersedes!r} is not a recorded assertion")

    return make_event(
        EVENT_TYPE_ASSERTION_MADE,
        {
            "assertion_id": new_assertion_id(),
            "subject_id": subject_id,
            "predicate_iri": relation_iri,
            "object": object_field,
            "valid_from": valid_from,
            "valid_to": None,
            "source_ids": list(source_ids),
            "confidence": confidence,
            "supersedes": supersedes,
        },
    )


def _literal_object(
    object_kind: str,
    object_value: str,
    *,
    datatype_iri: str | None,
    language: str | None,
) -> dict[str, Any]:
    """Build the payload ``object`` of an assertion, validating its shape.

    An entity/location object is just ``{kind, value}``: a datatype or a
    language tag on an IRI object is a caller bug. A literal object always
    carries both ``datatype_iri`` and ``language`` keys (exactly one non-null,
    ``xsd:string`` by default) so a reader never has to guess whether ``"250"``
    was a string or a number.

    Raises:
        ValueError: On a datatype/language tag on an IRI object, both a
            datatype and a language tag at once, a blank language tag, or a
            relative datatype IRI.
    """
    if object_kind != "literal":
        if datatype_iri is not None or language is not None:
            raise ValueError(
                f"literal_datatype_iri/literal_language are only valid for object_kind='literal', "
                f"got object_kind={object_kind!r}"
            )
        return {"kind": object_kind, "value": object_value}

    if datatype_iri is not None and language is not None:
        raise ValueError("a literal object carries a datatype or a language tag, never both")
    if language is not None:
        tag = language.strip()
        if not tag:
            raise ValueError("literal_language must be non-empty when given")
        return {"kind": "literal", "value": object_value, "datatype_iri": None, "language": tag}
    return {
        "kind": "literal",
        "value": object_value,
        "datatype_iri": require_absolute_iri(
            datatype_iri if datatype_iri is not None else DEFAULT_LITERAL_DATATYPE,
            "literal_datatype_iri",
        ),
        "language": None,
    }


def make_assertion(
    *,
    log: EventLog,
    subject_id: str,
    predicate_iri: str,
    object_kind: str,
    object_value: str,
    valid_from: str,
    source_ids: list[str],
    confidence: float | None = None,
    supersedes: str | None = None,
    literal_datatype_iri: str | None = None,
    literal_language: str | None = None,
    identity: IdentityService | None = None,
) -> SemanticEvent:
    """Record one statement about the world as a reified assertion.

    Args:
        log: Event log to append the assertion to.
        subject_id: Canonical entity the statement is about.
        predicate_iri: Absolute relation IRI (``core:locatedAt``). A bare local
            name is rejected: see :func:`build_assertion`.
        object_kind: One of ``entity``, ``location``, ``literal``.
        object_value: The object's id IRI or the literal's lexical form.
        valid_from: Valid time (when the statement holds in the world).
        source_ids: Provenance references.
        confidence: Optional 0..1.
        supersedes: Assertion id this statement replaces.
        literal_datatype_iri: Datatype of a literal object (default
            ``xsd:string``); mutually exclusive with ``literal_language``.
        literal_language: BCP 47 language tag of a literal object.
        identity: Optional registry; when given, the subject must be known.

    Raises:
        ValueError: On malformed input, an unknown subject, or a ``supersedes``
            target that no ``AssertionMade`` in the log records.
    """
    known: Collection[str] = frozenset()
    if supersedes is not None:
        known = {
            event.payload["assertion_id"]
            for event in log.read_all()
            if event.event_type == EVENT_TYPE_ASSERTION_MADE
        }
    event = build_assertion(
        subject_id=subject_id,
        predicate_iri=predicate_iri,
        object_kind=object_kind,
        object_value=object_value,
        valid_from=valid_from,
        source_ids=source_ids,
        confidence=confidence,
        supersedes=supersedes,
        literal_datatype_iri=literal_datatype_iri,
        literal_language=literal_language,
        identity=identity,
        known_assertions=known,
    )
    log.append(event)
    return event


def supersede_assertion(*, log: EventLog, assertion_id: str, reason: str = "") -> SemanticEvent:
    """Retract/correct an assertion via an ``AssertionSuperseded`` event.

    Raises:
        ValueError: If the assertion was never made, or is already superseded.
    """
    if _find_assertion_event(log, assertion_id) is None:
        raise ValueError(f"unknown assertion {assertion_id!r}")
    if is_superseded(log, assertion_id):
        raise ValueError(f"assertion {assertion_id!r} is already superseded")
    event = make_event(
        EVENT_TYPE_ASSERTION_SUPERSEDED,
        {
            "assertion_id": assertion_id,
            "reason": reason,
            "superseded_at": utc_now_iso(),
        },
    )
    log.append(event)
    return event


def load_assertions(log: EventLog) -> dict[str, dict[str, Any]]:
    """Replay the assertion ledger from the log: id -> assertion payload + status."""
    ledger: dict[str, dict[str, Any]] = {}
    for event in log.read_all():
        payload = event.payload
        if event.event_type == EVENT_TYPE_ASSERTION_MADE:
            ledger[payload["assertion_id"]] = {**payload, "status": "asserted"}
        elif event.event_type == EVENT_TYPE_ASSERTION_SUPERSEDED:
            entry = ledger.get(payload["assertion_id"])
            if entry is not None:
                entry["status"] = "superseded"
                entry["superseded_reason"] = payload.get("reason", "")
    return ledger


def dump_assertions(ledger: dict[str, dict[str, Any]]) -> str:
    """Serialize a ledger for tooling output (stable JSON, one line per row)."""
    return "\n".join(
        json.dumps({**entry, "assertion_id": assertion_id}, ensure_ascii=False, sort_keys=True)
        for assertion_id, entry in sorted(ledger.items())
    )


# -- RDF/SHACL mapping (ontology/middle/assertion.ttl + shapes/assertion_shapes.ttl) --

ASSERTION_NS = ASSERTION_MIDDLE_NS
CORE_NS = CORE_ONTOLOGY_NS
_OBJECT_PREDICATES = {
    "entity": (ASSERTION_NS, "hasObject"),
    "location": (ASSERTION_NS, "hasObject"),
    "literal": (ASSERTION_NS, "literalValue"),
}


def is_known_predicate(graph: Graph, predicate_iri: str) -> bool:
    """True when ``predicate_iri`` is a property the registered model declares.

    The ontology is the allowlist: a relation no module declares can never be
    asserted, so a free-text or LLM-proposed relation cannot mint graph
    vocabulary (roadmap Phase 2: *LLM chỉ đề xuất; semantic system quyết định
    acceptance*). This is the semantic layer of validation — the syntax layer
    (:func:`build_assertion`) only checks that the value is an absolute IRI.

    Args:
        graph: Loaded ontology modules (the registered model).
        predicate_iri: Absolute property IRI to look up.

    Raises:
        ValueError: On a blank or relative IRI.
    """
    node = URIRef(require_absolute_iri(predicate_iri, "predicate_iri"))
    return (node, RDF.type, OWL.ObjectProperty) in graph or (
        node,
        RDF.type,
        OWL.DatatypeProperty,
    ) in graph


def document_to_rdf(graph: Graph, event: SemanticEvent) -> URIRef:
    """Add the RDF mapping of one ``DocumentRegistered`` event; returns its node.

    The document is an ``assertion:Document`` (a subclass of ``core:Source``)
    carrying its title as ``core:name``.
    """
    if event.event_type != EVENT_TYPE_DOCUMENT_REGISTERED:
        raise ValueError(f"expected DocumentRegistered, got {event.event_type}")
    payload = event.payload
    node = URIRef(payload["document_id"])
    graph.add((node, RDF.type, URIRef(ASSERTION_NS + "Document")))
    graph.add((node, URIRef(CORE_NS + "name"), Literal(payload["title"], datatype=XSD.string)))
    return node


def assertion_to_rdf(graph: Graph, event: SemanticEvent) -> URIRef:
    """Add the RDF mapping of one ``AssertionMade`` event; returns its node.

    Mapping (ontology/middle/assertion.ttl): the relation is a node in the
    graph (``assertion:predicate`` → the event's ``predicate_iri``, so two
    statements differing only in their relation are different RDF), subject via
    ``core:describes``, object via ``assertion:hasObject`` (entity/location) or
    ``assertion:literalValue`` (literal, carrying its datatype or language tag),
    valid time via ``core:validFrom``/``core:validUntil``, provenance via
    ``core:hasSource`` (cited Documents), optional ``core:hasConfidence`` and
    the correction link ``assertion:supersedes``.

    Raises:
        ValueError: On a non-AssertionMade event, an unknown object kind, a
            payload without ``predicate_iri`` (a record written before the
            event-schema rename that never went through the log upcaster), a
            non-absolute predicate/object IRI, or a literal object carrying
            both a language tag and a datatype.
    """
    if event.event_type != EVENT_TYPE_ASSERTION_MADE:
        raise ValueError(f"expected AssertionMade, got {event.event_type}")
    payload = event.payload
    obj = payload["object"]
    kind = obj["kind"]
    if kind not in _OBJECT_PREDICATES:
        raise ValueError(f"unknown object kind {kind!r}")
    try:
        predicate_iri = payload["predicate_iri"]
    except KeyError as exc:
        raise ValueError("AssertionMade payload is missing predicate_iri") from exc

    node = URIRef(payload["assertion_id"])
    graph.add((node, RDF.type, URIRef(ASSERTION_NS + "Assertion")))
    graph.add((node, URIRef(CORE_NS + "describes"), URIRef(payload["subject_id"])))
    graph.add(
        (
            node,
            URIRef(ASSERTION_NS + "predicate"),
            URIRef(require_absolute_iri(predicate_iri, "predicate_iri")),
        )
    )

    ns, local = _OBJECT_PREDICATES[kind]
    object_node: URIRef | Literal = (
        _literal_object_value(obj)
        if kind == "literal"
        else URIRef(require_absolute_iri(obj["value"], "object.value"))
    )
    graph.add((node, URIRef(ns + local), object_node))

    graph.add(
        (
            node,
            URIRef(CORE_NS + "validFrom"),
            Literal(payload["valid_from"], datatype=XSD.dateTime),
        )
    )
    if payload.get("valid_to") is not None:
        graph.add(
            (
                node,
                URIRef(CORE_NS + "validUntil"),
                Literal(payload["valid_to"], datatype=XSD.dateTime),
            )
        )
    for source_id in payload.get("source_ids") or ():
        graph.add((node, URIRef(CORE_NS + "hasSource"), URIRef(source_id)))
    confidence = payload.get("confidence")
    if confidence is not None:
        graph.add(
            (
                node,
                URIRef(CORE_NS + "hasConfidence"),
                Literal(str(confidence), datatype=XSD.decimal),
            )
        )
    if payload.get("supersedes") is not None:
        graph.add((node, URIRef(ASSERTION_NS + "supersedes"), URIRef(payload["supersedes"])))
    return node


def _literal_object_value(obj: dict[str, Any]) -> Literal:
    """Build the RDF literal of a literal-valued assertion object.

    The event payload carries ``datatype_iri`` or ``language`` (exactly one, by
    contract), so the lexical form maps to the same RDF term the writer meant:
    ``"250"`` with ``xsd:decimal`` stays a number, ``"Việt Nam"@vi`` stays a
    language-tagged string. A payload missing both falls back to
    ``xsd:string`` — the documented default, and what a pre-typed v3 record
    written before this field existed means.

    Raises:
        ValueError: On a payload carrying a language tag and a datatype at once.
    """
    language = obj.get("language")
    datatype_iri = obj.get("datatype_iri")
    if language and datatype_iri:
        raise ValueError("literal object carries both a language tag and a datatype")
    if language:
        return Literal(obj["value"], lang=language)
    return Literal(
        obj["value"],
        datatype=URIRef(require_absolute_iri(datatype_iri or DEFAULT_LITERAL_DATATYPE, "datatype")),
    )
