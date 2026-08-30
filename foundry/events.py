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
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

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
        schema_version: Payload contract version the event was written with.
        occurred_at: Record time — the UTC instant the event entered the log
            (xsd:dateTime form). This is when *we* learned the fact, not when
            it held in the world; see ``valid_at``.
        payload: Event-specific fields; must only contain JSON-serializable values.
        valid_at: Valid time — the instant the fact holds in the real world
            (e.g. when the location was actually occupied). ``None`` means the
            valid time coincides with the record time. Distinguishing the two
            matters for out-of-band or retrospective facts.
        sequence: 1-based position in the log, assigned by :class:`EventLog`
            on append. ``None`` only for records read from legacy pre-v2
            logs, where consumers fall back to the physical line position.
    """

    event_id: str
    event_type: str
    schema_version: int
    occurred_at: str
    payload: dict[str, Any]
    valid_at: str | None = None
    sequence: int | None = None


def make_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    valid_at: str | None = None,
) -> SemanticEvent:
    """Build a new event with a fresh id, timestamp and current schema version.

    Args:
        event_type: One of ``EVENT_TYPES``.
        payload: Event-specific fields.
        valid_at: Valid time of the fact; omit when the fact holds now.

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
        valid_at=valid_at,
    )


def event_to_dict(event: SemanticEvent) -> dict[str, Any]:
    """Serialize an event to a plain dictionary for JSON transport."""
    return asdict(event)


def _upcast_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """v1 → v2: add the valid-time header and the log sequence field.

    v1 records carry neither field: ``valid_at`` becomes ``None`` (the v1
    contract had a single timestamp) and ``sequence`` stays ``None`` so
    readers fall back to the physical line position of the append-only file.
    """
    data = dict(data)
    data["schema_version"] = 2
    data["valid_at"] = None
    data["sequence"] = None
    return data


#: Upcast chain: maps a record's schema version to the function rewriting it
#: one version forward. Every historical version must stay registered here —
#: removing one breaks replay of logs written under it.
UPCASTERS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    1: _upcast_v1_to_v2,
}


def _upcast(data: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a record forward to the current schema version.

    Raises:
        ValueError: If the record is newer than the supported version or has
            no registered upcast path.
    """
    try:
        version = int(data["schema_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid schema_version {data.get('schema_version')!r}") from exc
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"event schema version {version} is newer than supported {SCHEMA_VERSION}"
        )
    while version != SCHEMA_VERSION:
        upcast = UPCASTERS.get(version)
        if upcast is None:
            raise ValueError(f"no upcast path from schema version {version}")
        data = upcast(data)
        version = int(data["schema_version"])
    return data


def event_from_dict(data: dict[str, Any]) -> SemanticEvent:
    """Rebuild an event from its dictionary form.

    Records written under older schema versions are upcast forward; this is
    the only sanctioned migration mechanism for the log (records are never
    rewritten in place — ADR-0002).

    Raises:
        ValueError: If required fields are missing, the record is invalid, or
            no upcast path exists.
    """
    data = _upcast(data)
    try:
        sequence = data.get("sequence")
        event = SemanticEvent(
            event_id=data["event_id"],
            event_type=data["event_type"],
            schema_version=int(data["schema_version"]),
            occurred_at=data["occurred_at"],
            payload=dict(data["payload"]),
            valid_at=data.get("valid_at"),
            sequence=int(sequence) if sequence is not None else None,
        )
    except KeyError as exc:
        raise ValueError(f"event record missing field: {exc}") from exc

    if event.event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event.event_type!r}")
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
        self._next_seq: int | None = None

    @property
    def path(self) -> Path:
        """Filesystem location of the log."""
        return self._path

    def _next_sequence(self) -> int:
        """Allocate the next 1-based log sequence (lazily synced with disk)."""
        if self._next_seq is None:
            self._next_seq = self._physical_count() + 1
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def _physical_count(self) -> int:
        """Count non-empty lines in the log file (0 when it does not exist)."""
        if not self._path.exists():
            return 0
        with self._path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    def append(self, event: SemanticEvent) -> None:
        """Append one event durably to the end of the log.

        The event is stamped with the next log sequence before writing.
        """
        stamped = replace(event, sequence=self._next_sequence())
        line = json.dumps(event_to_dict(stamped), ensure_ascii=False, sort_keys=True)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def extend(self, events: Iterator[SemanticEvent] | list[SemanticEvent]) -> int:
        """Append many events in one open handle; returns the count written."""
        count = 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            for event in events:
                stamped = replace(event, sequence=self._next_sequence())
                line = json.dumps(event_to_dict(stamped), ensure_ascii=False, sort_keys=True)
                handle.write(line + "\n")
                count += 1
        return count

    def read_all(self) -> list[SemanticEvent]:
        """Replay the full log in order.

        Every stamped record must sit exactly at its sequence position; legacy
        unstamped records (schema v1 logs) are accepted at any position.

        Raises:
            ValueError: If any line is not a valid event record or a stamped
                sequence is out of position (log corruption or truncation).
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
                    event = event_from_dict(json.loads(line))
                    if event.sequence is not None and event.sequence != lineno:
                        raise ValueError(
                            f"sequence discontinuity: record claims {event.sequence}, "
                            f"physical position is {lineno}"
                        )
                except ValueError as exc:
                    raise ValueError(f"{self._path}:{lineno}: {exc}") from exc
                events.append(event)
        return events
