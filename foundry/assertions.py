"""Document and Assertion model (foundation — docs/architecture.md §4.2).

Separates "the fact that we learned something" (an event) from "a statement
about the world" (an assertion). Every assertion is a first-class record with
its own stable id, generic provenance and bi-temporal timestamps:

    assertion_id (urn:assert:<hex>)   stable handle for correct/retract
    subject_id                        canonical entity the assertion is about
    predicate                         e.g. locatedAt, memberOf
    object                            {kind: entity|location|literal, value}
    valid_from / valid_to             valid time (when it holds in the world)
    source_ids[]                      provenance (documents / source records)
    confidence                        optional 0..1

Corrections never rewrite anything (ADR-0002): superseding an assertion emits
an ``AssertionSuperseded`` event that references the original id, and the read
model keeps the full assertion ledger with statuses. ``LocationObserved``-style
events keep working; the assertion layer is the target model new fact types
should build on.

Ontology mapping (SHACL shapes for assertions) is deliberately deferred: this
module fixes the event contract and the ledger; shapes land with the ontology
work in the next phase.
"""

from __future__ import annotations

import json
from typing import Any

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
from foundry.namespaces import new_assertion_id, new_document_id
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


def make_assertion(
    *,
    log: EventLog,
    subject_id: str,
    predicate: str,
    object_kind: str,
    object_value: str,
    valid_from: str,
    source_ids: list[str],
    confidence: float | None = None,
    supersedes: str | None = None,
    identity: IdentityService | None = None,
) -> SemanticEvent:
    """Record one statement about the world as a reified assertion.

    Args:
        log: Event log to append the assertion to.
        subject_id: Canonical entity the statement is about.
        predicate: Relation name (e.g. ``locatedAt``).
        object_kind: One of ``entity``, ``location``, ``literal``.
        object_value: The object's id or literal value.
        valid_from: Valid time (when the statement holds in the world).
        source_ids: Provenance references.
        confidence: Optional 0..1.
        supersedes: Assertion id this statement replaces.
        identity: Optional registry; when given, the subject must be known.

    Raises:
        ValueError: On malformed input, an unknown subject, or a ``supersedes``
            target that no ``AssertionMade`` in the log records.
    """
    if not predicate.strip():
        raise ValueError("predicate must be non-empty")
    if object_kind not in OBJECT_KINDS:
        raise ValueError(f"object_kind must be one of {list(OBJECT_KINDS)}, got {object_kind!r}")
    if not object_value.strip():
        raise ValueError("object_value must be non-empty")
    parse_instant(valid_from)  # validates the xsd:dateTime form
    if confidence is not None and not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence {confidence} outside [0, 1]")
    if identity is not None and not identity.knows(subject_id):
        raise ValueError(f"unknown subject {subject_id}; resolve identity first")
    if supersedes is not None and _find_assertion_event(log, supersedes) is None:
        raise ValueError(f"supersedes target {supersedes!r} is not a recorded assertion")

    event = make_event(
        EVENT_TYPE_ASSERTION_MADE,
        {
            "assertion_id": new_assertion_id(),
            "subject_id": subject_id,
            "predicate": predicate,
            "object": {"kind": object_kind, "value": object_value},
            "valid_from": valid_from,
            "valid_to": None,
            "source_ids": list(source_ids),
            "confidence": confidence,
            "supersedes": supersedes,
        },
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
