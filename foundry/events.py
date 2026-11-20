"""Immutable domain events and an append-only event log.

Canonical knowledge changes are recorded as facts-on-a-log: events are never
mutated or deleted in place; corrections are new events that supersede older
ones. This module defines the event contract (versioned in the root ``VERSION``
file, history in ``CHANGELOG-DATA.md``) and a JSONL-backed append-only store.
The store is deliberately boring so the transport can be swapped for a
Kafka-style stream later without touching the payload contract.

Invariant: once appended, bytes in the log file are never rewritten.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from foundry.namespaces import DEFAULT_LITERAL_DATATYPE, resolve_legacy_predicate
from foundry.provenance import (
    content_sha256,
    normalize_evidence,
    normalize_source_id,
    normalize_source_ids,
    stable_source_record_key,
)
from foundry.versioning import EVENT_SCHEMA_VERSION

EVENT_TYPE_ENTITY_CREATED = "EntityCreated"
EVENT_TYPE_LOCATION_OBSERVED = "LocationObserved"
EVENT_TYPE_ENTITY_MERGED = "EntityMerged"
EVENT_TYPE_EXTERNAL_ID_BOUND = "ExternalIdBound"
EVENT_TYPE_SOURCE_RECORD_MATCHED = "SourceRecordMatched"
EVENT_TYPE_ENTITY_CANNOT_LINK = "EntityCannotLinkAsserted"
EVENT_TYPE_REVIEW_QUEUED = "ResolutionReviewQueued"
EVENT_TYPE_ENTITY_SPLIT = "EntitySplit"
EVENT_TYPE_DOCUMENT_REGISTERED = "DocumentRegistered"
EVENT_TYPE_ASSERTION_MADE = "AssertionMade"
EVENT_TYPE_ASSERTION_SUPERSEDED = "AssertionSuperseded"
EVENT_TYPE_SENSOR_REGISTERED = "SensorRegistered"
EVENT_TYPE_OBSERVATION_RECORDED = "ObservationRecorded"
EVENT_TYPE_TRACK_OBSERVED = "TrackObserved"

EVENT_TYPES = frozenset(
    {
        EVENT_TYPE_ENTITY_CREATED,
        EVENT_TYPE_ENTITY_MERGED,
        EVENT_TYPE_EXTERNAL_ID_BOUND,
        EVENT_TYPE_SOURCE_RECORD_MATCHED,
        EVENT_TYPE_ENTITY_CANNOT_LINK,
        EVENT_TYPE_REVIEW_QUEUED,
        EVENT_TYPE_ENTITY_SPLIT,
        EVENT_TYPE_DOCUMENT_REGISTERED,
        EVENT_TYPE_ASSERTION_MADE,
        EVENT_TYPE_ASSERTION_SUPERSEDED,
        EVENT_TYPE_SENSOR_REGISTERED,
        EVENT_TYPE_OBSERVATION_RECORDED,
        EVENT_TYPE_TRACK_OBSERVED,
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
    idempotency_key: str | None = None
    request_fingerprint: str | None = None
    operation_event_count: int | None = None


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
    normalized_payload = dict(payload)
    if normalized_payload.get("source_id"):
        normalized_payload["source_id"] = normalize_source_id(normalized_payload["source_id"])
    if "source_ids" in normalized_payload:
        normalized_payload["source_ids"] = list(
            normalize_source_ids(normalized_payload["source_ids"])
        )
    if event_type == EVENT_TYPE_ASSERTION_MADE and normalized_payload.get("evidence") is not None:
        sources, evidence = normalize_evidence(
            normalized_payload.get("source_ids") or (), normalized_payload["evidence"]
        )
        normalized_payload["source_ids"] = list(sources)
        normalized_payload["evidence"] = list(evidence)
    return SemanticEvent(
        event_id=uuid.uuid4().hex,
        event_type=event_type,
        schema_version=EVENT_SCHEMA_VERSION,
        occurred_at=utc_now_iso(),
        payload=normalized_payload,
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


def _upcast_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """v2 → v3: ``AssertionMade`` payload carries the semantic contract.

    v2 recorded the relation as a bare name (``locatedAt``) which the RDF
    mapping could not place in the graph — two assertions differing only in
    their relation mapped to identical RDF. v3 records the absolute property
    IRI (``…/ontology/core#locatedAt``) instead, and states a literal object's
    type explicitly (``datatype_iri`` XOR ``language``, ``xsd:string`` by
    default) so ``"250"`` no longer loses whether it was a string or a number.

    The v2 bare name is mapped through :data:`LEGACY_PREDICATE_IRIS` — an
    explicit table, never ``core#<name>`` for whatever name the record happens
    to carry. An unknown legacy name raises: failing loudly beats silently
    fabricating a relation IRI that no release ever declared.

    Only the payload of ``AssertionMade`` changes; every other event type just
    advances its version marker.

    Raises:
        ValueError: On a v2 ``AssertionMade`` carrying a bare relation name
            outside the legacy table, or a malformed object.
    """
    data = dict(data)
    data["schema_version"] = 3
    if data.get("event_type") == EVENT_TYPE_ASSERTION_MADE:
        payload = dict(data.get("payload") or {})
        predicate = payload.pop("predicate", None)
        if isinstance(predicate, str) and "predicate_iri" not in payload:
            payload["predicate_iri"] = resolve_legacy_predicate(predicate)

        obj = dict(payload.get("object") or {})
        if obj.get("kind") == "literal":
            obj.setdefault("datatype_iri", DEFAULT_LITERAL_DATATYPE)
            obj.setdefault("language", None)
        payload["object"] = obj
        data["payload"] = payload
    return data


def _upcast_v3_to_v4(data: dict[str, Any]) -> dict[str, Any]:
    """v3 → v4: use generic assertions and normalize temporal/provenance data.

    Historical ``LocationObserved`` records become ``AssertionMade`` records
    with the original event ID and sequence. Their assertion IDs are derived
    from the immutable event ID, so repeated replay produces the same claim.
    Unresolved subjects stay typed references in the payload and never become
    canonical entities. Observation events retain their position and typed
    measurement slots for the ``ObservationResult`` projection.
    """
    data = dict(data)
    data["schema_version"] = 4
    event_type = data.get("event_type")
    payload = dict(data.get("payload") or {})

    if event_type == EVENT_TYPE_LOCATION_OBSERVED:
        event_id = str(data.get("event_id", ""))
        entity_id = payload.get("entity_id") or ""
        subject_ref = None
        if not entity_id:
            from foundry.namespaces import pending_reference_iri

            surface_form = str(payload.get("entity_ref") or "").strip()
            subject_ref = {
                "reference_id": pending_reference_iri(surface_form),
                "surface_form": surface_form,
                "entity_type": payload.get("entity_type"),
            }
        source_ids, evidence = normalize_evidence(payload.get("source_ids") or ())
        payload = {
            "assertion_id": f"urn:assert:legacy:{event_id}",
            "subject_id": entity_id,
            "subject_ref": subject_ref,
            "predicate_iri": "https://damminhtien.github.io/ontology-research/ontology/core#locatedAt",
            "object": {"kind": "location", "value": payload["location_uri"]},
            "valid_from": payload.get("valid_from") or data.get("occurred_at"),
            "valid_until": None,
            "source_ids": list(source_ids),
            "evidence": list(evidence),
            "confidence": payload.get("confidence"),
            "supersedes": None,
        }
        data["event_type"] = EVENT_TYPE_ASSERTION_MADE
    elif event_type == EVENT_TYPE_ASSERTION_MADE:
        if "valid_until" not in payload:
            payload["valid_until"] = payload.pop("valid_to", None)
        source_ids, evidence = normalize_evidence(
            payload.get("source_ids") or (), payload.get("evidence")
        )
        payload["source_ids"] = list(source_ids)
        payload["evidence"] = list(evidence)
        payload.setdefault("subject_ref", None)
    elif event_type == EVENT_TYPE_OBSERVATION_RECORDED:
        payload.setdefault("position_uri", payload.pop("location_uri", None))
        payload.setdefault("measurements", [])
        source_ids, evidence = normalize_evidence(
            payload.get("source_ids") or (), payload.get("evidence")
        )
        payload["source_ids"] = list(source_ids)
        payload["evidence"] = list(evidence)
        payload.setdefault(
            "observation_result_id", f"urn:observation-result:{data.get('event_id', '')}"
        )
    else:
        if "source_ids" in payload:
            payload["source_ids"] = list(normalize_source_ids(payload["source_ids"]))
        if payload.get("source_id"):
            from foundry.provenance import normalize_source_id

            payload["source_id"] = normalize_source_id(payload["source_id"])

    data["payload"] = payload
    return data


def _upcast_v4_to_v5(data: dict[str, Any]) -> dict[str, Any]:
    """v4 → v5: add optional durable-operation idempotency metadata."""
    data = dict(data)
    data["schema_version"] = 5
    data.setdefault("idempotency_key", None)
    data.setdefault("request_fingerprint", None)
    data.setdefault("operation_event_count", None)
    return data


def _upcast_v5_to_v6(data: dict[str, Any]) -> dict[str, Any]:
    """v5 → v6: make document lineage and assertion time/score semantics explicit."""
    data = dict(data)
    data["schema_version"] = 6
    payload = dict(data.get("payload") or {})
    event_type = data.get("event_type")
    if event_type == EVENT_TYPE_ASSERTION_MADE:
        payload["recorded_at"] = payload.get("recorded_at") or data.get("occurred_at")
        if "model_score" not in payload:
            payload["model_score"] = payload.pop("confidence", None)
        else:
            payload.pop("confidence", None)
        payload.setdefault("evidence_quality", "unknown")
        payload.setdefault("status", "asserted")
    elif event_type == EVENT_TYPE_DOCUMENT_REGISTERED:
        source_system = str(payload.get("source_system") or "legacy")
        source_id = normalize_source_id(str(payload.get("source_id") or source_system))
        record_id = str(
            payload.get("source_record_id") or payload.get("uri") or payload["document_id"]
        )
        if not payload.get("content_hash"):
            stable_content = json.dumps(
                {
                    "uri": payload.get("uri"),
                    "title": payload.get("title"),
                    "content_ref": payload.get("content_ref"),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            payload["content_hash"] = content_sha256(stable_content)
        payload.setdefault("source_id", source_id)
        payload.setdefault("source_record_id", record_id)
        payload.setdefault("source_record_key", stable_source_record_key(source_id, record_id))
        payload.setdefault("recorded_at", payload.get("registered_at") or data.get("occurred_at"))
    elif event_type == EVENT_TYPE_ENTITY_CREATED:
        raw_source_id = payload.get("source_id")
        source_id = normalize_source_id(str(raw_source_id or "legacy"))
        record_id = str(payload.get("source_record_id") or f"event:{data.get('event_id', '')}")
        payload.setdefault("source_id", source_id)
        payload.setdefault("source_record_id", record_id)
        payload.setdefault("source_record_key", stable_source_record_key(source_id, record_id))
        if not payload.get("content_hash"):
            stable_content = json.dumps(
                {
                    key: payload.get(key)
                    for key in ("entity_type", "name", "name_aliases", "external_ids")
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            payload["content_hash"] = content_sha256(stable_content)
    data["payload"] = payload
    return data


#: Upcast chain: maps a record's schema version to the function rewriting it
#: one version forward. Every historical version must stay registered here —
#: removing one breaks replay of logs written under it.
UPCASTERS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    1: _upcast_v1_to_v2,
    2: _upcast_v2_to_v3,
    3: _upcast_v3_to_v4,
    4: _upcast_v4_to_v5,
    5: _upcast_v5_to_v6,
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
            idempotency_key=data.get("idempotency_key"),
            request_fingerprint=data.get("request_fingerprint"),
            operation_event_count=(
                int(data["operation_event_count"])
                if data.get("operation_event_count") is not None
                else None
            ),
        )
    except KeyError as exc:
        raise ValueError(f"event record missing field: {exc}") from exc

    if event.event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event.event_type!r}")
    return event


class EventLogLocked(RuntimeError):
    """Raised when another process holds the log's writer lock."""


class IdempotencyKeyConflict(ValueError):
    """Raised when one idempotency key is reused for a different request."""


@dataclass(frozen=True)
class AppendResult:
    """The events committed by an append operation."""

    events: tuple[SemanticEvent, ...]
    duplicate: bool


class EventLog:
    """Append-only JSONL event store with segmented, single-writer transport.

    Storage layout for a log bound to ``path`` (the *base*, e.g.
    ``data/production.jsonl``):

    - the base file is the **active segment** — appends land here;
    - when the active segment reaches ``max_segment_bytes``, or before the
      next record would exceed it, the segment is renamed to
      ``<stem>-000001.jsonl``, ``<stem>-000002.jsonl``, … (ordered archives)
      and a fresh base file takes over; reads visit archives oldest-first,
      the base last;
    - ``sequence`` is a global 1-based offset across all segments, so the log
      can be replayed, checkpointed and verified as one logical stream.

    Write discipline:

    - a writer holds an exclusive ``flock`` on ``<path>.lock`` for the whole
      append batch; a second concurrent writer fails fast with
      :class:`EventLogLocked` instead of interleaving lines;
    - a batch may span segments; each completed segment and the final active
      segment are flushed and ``fsync``-ed before the call returns — a reported
      success survives a crash;
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
            max_segment_bytes: Maximum segment size, except when one record by
                itself exceeds the limit.
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

    @property
    def index_path(self) -> Path:
        """Sidecar containing durable sequence and commit metadata."""
        return self._path.with_suffix(self._path.suffix + ".index.sqlite3")

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

    def has_data(self) -> bool:
        """Return whether any event has crossed the durable commit point."""
        return bool(self._indexed_segments())

    def fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        """Return segment paths, sizes, and mtimes in replay order.

        Consumers use this to invalidate projections when an archive is
        created or the active segment grows.
        """
        fingerprint = []
        for filename, _size, _first, _last in self._indexed_segments():
            segment = self._path.parent / filename
            stat = segment.stat()
            fingerprint.append((segment.as_posix(), stat.st_size, stat.st_mtime_ns))
        return tuple(fingerprint)

    def _read_lock(self):
        """Acquire a shared lock so readers cannot race a segment rename."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        return handle

    def _archive_name(self, number: int) -> str:
        """Return the filename for an archived segment number."""
        return f"{self._path.stem}-{number:0{self.ARCHIVE_DIGITS}d}{self._path.suffix}"

    def _connect(self) -> sqlite3.Connection:
        """Open the metadata index with full SQLite durability."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        initialize = not self.index_path.exists()
        connection = sqlite3.connect(self.index_path, timeout=30.0)
        connection.execute("PRAGMA synchronous=FULL")
        if initialize:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute(
                """CREATE TABLE segments (
                       order_index INTEGER PRIMARY KEY,
                       filename TEXT NOT NULL UNIQUE,
                       committed_bytes INTEGER NOT NULL,
                       first_sequence INTEGER NOT NULL,
                       last_sequence INTEGER NOT NULL
                   )"""
            )
            connection.execute(
                "CREATE TABLE event_ids (event_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL)"
            )
            connection.execute(
                """CREATE TABLE idempotency (
                       key TEXT PRIMARY KEY,
                       fingerprint TEXT NOT NULL,
                       first_sequence INTEGER NOT NULL,
                       last_sequence INTEGER NOT NULL,
                       events_json TEXT NOT NULL
                   )"""
            )
        return connection

    @staticmethod
    def _fsync_file(handle) -> None:
        """Flush a file object to stable storage."""
        handle.flush()
        os.fsync(handle.fileno())

    @staticmethod
    def _write_bytes(handle, data: bytes) -> None:
        """Write a complete byte sequence or fail on a short write."""
        written = handle.write(data)
        if written != len(data):
            raise OSError(f"short event-log write: {written} of {len(data)} bytes")

    def _fsync_directory(self) -> None:
        """Persist directory entry changes such as segment rotation."""
        descriptor = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _scan_legacy_segments(self) -> list[tuple[str, int, int, int]]:
        """One-time scan to initialize metadata for a pre-index event log."""
        result: list[tuple[str, int, int, int]] = []
        sequence = 0
        for segment in self._segments():
            raw = segment.read_bytes()
            if not raw.endswith(b"\n"):
                if segment != self._path:
                    raise ValueError(f"archived segment has an unterminated tail: {segment}")
                raw = raw[: raw.rfind(b"\n") + 1]
                with segment.open("r+b") as handle:
                    handle.truncate(len(raw))
                    self._fsync_file(handle)
            first_sequence = sequence + 1
            for line_number, raw_line in enumerate(raw.splitlines(keepends=True), start=1):
                sequence += 1
                try:
                    event = event_from_dict(json.loads(raw_line))
                except (ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"{segment}:{line_number}: {exc}") from exc
                if event.sequence is not None and event.sequence != sequence:
                    raise ValueError(
                        f"{segment}:{line_number}: sequence discontinuity: "
                        f"record claims {event.sequence}, physical position is {sequence}"
                    )
            if raw:
                result.append((segment.name, len(raw), first_sequence, sequence))
        return result

    @staticmethod
    def _segment_events(
        path: Path, segments: Iterable[tuple[str, int, int, int]]
    ) -> list[SemanticEvent]:
        """Read only the committed byte ranges during metadata migration."""
        events = []
        for filename, size, _first, _last in segments:
            with (path.parent / filename).open("rb") as handle:
                remaining = size
                while remaining:
                    raw = handle.readline(remaining)
                    if not raw.endswith(b"\n"):
                        raise ValueError(f"committed event record is truncated in {filename}")
                    events.append(event_from_dict(json.loads(raw)))
                    remaining -= len(raw)
        return events

    def _initialize_index(self, connection: sqlite3.Connection) -> None:
        """Migrate a legacy log once or initialize an empty one."""
        if connection.execute("SELECT 1 FROM metadata WHERE key='last_sequence'").fetchone():
            return
        segments = self._scan_legacy_segments() if self._segments() else []
        last_sequence = segments[-1][3] if segments else 0
        connection.execute("BEGIN IMMEDIATE")
        try:
            for order_index, row in enumerate(segments):
                connection.execute(
                    "INSERT INTO segments VALUES (?, ?, ?, ?, ?)",
                    (order_index, *row),
                )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('last_sequence', ?)",
                (str(last_sequence),),
            )
            events = self._segment_events(self._path, segments)
            for event in events:
                connection.execute(
                    "INSERT INTO event_ids(event_id, sequence) VALUES (?, ?)",
                    (event.event_id, event.sequence or 0),
                )
            batches: dict[str, list[SemanticEvent]] = {}
            for event in events:
                if event.idempotency_key:
                    batches.setdefault(event.idempotency_key, []).append(event)
            for key, group in batches.items():
                expected_count = group[0].operation_event_count or len(group)
                fingerprint = group[0].request_fingerprint
                if len(group) != expected_count or not fingerprint:
                    raise ValueError(f"incomplete idempotency metadata for operation {key!r}")
                encoded = json.dumps(
                    [event_to_dict(item) for item in group], ensure_ascii=False, sort_keys=True
                )
                connection.execute(
                    "INSERT INTO idempotency VALUES (?, ?, ?, ?, ?)",
                    (key, fingerprint, group[0].sequence or 0, group[-1].sequence or 0, encoded),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        self._fsync_directory()

    @staticmethod
    def _segment_rows(connection: sqlite3.Connection) -> list[tuple[str, int, int, int]]:
        return connection.execute(
            "SELECT filename, committed_bytes, first_sequence, last_sequence "
            "FROM segments ORDER BY order_index"
        ).fetchall()

    def _recover_files(self, connection: sqlite3.Connection) -> None:
        """Restore files to the byte offsets published by the last commit."""
        rows = self._segment_rows(connection)
        expected = {row[0]: row for row in rows}
        base_name = self._path.name
        archive_count = sum(filename != base_name for filename in expected)
        if base_name in expected and not self._path.exists():
            moved_active = self._path.with_name(self._archive_name(archive_count + 1))
            if moved_active.exists():
                moved_active.rename(self._path)
        changed_directory = False
        for segment in self._segments():
            if segment.name not in expected:
                segment.unlink()
                changed_directory = True
        for filename, committed_bytes, _first, _last in rows:
            segment = self._path.parent / filename
            if not segment.exists():
                raise ValueError(f"committed event-log segment is missing: {segment}")
            actual_size = segment.stat().st_size
            if actual_size < committed_bytes:
                raise ValueError(
                    f"committed event-log segment is truncated: {segment} "
                    f"({actual_size} < {committed_bytes})"
                )
            if actual_size > committed_bytes:
                with segment.open("r+b") as handle:
                    handle.truncate(committed_bytes)
                    self._fsync_file(handle)
        if changed_directory:
            self._fsync_directory()

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

        Segment bytes are durable before the sidecar publishes their sequence.

        Raises:
            EventLogLocked: If another writer holds the log lock.
        """
        self.commit([event])

    def extend(self, events: Iterable[SemanticEvent]) -> int:
        """Append many events in one locked, fsync-ed batch.

        Returns the number of events in the durable batch. The entire iterable
        is materialized before writing, and readers never observe a partial
        batch.

        Raises:
            EventLogLocked: If another writer holds the log lock.
        """
        return len(self.commit(events).events)

    def find_idempotency(
        self, key: str, *, request_fingerprint: str | None = None
    ) -> AppendResult | None:
        """Find a committed operation and verify that the key is not reused."""
        if not key.strip():
            raise ValueError("idempotency key must be non-empty")
        if not self.index_path.exists():
            self._ensure_index()
        lock = self._read_lock()
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT fingerprint, events_json FROM idempotency WHERE key=?", (key,)
                ).fetchone()
            finally:
                connection.close()
            if row is None:
                return None
            fingerprint, encoded = row
            if request_fingerprint is not None and request_fingerprint != fingerprint:
                raise IdempotencyKeyConflict(
                    f"idempotency key {key!r} was already committed for a different request"
                )
            committed = tuple(event_from_dict(item) for item in json.loads(encoded))
            return AppendResult(committed, True)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def commit(
        self,
        events: Iterable[SemanticEvent],
        *,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
    ) -> AppendResult:
        """Commit one logical event batch atomically and return stamped events.

        Data files are fsync-ed before the SQLite transaction publishes their
        committed byte offsets, the sequence allocator, event ids, and the
        idempotency result. SQLite's transaction commit is the batch boundary.
        A crash before it commits leaves bytes beyond the prior offsets, which
        readers ignore and the next writer truncates.
        """
        batch = tuple(events)
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency key must be non-empty")
        if idempotency_key is not None and not batch:
            raise ValueError("idempotent operation batch must contain at least one event")
        if not batch:
            return AppendResult((), False)
        if request_fingerprint is None:
            stable_request = [
                {
                    "event_type": event.event_type,
                    "payload": event.payload,
                    "valid_at": event.valid_at,
                }
                for event in batch
            ]
            request_fingerprint = hashlib.sha256(
                json.dumps(stable_request, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()

        lock = self._writer_lock()
        connection = None
        active_handle = None
        try:
            connection = self._connect()
            self._initialize_index(connection)
            self._recover_files(connection)
            old_rows = self._segment_rows(connection)

            if idempotency_key is not None:
                prior = connection.execute(
                    "SELECT fingerprint, events_json FROM idempotency WHERE key=?",
                    (idempotency_key,),
                ).fetchone()
                if prior is not None:
                    if prior[0] != request_fingerprint:
                        raise IdempotencyKeyConflict(
                            f"idempotency key {idempotency_key!r} was already committed "
                            "for a different request"
                        )
                    committed = tuple(event_from_dict(item) for item in json.loads(prior[1]))
                    return AppendResult(committed, True)

            last_sequence_row = connection.execute(
                "SELECT value FROM metadata WHERE key='last_sequence'"
            ).fetchone()
            sequence = int(last_sequence_row[0]) if last_sequence_row else 0
            stamped_events = []
            for event in batch:
                sequence += 1
                stamped_events.append(
                    replace(
                        event,
                        sequence=sequence,
                        idempotency_key=idempotency_key,
                        request_fingerprint=(request_fingerprint if idempotency_key else None),
                        operation_event_count=(len(batch) if idempotency_key else None),
                    )
                )

            for event in stamped_events:
                try:
                    event_from_dict(event_to_dict(event))
                except ValueError as exc:
                    raise ValueError(f"invalid event {event.event_id!r}: {exc}") from exc
                if connection.execute(
                    "SELECT 1 FROM event_ids WHERE event_id=?", (event.event_id,)
                ).fetchone():
                    raise ValueError(f"event id {event.event_id!r} is already committed")

            connection.execute("BEGIN IMMEDIATE")
            planned = [list(row) for row in old_rows]
            active_size = planned[-1][1] if planned and planned[-1][0] == self._path.name else 0
            changed_directory = False

            def sync_and_close() -> None:
                nonlocal active_handle
                if active_handle is not None:
                    self._fsync_file(active_handle)
                    active_handle.close()
                    active_handle = None

            def rotate_active() -> None:
                nonlocal active_size, changed_directory, planned
                sync_and_close()
                if active_size:
                    archive_numbers = [
                        int(Path(row[0]).stem[len(self._path.stem) + 1 :])
                        for row in planned
                        if row[0] != self._path.name
                        and Path(row[0]).stem[len(self._path.stem) + 1 :].isdigit()
                    ]
                    archive_name = self._archive_name(max(archive_numbers, default=0) + 1)
                    self._path.rename(self._path.with_name(archive_name))
                    for row in planned:
                        if row[0] == self._path.name:
                            row[0] = archive_name
                            break
                    changed_directory = True
                planned = [row for row in planned if row[0] != self._path.name]
                active_size = 0

            try:
                for event in stamped_events:
                    line = (
                        json.dumps(event_to_dict(event), ensure_ascii=False, sort_keys=True) + "\n"
                    ).encode("utf-8")
                    if active_size and active_size + len(line) > self._max_segment_bytes:
                        rotate_active()
                    if active_handle is None:
                        if not self._path.exists():
                            changed_directory = True
                        active_handle = self._path.open("ab")
                    self._write_bytes(active_handle, line)
                    if not planned or planned[-1][0] != self._path.name:
                        planned.append([self._path.name, 0, event.sequence, event.sequence])
                    active_row = planned[-1]
                    active_size += len(line)
                    active_row[1] = active_size
                    active_row[3] = event.sequence
                    if active_size >= self._max_segment_bytes:
                        sync_and_close()
                        rotate_active()
                sync_and_close()
                if changed_directory or any(
                    row[0] == self._path.name and not self._path.exists() for row in planned
                ):
                    self._fsync_directory()

                connection.execute("DELETE FROM segments")
                for order_index, row in enumerate(planned):
                    connection.execute(
                        "INSERT INTO segments VALUES (?, ?, ?, ?, ?)",
                        (order_index, row[0], row[1], row[2], row[3]),
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES ('last_sequence', ?)",
                    (str(sequence),),
                )
                for event in stamped_events:
                    connection.execute(
                        "INSERT INTO event_ids(event_id, sequence) VALUES (?, ?)",
                        (event.event_id, event.sequence),
                    )
                if idempotency_key is not None:
                    encoded = json.dumps(
                        [event_to_dict(event) for event in stamped_events],
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    connection.execute(
                        "INSERT INTO idempotency VALUES (?, ?, ?, ?, ?)",
                        (
                            idempotency_key,
                            request_fingerprint,
                            stamped_events[0].sequence,
                            stamped_events[-1].sequence,
                            encoded,
                        ),
                    )
                connection.commit()
            except BaseException:
                if active_handle is not None:
                    active_handle.close()
                    active_handle = None
                connection.rollback()
                self._recover_files_from_rows(old_rows)
                raise
            return AppendResult(tuple(stamped_events), False)
        finally:
            if connection is not None:
                connection.close()
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def _recover_files_from_rows(self, rows: list[tuple]) -> None:
        """Undo bytes and rotations that were not published by SQLite."""
        expected = {row[0]: row for row in rows}
        base_name = self._path.name
        archive_count = sum(filename != base_name for filename in expected)
        if base_name in expected and not self._path.exists():
            moved_active = self._path.with_name(self._archive_name(archive_count + 1))
            if moved_active.exists():
                moved_active.rename(self._path)
        changed_directory = False
        for segment in self._segments():
            if segment.name not in expected:
                segment.unlink()
                changed_directory = True
        for filename, committed_bytes, _first, _last in rows:
            segment = self._path.parent / filename
            if not segment.exists():
                raise ValueError(f"committed event-log segment is missing: {segment}")
            if segment.stat().st_size > committed_bytes:
                with segment.open("r+b") as handle:
                    handle.truncate(committed_bytes)
                    self._fsync_file(handle)
        if changed_directory:
            self._fsync_directory()

    def _ensure_index(self) -> None:
        """Initialize or migrate the sidecar under the writer lock."""
        lock = self._writer_lock()
        try:
            connection = self._connect()
            try:
                self._initialize_index(connection)
                self._recover_files(connection)
            finally:
                connection.close()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def _indexed_segments(self) -> list[tuple[str, int, int, int]]:
        """Return only segment ranges published by a successful commit."""
        if not self.index_path.exists():
            self._ensure_index()
        lock = self._read_lock()
        try:
            connection = self._connect()
            try:
                self._initialize_index(connection)
                return self._segment_rows(connection)
            finally:
                connection.close()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def iter_all(self) -> Iterator[SemanticEvent]:
        """Yield events inside the durable byte ranges, in sequence order."""
        if not self.index_path.exists():
            self._ensure_index()
        lock = self._read_lock()
        try:
            connection = self._connect()
            try:
                self._initialize_index(connection)
                segments = self._segment_rows(connection)
            finally:
                connection.close()
            position = 0
            for filename, committed_bytes, _first, _last in segments:
                segment = self._path.parent / filename
                with segment.open("rb") as handle:
                    remaining = committed_bytes
                    while remaining:
                        raw = handle.readline(remaining)
                        if not raw.endswith(b"\n"):
                            raise ValueError(f"committed event record is truncated in {segment}")
                        position += 1
                        try:
                            event = event_from_dict(json.loads(raw))
                            if event.sequence is not None and event.sequence != position:
                                raise ValueError(
                                    f"sequence discontinuity: record claims {event.sequence}, "
                                    f"physical position is {position}"
                                )
                        except (ValueError, json.JSONDecodeError) as exc:
                            raise ValueError(f"{segment}:{position}: {exc}") from exc
                        remaining -= len(raw)
                        yield event
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    @property
    def last_sequence(self) -> int:
        """Highest committed global sequence, or zero for an empty log."""
        segments = self._indexed_segments()
        return segments[-1][3] if segments else 0

    def iter_after_sequence(self, sequence: int) -> Iterator[SemanticEvent]:
        """Stream only committed events after a projection checkpoint.

        Segment metadata lets the reader skip complete segments without
        opening them. It validates sequence continuity in the first relevant
        segment onward; earlier segments were validated when their metadata
        was published or during full replay.
        """
        if sequence < 0:
            raise ValueError("sequence must be non-negative")
        if not self.index_path.exists():
            self._ensure_index()
        lock = self._read_lock()
        try:
            connection = self._connect()
            try:
                self._initialize_index(connection)
                segments = self._segment_rows(connection)
            finally:
                connection.close()
            for filename, committed_bytes, first, last in segments:
                if last <= sequence:
                    continue
                path = self._path.parent / filename
                position = first - 1
                with path.open("rb") as handle:
                    remaining = committed_bytes
                    while remaining:
                        raw = handle.readline(remaining)
                        if not raw.endswith(b"\n"):
                            raise ValueError(f"committed event record is truncated in {path}")
                        position += 1
                        try:
                            event = event_from_dict(json.loads(raw))
                            if event.sequence is not None and event.sequence != position:
                                raise ValueError(
                                    f"sequence discontinuity: record claims {event.sequence}, "
                                    f"physical position is {position}"
                                )
                        except (ValueError, json.JSONDecodeError) as exc:
                            raise ValueError(f"{path}:{position}: {exc}") from exc
                        remaining -= len(raw)
                        if position > sequence:
                            if event.sequence is None:
                                event = replace(event, sequence=position)
                            yield event
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def read_all(self) -> list[SemanticEvent]:
        """Replay the full log into a list for callers that need materialization."""
        return list(self.iter_all())
