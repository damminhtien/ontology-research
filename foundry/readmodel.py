"""Materialized read model for operational queries.

Write model = append-only event log (correctness / provenance / history).
This module is the read side: plain in-memory indexes that answer the hot
operational queries in O(1)-ish without touching ontology or SPARQL - the
live embodiment of the rule "never make the operational query path pay for
semantic complexity it does not need".

Design contract (ADR-0004):
- Projector owns write semantics; ReadModel only exposes reads.
- In-memory scaffold: interfaces are stable so a real store (graph db /
  relational projection table) can replace internals in Phase 6 without
  changing call sites.
- Nothing is persisted here; rebuild = replay the log through the projector.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def parse_instant(text: str) -> datetime:
    """Parse an xsd:dateTime instant ('…Z' or offset) to an aware datetime.

    Raises:
        ValueError: On malformed input or a naive (timezone-less) value.
    """
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid instant {text!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"instant {text!r} must be timezone-aware")
    return parsed


def format_instant(moment: datetime) -> str:
    """Format an aware datetime as xsd:dateTime with trailing Z for UTC."""
    return moment.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class EntityView:
    """Projection of one entity as the operational API sees it."""

    entity_id: str
    entity_type: str
    name: str
    first_seen: str
    last_event_time: str


@dataclass(frozen=True)
class LocationFact:
    """The latest known location of an entity plus when it became known."""

    location_uri: str
    as_of: str
    source_ids: tuple[str, ...] = ()


@dataclass
class _EntityRecord:
    entity_type: str
    name: str
    first_seen: str
    last_event_time: str


@dataclass
class _LocationEntry:
    location_uri: str
    valid_from: datetime
    source_ids: tuple[str, ...]
    event_id: str


@dataclass
class _AssertionRecord:
    """One entry in the assertion ledger (docs/architecture.md §4.2)."""

    subject_id: str
    predicate: str
    object: dict
    valid_from: str
    valid_to: str | None
    source_ids: tuple[str, ...]
    confidence: float | None
    status: str  # asserted | superseded


class ReadModel:
    """In-memory read model over the canonical event stream.

    Thread safety is deliberately out of scope for the in-memory scaffold;
    a future store-backed implementation owns concurrency.
    """

    def __init__(self) -> None:
        """Start empty; the projector fills records."""
        self._entities: dict[str, _EntityRecord] = {}
        self._merged_records: dict[str, _EntityRecord] = {}
        self._location_history: dict[str, list[_LocationEntry]] = {}
        self._by_location: dict[str, set[str]] = {}
        self._current_location: dict[str, str] = {}
        self._merged_into: dict[str, str] = {}
        self._assertions: dict[str, _AssertionRecord] = {}
        self._applied_event_ids: set[str] = set()
        self._checkpoint_sequence: int | None = None
        self._last_event_time: datetime | None = None

    # -- write surface (called by the projector only) -----------------------

    def upsert_entity(
        self, entity_id: str, entity_type: str, name: str, event_time: datetime
    ) -> None:
        """Record that an entity exists; keeps first_seen, advances last_event."""
        existing = self._entities.get(entity_id)
        if existing is None:
            self._entities[entity_id] = _EntityRecord(
                entity_type=entity_type,
                name=name,
                first_seen=event_time.isoformat(),
                last_event_time=event_time.isoformat(),
            )
        elif event_time.isoformat() > existing.last_event_time:
            existing.last_event_time = event_time.isoformat()

    def add_location_observation(
        self,
        entity_id: str,
        location_uri: str,
        valid_from: datetime,
        source_ids: tuple[str, ...],
        event_id: str,
    ) -> None:
        """Append a location observation to an entity's temporal history."""
        entry = _LocationEntry(
            location_uri=location_uri,
            valid_from=valid_from,
            source_ids=source_ids,
            event_id=event_id,
        )
        history = self._location_history.setdefault(entity_id, [])
        history.append(entry)
        history.sort(key=lambda e: (e.valid_from, e.event_id))
        self._refresh_location_index(entity_id)

    def _refresh_location_index(self, entity_id: str) -> None:
        """Keep the reverse index correct after one entity's history changes.

        Streaming-safe: safe to call after every single apply instead of only
        at the end of a full replay.
        """
        history = self._location_history.get(entity_id)
        current = history[-1].location_uri if history else None
        previous = self._current_location.get(entity_id)
        if previous == current:
            return
        if previous is not None:
            bucket = self._by_location.get(previous)
            if bucket is not None:
                bucket.discard(entity_id)
                if not bucket:
                    del self._by_location[previous]
        if current is not None:
            self._by_location.setdefault(current, set()).add(entity_id)
        self._current_location[entity_id] = current

    def merge_entities(self, survivor_id: str, duplicate_id: str, event_time: datetime) -> None:
        """Fold a merged duplicate into its survivor (``EntityMerged`` projection).

        Moves the duplicate's location history onto the survivor, removes the
        duplicate from the entity and location indexes and records a redirect
        so point lookups on the old id can be chased. Idempotent: re-applying
        a merge is a no-op.
        """
        history = self._location_history.pop(duplicate_id, None)
        if history:
            target = self._location_history.setdefault(survivor_id, [])
            target.extend(history)
            target.sort(key=lambda e: (e.valid_from, e.event_id))
        record = self._entities.pop(duplicate_id, None)
        if record is not None:
            # tombstone: an EntitySplit can restore the entity view from here
            self._merged_records[duplicate_id] = record
        self._merged_into[duplicate_id] = survivor_id
        # assertions follow their subject through the merge
        for record in self._assertions.values():
            if record.subject_id == duplicate_id:
                record.subject_id = survivor_id
        self._refresh_location_index(survivor_id)
        self._refresh_location_index(duplicate_id)
        self._current_location.pop(duplicate_id, None)
        self.touch(event_time)

    def unmerge_entities(self, survivor_id: str, duplicate_id: str, event_time: datetime) -> None:
        """Undo an ``EntityMerged`` projection (``EntitySplit`` event).

        Restores the duplicate's entity view from the merge tombstone and
        clears the redirect. Location history stays with the survivor — the
        duplicate starts with an empty history; history re-partition belongs
        to the assertion model where every assertion names its subject.
        Idempotent: splitting an unmerged pair is a no-op.
        """
        record = self._merged_records.pop(duplicate_id, None)
        if record is None:
            return  # never merged (or already split): nothing to restore
        self._entities[duplicate_id] = record
        self._merged_into.pop(duplicate_id, None)
        self._refresh_location_index(survivor_id)
        self._refresh_location_index(duplicate_id)
        self.touch(event_time)

    # -- apply bookkeeping (per-event idempotency + checkpoint) ---------------

    def is_applied(self, event_id: str) -> bool:
        """True when this exact event was already folded into the model."""
        return event_id in self._applied_event_ids

    def mark_applied(self, event_id: str, sequence: int | None) -> None:
        """Record one applied event and advance the checkpoint watermark."""
        self._applied_event_ids.add(event_id)
        if sequence is not None and (
            self._checkpoint_sequence is None or sequence > self._checkpoint_sequence
        ):
            self._checkpoint_sequence = sequence

    @property
    def checkpoint_sequence(self) -> int | None:
        """Highest log sequence folded into the model (checkpoint anchor)."""
        return self._checkpoint_sequence

    @property
    def applied_event_count(self) -> int:
        """Number of distinct events applied (for checkpoint reporting)."""
        return len(self._applied_event_ids)

    def touch(self, event_time: datetime) -> None:
        """Track the newest event time seen for lag calculation."""
        if self._last_event_time is None or event_time > self._last_event_time:
            self._last_event_time = event_time

    # -- read surface ---------------------------------------------------------

    def get_entity(self, entity_id: str) -> EntityView | None:
        """Q1 point lookup by canonical id."""
        record = self._entities.get(entity_id)
        if record is None:
            return None
        return EntityView(
            entity_id=entity_id,
            entity_type=record.entity_type,
            name=record.name,
            first_seen=record.first_seen,
            last_event_time=record.last_event_time,
        )

    def merged_into(self, entity_id: str) -> str | None:
        """Survivor id when ``entity_id`` was merged away, else ``None``."""
        return self._merged_into.get(entity_id)

    def find_by_name(self, name: str) -> list[EntityView]:
        """Exact-name lookup (case-insensitive) - used by the demo query API."""
        needle = name.casefold()
        return [
            self.get_entity(entity_id)
            for entity_id, record in self._entities.items()
            if record.name.casefold() == needle
        ]

    def current_location(self, entity_id: str) -> LocationFact | None:
        """Latest location known for an entity (projection, not history)."""
        history = self._location_history.get(entity_id)
        if not history:
            return None
        latest = history[-1]
        return LocationFact(
            location_uri=latest.location_uri,
            as_of=latest.valid_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
            source_ids=latest.source_ids,
        )

    def location_as_of(self, entity_id: str, instant: datetime) -> LocationFact | None:
        """Q4 temporal: newest observation with valid_from <= instant."""
        history = self._location_history.get(entity_id)
        if not history:
            return None
        best: _LocationEntry | None = None
        for entry in history:
            if entry.valid_from <= instant:
                best = entry
        if best is None:
            return None
        return LocationFact(
            location_uri=best.location_uri,
            as_of=best.valid_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
            source_ids=best.source_ids,
        )

    def entities_at(self, location_uri: str) -> set[str]:
        """Entities whose CURRENT location is ``location_uri`` (reverse index)."""
        return set(self._by_location.get(location_uri, set()))

    def stats(self, now: datetime | None = None) -> dict:
        """Health numbers for monitoring: counts and projection lag in seconds."""
        from foundry.events import utc_now_iso

        current = now or parse_instant(utc_now_iso())
        lag_seconds = (
            (current - self._last_event_time).total_seconds()
            if self._last_event_time is not None
            else None
        )
        locations = {e.location_uri for hist in self._location_history.values() for e in hist}
        return {
            "entities": len(self._entities),
            "with_location": len(self._location_history),
            "locations": len(locations),
            "assertions": len(self._assertions),
            "last_event_time": (
                self._last_event_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                if self._last_event_time
                else None
            ),
            "lag_seconds": lag_seconds,
        }

    # -- assertion ledger (docs/architecture.md §4.2) -------------------------

    def add_assertion(self, payload: dict) -> None:
        """Fold one ``AssertionMade`` into the ledger."""
        self._assertions[payload["assertion_id"]] = _AssertionRecord(
            subject_id=payload["subject_id"],
            predicate=payload["predicate"],
            object=dict(payload["object"]),
            valid_from=payload["valid_from"],
            valid_to=payload.get("valid_to"),
            source_ids=tuple(payload.get("source_ids") or ()),
            confidence=payload.get("confidence"),
            status="asserted",
        )
        superseded = payload.get("supersedes")
        if superseded is not None and superseded in self._assertions:
            self._assertions[superseded].status = "superseded"

    def supersede_assertion(self, assertion_id: str) -> None:
        """Mark one assertion superseded.

        Raises:
            ValueError: If the assertion is not in the ledger (log corruption).
        """
        record = self._assertions.get(assertion_id)
        if record is None:
            raise ValueError(f"assertion {assertion_id!r} not in ledger; log is corrupt")
        record.status = "superseded"

    def get_assertion(self, assertion_id: str) -> dict | None:
        """Full ledger entry for one assertion (None when unknown)."""
        record = self._assertions.get(assertion_id)
        if record is None:
            return None
        return {
            "assertion_id": assertion_id,
            "subject_id": record.subject_id,
            "predicate": record.predicate,
            "object": dict(record.object),
            "valid_from": record.valid_from,
            "valid_to": record.valid_to,
            "source_ids": list(record.source_ids),
            "confidence": record.confidence,
            "status": record.status,
        }

    def active_assertions(self, subject_id: str, predicate: str | None = None) -> list[dict]:
        """Non-superseded assertions for a subject, optionally by predicate."""
        return [
            entry
            for entry in (
                self.get_assertion(aid)
                for aid, record in self._assertions.items()
                if record.subject_id == subject_id
                and record.status == "asserted"
                and (predicate is None or record.predicate == predicate)
            )
            if entry is not None
        ]

    # -- internal maintenance (projector-only, keeps indexes consistent) -------

    def save_snapshot(self, path) -> None:
        """Persist the model state for incremental resume (a cache, not truth).

        The event log stays the single source of truth (ADR-0002/0004); the
        snapshot only spares a server restart a full replay. Written
        atomically (tmp + rename).
        """
        path = path if isinstance(path, Path) else Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(pickle.dumps(self, protocol=pickle.HIGHEST_PROTOCOL))
        tmp.replace(path)

    @staticmethod
    def load_snapshot(path) -> ReadModel | None:
        """Restore a persisted model; ``None`` when missing or corrupt.

        A corrupt cache degrades to a full replay — it must never take the
        console down, so any deserialization problem is swallowed here.
        """
        path = path if isinstance(path, Path) else Path(path)
        if not path.exists():
            return None
        try:
            model = pickle.loads(path.read_bytes())
        except Exception:
            return None
        return model if isinstance(model, ReadModel) else None

    def rebuild_location_index(self) -> None:
        """Recompute the reverse index from history (used after full replay)."""
        self._by_location = {}
        for entity_id, history in self._location_history.items():
            if history:
                self._by_location.setdefault(history[-1].location_uri, set()).add(entity_id)
