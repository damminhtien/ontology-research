"""Immutable domain events and an append-only event log.

Canonical knowledge changes are recorded as facts-on-a-log: events are never
mutated or deleted in place; corrections are new events that supersede older
ones. This module defines the event contract (schema v1) and a JSONL-backed
append-only store. The store is deliberately boring so the transport can be
swapped for a Kafka-style stream later without touching the payload contract.

Invariant: once appended, bytes in the log file are never rewritten.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

EVENT_TYPE_ENTITY_CREATED = "EntityCreated"
EVENT_TYPE_LOCATION_OBSERVED = "LocationObserved"
EVENT_TYPE_AFFILIATION_ASSESSED = "AffiliationAssessed"
EVENT_TYPE_ENTITY_MERGED = "EntityMerged"

EVENT_TYPES = frozenset(
    {
        EVENT_TYPE_ENTITY_CREATED,
        EVENT_TYPE_LOCATION_OBSERVED,
        EVENT_TYPE_AFFILIATION_ASSESSED,
        EVENT_TYPE_ENTITY_MERGED,
    }
)


def utc_now_iso() -> str:
    """Return the current UTC instant as an xsd:dateTime lexical form."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SemanticEvent:
    """A single immutable canonical fact.

    Attributes:
        event_id: Unique identifier (uuid4 hex) assigned at creation time.
        event_type: Discriminator, one of ``EVENT_TYPES``.
        schema_version: Payload contract version, currently 1.
        occurred_at: UTC instant the event was recorded (xsd:dateTime form).
        payload: Event-specific fields; must only contain JSON-serializable values.
    """

    event_id: str
    event_type: str
    schema_version: int
    occurred_at: str
    payload: dict[str, Any]


def make_event(event_type: str, payload: dict[str, Any]) -> SemanticEvent:
    """Build a new event with a fresh id, timestamp and current schema version.

    Raises:
        ValueError: If ``event_type`` is not a known event type.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(
            f"unknown event type {event_type!r}; expected one of {sorted(EVENT_TYPES)}"
        )
    return SemanticEvent(
        event_id=uuid.uuid4().hex,
        event_type=event_type,
        schema_version=SCHEMA_VERSION,
        occurred_at=utc_now_iso(),
        payload=payload,
    )


def event_to_dict(event: SemanticEvent) -> dict[str, Any]:
    """Serialize an event to a plain dictionary for JSON transport."""
    data = asdict(event)
    data["schema_version"] = SCHEMA_VERSION
    return data


def event_from_dict(data: dict[str, Any]) -> SemanticEvent:
    """Rebuild an event from its dictionary form.

    Raises:
        ValueError: If required fields are missing or the record is invalid.
    """
    try:
        event = SemanticEvent(
            event_id=data["event_id"],
            event_type=data["event_type"],
            schema_version=int(data["schema_version"]),
            occurred_at=data["occurred_at"],
            payload=dict(data["payload"]),
        )
    except KeyError as exc:
        raise ValueError(f"event record missing field: {exc}") from exc

    if event.event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event.event_type!r}")
    if event.schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema version {event.schema_version}; expected {SCHEMA_VERSION}"
        )
    return event


class EventLog:
    """Append-only JSONL event store.

    The log is intentionally minimal: append and replay. There is no update or
    delete operation by design - history is immutable and corrections are new
    events that supersede earlier ones.
    """

    def __init__(self, path: Path) -> None:
        """Create a log bound to ``path``; the file is created on first append."""
        self._path = path

    @property
    def path(self) -> Path:
        """Filesystem location of the log."""
        return self._path

    def append(self, event: SemanticEvent) -> None:
        """Append one event durably to the end of the log."""
        line = json.dumps(event_to_dict(event), ensure_ascii=False, sort_keys=True)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def extend(self, events: Iterator[SemanticEvent] | list[SemanticEvent]) -> int:
        """Append many events in one open handle; returns the count written."""
        count = 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            for event in events:
                line = json.dumps(event_to_dict(event), ensure_ascii=False, sort_keys=True)
                handle.write(line + "\n")
                count += 1
        return count

    def read_all(self) -> list[SemanticEvent]:
        """Replay the full log in order.

        Raises:
            ValueError: If any line is not a valid event record.
        """
        if not self._path.exists():
            return []
        events: list[SemanticEvent] = []
        with self._path.open("r", encoding="utf-8") as handle:
            for lineno, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    events.append(event_from_dict(json.loads(line)))
                except ValueError as exc:
                    raise ValueError(f"{self._path}:{lineno}: {exc}") from exc
        return events
