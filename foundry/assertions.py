"""Document and Assertion model (docs/architecture.md §4.2).

Separates "the fact that we learned something" (an event) from "a statement
about the world" (an assertion — an ``assertion:Assertion``, i.e. an
``InformationObject``, never an event). Every assertion is a first-class record
with its own stable id, generic provenance and bi-temporal timestamps:

    assertion_id (urn:assert:<hex>)   stable handle for correct/retract
    subject_id                        canonical entity the assertion is about
    predicate_iri                     absolute relation IRI (core:locatedAt)
    object                            {kind: entity|location|literal, value, ...}
    valid_from / valid_until          half-open valid-time interval
    source_ids[]                      normalized provenance Source references
    recorded_at                       event record-time, separate from valid time
    model_score / evidence_quality    independent, non-probabilistic metadata

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
(absolute IRIs, well-formed object, time bounds, model score) and the ingestion
pipeline checks the *semantic* contract (``is_known_predicate`` against the
registered ontology); SHACL then enforces the structural RDF constraints.

Corrections never rewrite anything (ADR-0002): a replacement AssertionMade
carries ``supersedes`` to link the prior claim. ``AssertionSuperseded`` records
a retraction with no replacement. The read model keeps the complete assertion
ledger and refreshes location projections for both operations.

Ontology presence: ``ontology/middle/assertion.ttl`` declares the classes and
properties, ``shapes/assertion_shapes.ttl`` mirrors the write-path validation,
and :func:`assertion_to_rdf` maps the events onto that model.
"""

from __future__ import annotations

import json
import math
from collections.abc import Collection
from dataclasses import replace
from pathlib import Path
from typing import Any

from pyshacl import validate
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.collection import Collection as RDFCollection
from rdflib.namespace import OWL, RDF, RDFS, XSD

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
    IDENTITY_MIDDLE_NS,
    new_assertion_id,
    require_absolute_iri,
)
from foundry.provenance import SourceRecord, content_sha256, normalize_evidence
from foundry.readmodel import parse_instant

OBJECT_KINDS = ("entity", "location", "literal")


def register_document(
    *,
    log: EventLog,
    uri: str,
    title: str,
    source_system: str,
    content_ref: str | None = None,
    record_id: str | None = None,
    content: str | bytes | None = None,
    content_hash: str | None = None,
) -> SemanticEvent:
    """Validate and durably register one source document version.

    ``record_id`` is stable across versions; ``content_hash`` distinguishes
    versions. Replaying the same record and bytes returns its original event.

    Raises:
        ValueError: On empty uri, title or source_system.
    """
    uri = require_absolute_iri(uri, "document uri")
    if not title.strip():
        raise ValueError("document title must be non-empty")
    if not source_system.strip():
        raise ValueError("source_system must be non-empty")
    stable_record_id = record_id.strip() if record_id is not None else uri.strip()
    if content_hash is None:
        if content is None:
            raise ValueError("document content or an explicit content_hash is required")
        content_hash = content_sha256(content)
    elif content is not None and content_hash != content_sha256(content):
        raise ValueError("content_hash does not match the supplied document content")
    source_record = SourceRecord(source_system, stable_record_id, content_hash)
    document_id = "urn:doc:" + content_sha256(f"{source_record.key}\0{source_record.content_hash}")
    payload = {
        "document_id": document_id,
        "uri": uri.strip(),
        "title": title.strip(),
        "source_system": source_system.strip(),
        "source_id": source_record.source_id,
        "source_record_id": source_record.record_id,
        "source_record_key": source_record.key,
        "content_hash": source_record.content_hash,
        "content_ref": content_ref,
    }
    event = make_event(EVENT_TYPE_DOCUMENT_REGISTERED, payload)
    event = replace(event, payload={**event.payload, "recorded_at": event.occurred_at})

    graph = Graph()
    document_to_rdf(graph, event)
    shape_path = Path(__file__).resolve().parent.parent / "shapes" / "assertion_shapes.ttl"
    shapes = Graph().parse(shape_path.as_posix(), format="turtle")
    conforms, _, report = validate(
        data_graph=graph,
        shacl_graph=shapes,
        inference="none",
        advanced=True,
    )
    if not conforms:
        raise ValueError(f"DocumentRegistered SHACL violation: {report.strip()}")

    stable_request = {key: value for key, value in payload.items() if key != "document_id"}
    fingerprint = content_sha256(
        json.dumps(stable_request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    result = log.commit(
        [event],
        idempotency_key=f"document:{source_record.key}:{source_record.content_hash}",
        request_fingerprint=fingerprint,
    )
    return result.events[0]


def _find_assertion_event(log: EventLog, assertion_id: str) -> SemanticEvent | None:
    for event in log.read_all():
        if (
            event.event_type == EVENT_TYPE_ASSERTION_MADE
            and event.payload.get("assertion_id") == assertion_id
        ):
            return event
    return None


def is_superseded(log: EventLog, assertion_id: str) -> bool:
    """Return whether retraction or replacement has ended this assertion."""
    return any(
        (
            event.event_type == EVENT_TYPE_ASSERTION_SUPERSEDED
            and event.payload.get("assertion_id") == assertion_id
        )
        or (
            event.event_type == EVENT_TYPE_ASSERTION_MADE
            and event.payload.get("supersedes") == assertion_id
        )
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
    subject_ref: dict[str, Any] | None = None,
    valid_until: str | None = None,
    evidence: list[dict[str, object]] | None = None,
    confidence: float | None = None,
    model_score: float | None = None,
    evidence_quality: str = "unknown",
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
        subject_id: Canonical entity the statement is about. Use an empty
            string with ``subject_ref`` for a pending, noncanonical subject.
        subject_ref: Noncanonical reference with ``reference_id`` and
            ``surface_form``. It remains pending until identity is unambiguous.
        predicate_iri: Absolute relation IRI (``core:locatedAt``); a bare
            local name is rejected — see
            :func:`foundry.namespaces.require_absolute_iri`.
        object_kind: One of ``entity``, ``location``, ``literal``.
        object_value: The object's id IRI, or the literal's lexical form.
        valid_from: Valid time (when the statement holds in the world).
        source_ids: Provenance references (non-empty unless evidence supplies
            the source references).
        valid_until: Exclusive end of the half-open interval.
        evidence: Inline source-local locator/span references.
        confidence: Deprecated alias for ``model_score``. Neither field is a
            probability that the assertion is true.
        model_score: Optional extractor/system score in [0,1].
        evidence_quality: Categorical evidence assessment independent of score.
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
            a language tag on a literal, a malformed timestamp, a model score
            outside ``[0, 1]``), an unknown subject, or a ``supersedes`` target
            that is not in ``known_assertions``.
    """
    if not source_ids and not evidence:
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
    if valid_until is not None and parse_instant(valid_until) <= parse_instant(valid_from):
        raise ValueError("valid_until must be later than valid_from (half-open interval)")
    if confidence is not None and model_score is not None:
        raise ValueError("supply model_score; confidence is a deprecated alias")
    score = model_score if model_score is not None else confidence
    if score is not None and (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0.0 <= score <= 1.0
    ):
        raise ValueError(f"model_score {score} outside [0, 1]")
    if not isinstance(evidence_quality, str):
        raise ValueError("evidence_quality must be a string")
    evidence_quality = evidence_quality.strip().lower()
    if evidence_quality not in {"direct", "corroborated", "indirect", "weak", "unknown"}:
        raise ValueError(f"unsupported evidence_quality {evidence_quality!r}")
    if subject_id and subject_ref is not None:
        raise ValueError("supply exactly one canonical subject_id or subject_ref")
    if not subject_id and subject_ref is None:
        raise ValueError("supply exactly one canonical subject_id or subject_ref")
    if subject_id:
        require_absolute_iri(subject_id, "subject_id")
    else:
        reference_id = require_absolute_iri(
            str(subject_ref.get("reference_id", "")), "subject_ref.reference_id"
        )
        surface_form = str(subject_ref.get("surface_form", "")).strip()
        if not surface_form:
            raise ValueError("subject_ref.surface_form must be non-empty")
        subject_ref = {
            "reference_id": reference_id,
            "surface_form": surface_form,
            "entity_type": subject_ref.get("entity_type"),
        }
    if identity is not None and subject_id and not identity.knows(subject_id):
        raise ValueError(f"unknown subject {subject_id}; resolve identity first")
    if supersedes is not None and supersedes not in known_assertions:
        raise ValueError(f"supersedes target {supersedes!r} is not a recorded assertion")

    normalized_source_ids, evidence_refs = normalize_evidence(source_ids, evidence)
    event = make_event(
        EVENT_TYPE_ASSERTION_MADE,
        {
            "assertion_id": new_assertion_id(),
            "subject_id": subject_id,
            "subject_ref": subject_ref,
            "predicate_iri": relation_iri,
            "object": object_field,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "source_ids": list(normalized_source_ids),
            "evidence": list(evidence_refs),
            "recorded_at": None,
            "status": "asserted",
            "model_score": score,
            "evidence_quality": evidence_quality,
            "supersedes": supersedes,
        },
    )
    return replace(event, payload={**event.payload, "recorded_at": event.occurred_at})


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
    subject_ref: dict[str, Any] | None = None,
    valid_until: str | None = None,
    evidence: list[dict[str, object]] | None = None,
    confidence: float | None = None,
    model_score: float | None = None,
    evidence_quality: str = "unknown",
    supersedes: str | None = None,
    literal_datatype_iri: str | None = None,
    literal_language: str | None = None,
    identity: IdentityService | None = None,
    idempotency_key: str | None = None,
) -> SemanticEvent:
    """Record one statement about the world as a reified assertion.

    Args:
        log: Event log to append the assertion to.
        subject_id: Canonical entity the statement is about, or empty when
            ``subject_ref`` is supplied.
        subject_ref: Noncanonical subject reference held pending resolution.
        predicate_iri: Absolute relation IRI (``core:locatedAt``). A bare local
            name is rejected: see :func:`build_assertion`.
        object_kind: One of ``entity``, ``location``, ``literal``.
        object_value: The object's id IRI or the literal's lexical form.
        valid_from: Valid time (when the statement holds in the world).
        valid_until: Exclusive end of the half-open validity interval.
        source_ids: Provenance references.
        evidence: Inline source-local locator/span references.
        confidence: Deprecated alias for model_score.
        idempotency_key: Stable key for retrying this logical assertion.
        model_score: Optional extractor/system score in [0,1], not a fact probability.
        evidence_quality: Categorical evidence assessment independent of score.
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
        subject_ref=subject_ref,
        valid_until=valid_until,
        evidence=evidence,
        confidence=confidence,
        model_score=model_score,
        evidence_quality=evidence_quality,
        supersedes=supersedes,
        literal_datatype_iri=literal_datatype_iri,
        literal_language=literal_language,
        identity=identity,
        known_assertions=known,
    )
    operation_key = f"assertion-status:{supersedes}" if supersedes is not None else idempotency_key
    fingerprint = assertion_request_fingerprint(event)
    if operation_key is not None:
        prior = log.find_idempotency(operation_key, request_fingerprint=fingerprint)
        if prior is not None:
            return prior.events[0]
    if supersedes is not None and is_superseded(log, supersedes):
        raise ValueError(f"supersedes target {supersedes!r} is already superseded")
    if operation_key is None:
        log.append(event)
        return event
    return log.commit(
        [event], idempotency_key=operation_key, request_fingerprint=fingerprint
    ).events[0]


def supersede_assertion(*, log: EventLog, assertion_id: str, reason: str = "") -> SemanticEvent:
    """Retract an assertion with no replacement via an event.

    Raises:
        ValueError: If the assertion was never made, or is already superseded.
    """
    key = f"assertion-status:{assertion_id}"
    fingerprint = content_sha256(
        json.dumps(
            {"operation": "retract", "assertion_id": assertion_id, "reason": reason},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    prior = log.find_idempotency(key)
    if prior is not None:
        if prior.events[0].event_type == EVENT_TYPE_ASSERTION_SUPERSEDED:
            return log.find_idempotency(key, request_fingerprint=fingerprint).events[0]
        raise ValueError(f"assertion {assertion_id!r} is already superseded")
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
    return log.commit([event], idempotency_key=key, request_fingerprint=fingerprint).events[0]


def load_assertions(log: EventLog) -> dict[str, dict[str, Any]]:
    """Replay the assertion ledger from the log: id -> assertion payload + status."""
    ledger: dict[str, dict[str, Any]] = {}
    for event in log.read_all():
        payload = event.payload
        if event.event_type == EVENT_TYPE_ASSERTION_MADE:
            assertion_id = payload["assertion_id"]
            ledger[assertion_id] = {**payload, "status": "asserted"}
            superseded_id = payload.get("supersedes")
            if superseded_id in ledger:
                ledger[superseded_id]["status"] = "superseded"
                ledger[superseded_id]["superseded_by"] = assertion_id
        elif event.event_type == EVENT_TYPE_ASSERTION_SUPERSEDED:
            entry = ledger.get(payload["assertion_id"])
            if entry is not None:
                entry["status"] = "retracted"
                entry["superseded_reason"] = payload.get("reason", "")
    return ledger


def dump_assertions(ledger: dict[str, dict[str, Any]]) -> str:
    """Serialize a ledger for tooling output (stable JSON, one line per row)."""
    return "\n".join(
        json.dumps({**entry, "assertion_id": assertion_id}, ensure_ascii=False, sort_keys=True)
        for assertion_id, entry in sorted(ledger.items())
    )


def assertion_request_fingerprint(event: SemanticEvent) -> str:
    """Hash assertion identity without generated or model-scored metadata.

    A retry for the same evidence and asserted statement must remain
    idempotent if an extractor's score drifts between runs. The first durable
    score remains part of the event; callers need a new idempotency key to
    record a separate assessment.
    """
    payload = dict(event.payload)
    payload.pop("assertion_id", None)
    payload.pop("recorded_at", None)
    payload.pop("model_score", None)
    return content_sha256(
        json.dumps(
            {"event_type": event.event_type, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
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
    object_property = (node, RDF.type, OWL.ObjectProperty) in graph
    datatype_property = (node, RDF.type, OWL.DatatypeProperty) in graph
    if object_property == datatype_property:
        return False
    if not list(graph.objects(node, RDFS.domain)) or not list(graph.objects(node, RDFS.range)):
        return False
    assertable = URIRef(CORE_NS + "assertable")
    return Literal(False) not in set(graph.objects(node, assertable))


def predicate_compatibility(
    graph: Graph,
    predicate_iri: str,
    *,
    subject_class_iri: str,
    object_kind: str,
    object_class_iri: str | None = None,
    literal_datatype_iri: str | None = None,
    literal_language: str | None = None,
) -> str | None:
    """Return a semantic incompatibility reason, or ``None`` when compatible.

    Compatibility comes from the registered ontology: the relation must be a
    declared, assertable object or datatype property with explicit domains and
    ranges. All declared domains and ranges must accept the supplied types
    (RDFS gives multiple domain/range statements intersection semantics).
    """
    node = URIRef(require_absolute_iri(predicate_iri, "predicate_iri"))
    if not is_known_predicate(graph, predicate_iri):
        return f"predicate {predicate_iri!r} is undeclared, non-assertable, or lacks domain/range"

    is_object_property = (node, RDF.type, OWL.ObjectProperty) in graph
    if is_object_property and object_kind == "literal":
        return "object property requires an entity or location object"
    if not is_object_property and object_kind != "literal":
        return "datatype property requires a literal object"

    if object_kind not in {"entity", "location", "literal"}:
        return f"unsupported object kind {object_kind!r}"

    subject = URIRef(require_absolute_iri(subject_class_iri, "subject_class_iri"))
    domains = list(graph.objects(node, RDFS.domain))
    for domain in domains:
        if not _is_class_compatible(graph, subject, domain):
            return f"subject type {subject} is incompatible with domain {domain}"

    ranges = list(graph.objects(node, RDFS.range))
    if object_kind == "literal":
        if literal_language is not None and literal_datatype_iri is not None:
            return "a literal cannot have both a language tag and an explicit datatype"
        datatype = URIRef(
            RDF.langString
            if literal_language is not None
            else require_absolute_iri(
                literal_datatype_iri or DEFAULT_LITERAL_DATATYPE, "datatype_iri"
            )
        )
        for value_range in ranges:
            if not _datatype_compatible(graph, datatype, value_range):
                return f"literal datatype {datatype} is incompatible with range {value_range}"
    else:
        if object_class_iri is None:
            object_class_iri = (
                CORE_ONTOLOGY_NS + "Location"
                if object_kind == "location"
                else CORE_ONTOLOGY_NS + "Entity"
            )
        object_class = URIRef(require_absolute_iri(object_class_iri, "object_class_iri"))
        for value_range in ranges:
            if not _is_class_compatible(graph, object_class, value_range):
                return f"object type {object_class} is incompatible with range {value_range}"
    return None


def _datatype_compatible(graph: Graph, actual: URIRef, expected: object) -> bool:
    """Check a datatype against a named range or an OWL data-range union."""
    if expected == RDFS.Literal:
        return True
    union_head = graph.value(expected, OWL.unionOf)
    if union_head is not None:
        return any(
            _datatype_compatible(graph, actual, member)
            for member in RDFCollection(graph, union_head)
        )
    return _is_class_compatible(graph, actual, expected)


def _is_class_compatible(graph: Graph, actual: URIRef, expected: object) -> bool:
    """True when ``actual`` is the expected type or a declared subclass."""
    if actual == expected:
        return True
    visited = {actual}
    frontier = [actual]
    while frontier:
        current = frontier.pop()
        for parent in graph.objects(current, RDFS.subClassOf):
            if parent == expected:
                return True
            if parent not in visited:
                visited.add(parent)
                frontier.append(parent)
    return False


def document_to_rdf(graph: Graph, event: SemanticEvent) -> URIRef:
    """Add the RDF mapping of one ``DocumentRegistered`` event; returns its node.

    The document is an ``assertion:Document`` (a subclass of ``core:Source``)
    carrying its title, source-record key, content hash and record time.
    """
    if event.event_type != EVENT_TYPE_DOCUMENT_REGISTERED:
        raise ValueError(f"expected DocumentRegistered, got {event.event_type}")
    payload = event.payload
    node = URIRef(payload["document_id"])
    graph.add((node, RDF.type, URIRef(ASSERTION_NS + "Document")))
    graph.add((node, URIRef(CORE_NS + "name"), Literal(payload["title"], datatype=XSD.string)))
    for field, local_name in (
        ("uri", "documentUri"),
        ("source_system", "sourceSystem"),
        ("source_record_id", "sourceRecordId"),
        ("source_record_key", "sourceRecordKey"),
        ("content_hash", "contentHash"),
    ):
        value = payload.get(field)
        if value is not None:
            graph.add(
                (node, URIRef(ASSERTION_NS + local_name), Literal(value, datatype=XSD.string))
            )
    graph.add(
        (
            node,
            URIRef(CORE_NS + "recordedAt"),
            Literal(payload.get("recorded_at", event.occurred_at), datatype=XSD.dateTime),
        )
    )
    return node


def assertion_to_rdf(
    graph: Graph,
    event: SemanticEvent,
    *,
    status_override: str | None = None,
) -> URIRef:
    """Add the RDF mapping of one ``AssertionMade`` event; returns its node.

    Mapping (ontology/middle/assertion.ttl): the relation is a node in the
    graph (``assertion:predicate`` → the event's ``predicate_iri``, so two
    statements differing only in their relation are different RDF), subject via
    ``core:describes``, object via ``assertion:hasObject`` (entity/location) or
    ``assertion:literalValue`` (literal, carrying its datatype or language tag),
    valid time via ``core:validFrom``/``core:validUntil``, recorded time via
    ``core:recordedAt``, provenance via ``core:hasSource`` and exact evidence,
    a non-probabilistic ``assertion:modelScore`` plus categorical evidence
    quality, and the correction link ``assertion:supersedes``.

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
    subject_ref = payload.get("subject_ref")
    if subject_ref:
        reference = URIRef(
            require_absolute_iri(subject_ref["reference_id"], "subject_ref.reference_id")
        )
        graph.add((reference, RDF.type, URIRef(IDENTITY_MIDDLE_NS + "UnresolvedReference")))
        graph.add(
            (
                reference,
                URIRef(IDENTITY_MIDDLE_NS + "referenceText"),
                Literal(subject_ref["surface_form"], datatype=XSD.string),
            )
        )
        graph.add((node, URIRef(ASSERTION_NS + "subjectReference"), reference))
    else:
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
    graph.add(
        (
            node,
            URIRef(CORE_NS + "recordedAt"),
            Literal(payload.get("recorded_at", event.occurred_at), datatype=XSD.dateTime),
        )
    )
    valid_until = payload.get("valid_until", payload.get("valid_to"))
    if valid_until is not None:
        graph.add(
            (
                node,
                URIRef(CORE_NS + "validUntil"),
                Literal(valid_until, datatype=XSD.dateTime),
            )
        )
    source_ids, evidence = normalize_evidence(
        payload.get("source_ids") or (), payload.get("evidence")
    )
    for source_id in source_ids:
        source = URIRef(source_id)
        graph.add((node, URIRef(CORE_NS + "hasSource"), source))
        graph.add((source, RDF.type, URIRef(CORE_NS + "Source")))
        source_name = source_id.rstrip("/").rsplit("/", 1)[-1].split("#")[-1]
        graph.add((source, URIRef(CORE_NS + "name"), Literal(source_name, datatype=XSD.string)))
    for item in evidence:
        evidence_node = BNode()
        graph.add((node, URIRef(ASSERTION_NS + "evidence"), evidence_node))
        graph.add(
            (
                evidence_node,
                URIRef(ASSERTION_NS + "evidenceSource"),
                URIRef(item["source_id"]),
            )
        )
        if item.get("locator") is not None:
            graph.add(
                (
                    evidence_node,
                    URIRef(ASSERTION_NS + "locator"),
                    Literal(item["locator"], datatype=XSD.string),
                )
            )
        span = item.get("span")
        if isinstance(span, dict):
            graph.add((evidence_node, URIRef(ASSERTION_NS + "spanStart"), Literal(span["start"])))
            graph.add((evidence_node, URIRef(ASSERTION_NS + "spanEnd"), Literal(span["end"])))
            graph.add(
                (
                    evidence_node,
                    URIRef(ASSERTION_NS + "spanUnit"),
                    Literal(span["unit"], datatype=XSD.string),
                )
            )
            if span.get("exact_text") is not None:
                graph.add(
                    (
                        evidence_node,
                        URIRef(ASSERTION_NS + "exactText"),
                        Literal(span["exact_text"], datatype=XSD.string),
                    )
                )
    model_score = payload.get("model_score", payload.get("confidence"))
    if model_score is not None:
        graph.add(
            (
                node,
                URIRef(ASSERTION_NS + "modelScore"),
                Literal(str(model_score), datatype=XSD.decimal),
            )
        )
    graph.add(
        (
            node,
            URIRef(ASSERTION_NS + "evidenceQuality"),
            Literal(payload.get("evidence_quality", "unknown"), datatype=XSD.string),
        )
    )
    graph.add(
        (
            node,
            URIRef(ASSERTION_NS + "status"),
            Literal(status_override or payload.get("status", "asserted"), datatype=XSD.string),
        )
    )
    if payload.get("supersedes") is not None:
        graph.add((node, URIRef(ASSERTION_NS + "supersedes"), URIRef(payload["supersedes"])))
    return node


def assertion_superseded_to_rdf(graph: Graph, event: SemanticEvent) -> URIRef:
    """Map a retraction event without changing the referenced assertion."""
    if event.event_type != EVENT_TYPE_ASSERTION_SUPERSEDED:
        raise ValueError(f"expected AssertionSuperseded, got {event.event_type}")
    payload = event.payload
    node = URIRef(f"urn:event:{event.event_id}")
    graph.add((node, RDF.type, URIRef(CORE_NS + "Event")))
    graph.add(
        (node, URIRef(CORE_NS + "atTime"), Literal(payload["superseded_at"], datatype=XSD.dateTime))
    )
    graph.add((node, URIRef(ASSERTION_NS + "retracts"), URIRef(payload["assertion_id"])))
    if payload.get("reason"):
        graph.add(
            (
                node,
                URIRef(ASSERTION_NS + "retractionReason"),
                Literal(payload["reason"], datatype=XSD.string),
            )
        )
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
