"""Immutable domain events and an append-only event log.

Canonical knowledge changes are recorded as facts-on-a-log: events are never
mutated or deleted in place; corrections are new events that supersede older
ones. This module defines the event contract (schema v1) and a JSONL-backed
append-only store. The store is deliberately boring so the transport can be
swapped for a Kafka-style stream later without touching the payload contract.

Invariant: once appended, bytes in the log file are never rewritten.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from foundry.versioning import EVENT_SCHEMA_VERSION

EVENT_TYPE_ENTITY_CREATED = "EntityCreated"
EVENT_TYPE_LOCATION_OBSERVED = "LocationObserved"
EVENT_TYPE_AFFILIATION_ASSESSED = "AffiliationAssessed"
EVENT_TYPE_ENTITY_MERGED = "EntityMerged"
EVENT_TYPE_EXTERNAL_ID_BOUND = "ExternalIdBound"

EVENT_TYPES = frozenset(
    {
        EVENT_TYPE_ENTITY_CREATED,
        EVENT_TYPE_LOCATION_OBSERVED,
        EVENT_TYPE_AFFILIATION_ASSESSED,
        EVENT_TYPE_ENTITY_MERGED,
        EVENT_TYPE_EXTERNAL_ID_BOUND,
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
        schema_version=EVENT_SCHEMA_VERSION,
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
    if version > EVENT_SCHEMA_VERSION:
        raise ValueError(
            f"event schema version {version} is newer than supported {EVENT_SCHEMA_VERSION}"
        )
    while version != EVENT_SCHEMA_VERSION:
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


class EventLogLocked(RuntimeError):
    """Raised when another process holds the log's writer lock."""


class EventLog:
    """Append-only JSONL event store with segmented, single-writer transport.

    Storage layout for a log bound to ``path`` (the *base*, e.g.
    ``data/production.jsonl``):

    - the base file is the **active segment** — appends land here;
    - when the active segment reaches ``max_segment_bytes`` it is renamed to
      ``<stem>-000001.jsonl``, ``<stem>-000002.jsonl``, … (ordered archives)
      and a fresh base file takes over; reads visit archives oldest-first,
      the base last;
    - ``sequence`` is a global 1-based offset across all segments, so the log
      can be replayed, checkpointed and verified as one logical stream.

    Write discipline:

    - a writer holds an exclusive ``flock`` on ``<path>.lock`` for the whole
      append batch; a second concurrent writer fails fast with
      :class:`EventLogLocked` instead of interleaving lines;
    - every batch is flushed and ``fsync``-ed before the call returns — a
      reported success survives a crash;
    - a trailing unterminated line (crash mid-write) is not a committed
      record: reads skip it and the next append truncates it.

    There is no update or delete operation by design — history is immutable
    and corrections are new events that supersede earlier ones (ADR-0002).
    """

    #: Rolled archives keep this many digits between stem and suffix.
    ARCHIVE_DIGITS = 6

    def __init__(self, path: Path, *, max_segment_bytes: int = 32 * 1024 * 1024) -> None:
        """Bind the log to ``path``; files are created on first append.

        Args:
            path: Base path of the log (the active segment).
            max_segment_bytes: Roll the active segment to an archive once it
                reaches this size.
        """
        if max_segment_bytes < 1:
            raise ValueError("max_segment_bytes must be >= 1")
        self._path = path
        self._max_segment_bytes = max_segment_bytes
        self._next_seq: int | None = None

    @property
    def path(self) -> Path:
        """Base path of the log (the active segment)."""
        return self._path

    @property
    def lock_path(self) -> Path:
        """Writer lock file guarding append batches."""
        return self._path.with_suffix(self._path.suffix + ".lock")

    # -- segment layout ------------------------------------------------------

    def _archives(self) -> list[Path]:
        """Rolled archive segments in replay order (oldest first)."""
        parent = self._path.parent
        if not parent.exists():
            return []
        prefix = f"{self._path.stem}-"
        digits = self.ARCHIVE_DIGITS
        candidates = parent.glob(f"{prefix}[0-9]*{self._path.suffix}")
        return sorted(
            (p for p in candidates if p.stem[len(prefix) :].isdigit()),
            key=lambda p: (len(p.stem) != len(prefix) + digits, p.name),
        )

    def _segments(self) -> list[Path]:
        """All segment files in replay order (archives, then the active base)."""
        segments = self._archives()
        if self._path.exists():
            segments.append(self._path)
        return segments

    def _roll_if_needed(self) -> None:
        """Rename the active segment to the next archive when it is full."""
        if not self._path.exists() or self._path.stat().st_size < self._max_segment_bytes:
            return
        archive = self._path.with_name(
            f"{self._path.stem}-{len(self._archives()) + 1:0{self.ARCHIVE_DIGITS}d}"
            f"{self._path.suffix}"
        )
        self._path.rename(archive)

    # -- write path ----------------------------------------------------------

    def _next_sequence(self) -> int:
        """Allocate the next 1-based log sequence (lazily synced with disk)."""
        if self._next_seq is None:
            self._next_seq = self._physical_count() + 1
        seq = self._next_seq
        self._next_seq += 1
        return seq

    def _physical_count(self) -> int:
        """Count physical lines across every segment (0 when the log is empty)."""
        return sum(self._count_lines(segment) for segment in self._segments())

    @staticmethod
    def _count_lines(segment: Path) -> int:
        if not segment.exists():
            return 0
        with segment.open("rb") as handle:
            return sum(1 for _ in handle)

    def _recover_tail(self) -> None:
        """Truncate a trailing unterminated line (crash mid-write artifact).

        A committed record always ends with a newline; a partial line carries
        no sequence stamp and no durability guarantee, so dropping it keeps the
        log appendable without rewriting any committed byte.
        """
        if not self._path.exists() or self._path.stat().st_size == 0:
            return
        with self._path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return
            window_start = max(0, size - 65536)
            handle.seek(window_start)
            tail = handle.read()
        cut = tail.rfind(b"\n")
        good_size = window_start + cut + 1 if cut != -1 else 0
        with self._path.open("r+b") as handle:
            handle.truncate(good_size)

    def _writer_lock(self):
        """Acquire the exclusive writer lock; returns the open lock handle."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise EventLogLocked(
                f"another writer holds {self.lock_path}; refusing to interleave appends"
            ) from exc
        return handle

    def append(self, event: SemanticEvent) -> None:
        """Append one event durably to the end of the log.

        The event is stamped with the next log sequence before writing; the
        batch is flushed and fsync-ed before returning.

        Raises:
            EventLogLocked: If another writer holds the log lock.
        """
        self.extend([event])

    def extend(self, events: Iterator[SemanticEvent] | list[SemanticEvent]) -> int:
        """Append many events in one locked, fsync-ed batch.

        Returns the count written. A trailing partial line from a previous
        crash is truncated first so new records never concatenate onto it.

        Raises:
            EventLogLocked: If another writer holds the log lock.
        """
        count = 0
        lock = self._writer_lock()
        try:
            self._recover_tail()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                for event in events:
                    stamped = replace(event, sequence=self._next_sequence())
                    line = json.dumps(event_to_dict(stamped), ensure_ascii=False, sort_keys=True)
                    handle.write(line + "\n")
                    count += 1
                handle.flush()
                os.fsync(handle.fileno())
            self._roll_if_needed()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        return count

    def read_all(self) -> list[SemanticEvent]:
        """Replay the full log in order (archives oldest-first, base last).

        Every stamped record must sit exactly at its global sequence position;
        legacy unstamped records (schema v1 logs) are accepted at any position.
        A trailing unterminated line in the active segment is an uncommitted
        crash artifact and is skipped.

        Raises:
            ValueError: If any line is not a valid event record or a stamped
                sequence is out of position (log corruption or truncation).
        """
        events: list[SemanticEvent] = []
        position = 0
        segments = self._segments()
        for index, segment in enumerate(segments):
            is_active = index == len(segments) - 1
            raw_lines = segment.read_bytes().split(b"\n")
            if raw_lines and raw_lines[-1] == b"":
                raw_lines.pop()  # artifact of splitting a complete file
            elif is_active and raw_lines:
                raw_lines.pop()  # uncommitted partial tail: no trailing newline
            for raw in raw_lines:
                position += 1
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = event_from_dict(json.loads(line))
                    if event.sequence is not None and event.sequence != position:
                        raise ValueError(
                            f"sequence discontinuity: record claims {event.sequence}, "
                            f"physical position is {position}"
                        )
                except ValueError as exc:
                    raise ValueError(f"{segment}:{position}: {exc}") from exc
                events.append(event)
        return events
