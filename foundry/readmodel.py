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
from datetime import UTC, datetime
from pathlib import Path

from foundry.namespaces import CORE_LOCATED_AT, normalize_surface, require_absolute_iri


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
    """One unique location value active at a valid-time instant."""

    location_uri: str
    as_of: str
    source_ids: tuple[str, ...] = ()
    assertion_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocationResolution:
    """Location lookup outcome; conflicting active values stay explicit."""

    status: str  # none | unique | conflict
    fact: LocationFact | None = None
    conflicts: tuple[LocationFact, ...] = ()


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
    valid_until: datetime | None
    source_ids: tuple[str, ...]
    assertion_id: str


@dataclass
class _TrackRecord:
    """One derived track hypothesis (tracking vertical, Phase 4)."""

    entity_id: str
    subject_name: str
    observation_ids: tuple[str, ...]
    source_ids: tuple[str, ...]


@dataclass
class _AssertionRecord:
    """One entry in the assertion ledger (docs/architecture.md §4.2)."""

    subject_id: str
    predicate_iri: str
    object: dict
    valid_from: str
    valid_until: str | None
    source_ids: tuple[str, ...]
    evidence: tuple[dict, ...]
    confidence: float | None
    status: str  # asserted | superseded
    original_subject_id: str
    subject_ref: dict | None = None
    subject_status: str = "resolved"


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
        self._pending_assertions: dict[str, set[str]] = {}
        self._name_owners: dict[str, set[str]] = {}
        self._names_by_entity: dict[str, set[str]] = {}
        self._by_location: dict[str, set[str]] = {}
        self._current_location: dict[str, str] = {}
        self._merged_into: dict[str, str] = {}
        self._assertions: dict[str, _AssertionRecord] = {}
        self._tracks: dict[str, _TrackRecord] = {}
        self._applied_event_ids: set[str] = set()
        self._checkpoint_sequence: int | None = None
        self._last_event_time: datetime | None = None

    # -- write surface (called by the projector only) -----------------------

    def upsert_entity(
        self,
        entity_id: str,
        entity_type: str,
        name: str,
        event_time: datetime,
        aliases: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Record an entity and link references whose names now resolve uniquely."""
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
        for surface in (name, *aliases):
            normalized = normalize_surface(surface)
            if not normalized:
                continue
            self._name_owners.setdefault(normalized, set()).add(entity_id)
            self._names_by_entity.setdefault(entity_id, set()).add(normalized)
            self._resolve_pending_references(normalized)

    def _canonical_id(self, entity_id: str) -> str:
        """Follow recorded entity merges to the current canonical ID."""
        visited: set[str] = set()
        while entity_id in self._merged_into and entity_id not in visited:
            visited.add(entity_id)
            entity_id = self._merged_into[entity_id]
        return entity_id

    def _location_entry(self, assertion_id: str, record: _AssertionRecord) -> _LocationEntry | None:
        if (
            record.predicate_iri != CORE_LOCATED_AT
            or record.object.get("kind") != "location"
            or record.status != "asserted"
        ):
            return None
        return _LocationEntry(
            location_uri=record.object["value"],
            valid_from=parse_instant(record.valid_from),
            valid_until=parse_instant(record.valid_until) if record.valid_until else None,
            source_ids=record.source_ids,
            assertion_id=assertion_id,
        )

    def _attach_location_claim(self, assertion_id: str, record: _AssertionRecord) -> None:
        if not record.subject_id:
            return
        entry = self._location_entry(assertion_id, record)
        if entry is None:
            return
        history = self._location_history.setdefault(record.subject_id, [])
        history[:] = [item for item in history if item.assertion_id != assertion_id]
        history.append(entry)
        history.sort(key=lambda item: (item.valid_from, item.assertion_id))
        self._refresh_location_index(record.subject_id)

    def _remove_location_claim(self, assertion_id: str, entity_id: str) -> None:
        history = self._location_history.get(entity_id)
        if history is None:
            return
        history[:] = [item for item in history if item.assertion_id != assertion_id]
        if not history:
            self._location_history.pop(entity_id, None)
        self._refresh_location_index(entity_id)

    def _resolve_pending_references(self, normalized_surface: str) -> int:
        assertion_ids = {
            assertion_id
            for assertion_id, record in self._assertions.items()
            if record.subject_ref
            and normalize_surface(record.subject_ref["surface_form"]) == normalized_surface
        }
        linked = 0
        for assertion_id in assertion_ids:
            record = self._assertions.get(assertion_id)
            if record is None:
                continue
            expected_type = record.subject_ref.get("entity_type") if record.subject_ref else None
            owners = {
                self._canonical_id(entity_id)
                for entity_id in self._name_owners.get(normalized_surface, set())
                if expected_type is None
                or (
                    self._entities.get(self._canonical_id(entity_id), None) is not None
                    and self._entities[self._canonical_id(entity_id)].entity_type == expected_type
                )
            }
            previous_subject = record.subject_id
            if len(owners) == 1:
                record.subject_id = next(iter(owners))
                record.subject_status = "resolved"
                self._pending_assertions.get(normalized_surface, set()).discard(assertion_id)
            else:
                record.subject_id = ""
                record.subject_status = "ambiguous" if owners else "unresolved"
                self._pending_assertions.setdefault(normalized_surface, set()).add(assertion_id)
            if previous_subject and previous_subject != record.subject_id:
                self._remove_location_claim(assertion_id, previous_subject)
            if record.subject_id:
                self._attach_location_claim(assertion_id, record)
                if previous_subject != record.subject_id:
                    linked += 1
        if not self._pending_assertions.get(normalized_surface):
            self._pending_assertions.pop(normalized_surface, None)
        return linked

    @property
    def pending_count(self) -> int:
        """Assertions whose noncanonical subject is unresolved or ambiguous."""
        return sum(
            1
            for record in self._assertions.values()
            if record.subject_ref is not None and not record.subject_id
        )

    def _refresh_location_index(self, entity_id: str) -> None:
        """Keep the reverse index correct after one entity's history changes.

        Streaming-safe: safe to call after every single apply instead of only
        at the end of a full replay.
        """
        resolution = self.location_resolution(entity_id, datetime.now(UTC))
        current = resolution.fact.location_uri if resolution.status == "unique" else None
        previous = self._current_location.get(entity_id)
        if previous == current and (current is not None or entity_id not in self._current_location):
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
        else:
            self._current_location.pop(entity_id, None)

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
            target.sort(key=lambda e: (e.valid_from, e.assertion_id))
        record = self._entities.pop(duplicate_id, None)
        if record is not None:
            # tombstone: an EntitySplit can restore the entity view from here
            self._merged_records[duplicate_id] = record
        self._merged_into[duplicate_id] = survivor_id
        # assertions follow their subject through the merge
        for assertion_id, record in self._assertions.items():
            if record.subject_id == duplicate_id:
                record.subject_id = survivor_id
                self._attach_location_claim(assertion_id, record)
        for normalized in self._names_by_entity.get(duplicate_id, set()):
            self._resolve_pending_references(normalized)
        self._refresh_location_index(survivor_id)
        self._current_location.pop(duplicate_id, None)
        self.touch(event_time)

    def unmerge_entities(self, survivor_id: str, duplicate_id: str, event_time: datetime) -> None:
        """Undo an ``EntityMerged`` projection (``EntitySplit`` event).

        Restores the duplicate's entity view from the merge tombstone and
        clears the redirect. Assertions whose original subject was the
        duplicate move back with it, so the location indexes match the
        restored identity split.
        Idempotent: splitting an unmerged pair is a no-op.
        """
        record = self._merged_records.pop(duplicate_id, None)
        if record is None:
            return  # never merged (or already split): nothing to restore
        self._entities[duplicate_id] = record
        self._merged_into.pop(duplicate_id, None)
        for assertion_id, assertion in self._assertions.items():
            if assertion.original_subject_id == duplicate_id:
                if assertion.subject_id and assertion.subject_id != duplicate_id:
                    self._remove_location_claim(assertion_id, assertion.subject_id)
                assertion.subject_id = duplicate_id
                self._attach_location_claim(assertion_id, assertion)
        for normalized in self._names_by_entity.get(duplicate_id, set()):
            self._resolve_pending_references(normalized)
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
        """Return the unique location active now, or ``None`` for none/conflict."""
        return self.location_resolution(entity_id, datetime.now(UTC)).fact

    def location_as_of(self, entity_id: str, instant: datetime) -> LocationFact | None:
        """Return the unique location active at ``instant`` under [from, until)."""
        return self.location_resolution(entity_id, instant).fact

    def location_resolution(self, entity_id: str, instant: datetime) -> LocationResolution:
        """Resolve location claims without silently choosing between conflicts."""
        if instant.tzinfo is None:
            raise ValueError("location lookup instant must be timezone-aware")
        canonical_id = self._canonical_id(entity_id)
        history = self._location_history.get(canonical_id, ())
        grouped: dict[str, list[_LocationEntry]] = {}
        for entry in history:
            assertion = self._assertions.get(entry.assertion_id)
            if assertion is None or assertion.status != "asserted":
                continue
            if entry.valid_from <= instant and (
                entry.valid_until is None or instant < entry.valid_until
            ):
                grouped.setdefault(entry.location_uri, []).append(entry)
        facts = tuple(
            LocationFact(
                location_uri=location_uri,
                as_of=format_instant(max(entry.valid_from for entry in entries)),
                source_ids=tuple(
                    sorted({source for entry in entries for source in entry.source_ids})
                ),
                assertion_ids=tuple(sorted(entry.assertion_id for entry in entries)),
            )
            for location_uri, entries in sorted(grouped.items())
        )
        if not facts:
            return LocationResolution("none")
        if len(facts) == 1:
            return LocationResolution("unique", fact=facts[0])
        return LocationResolution("conflict", conflicts=facts)

    def location_conflicts(
        self, entity_id: str, instant: datetime | None = None
    ) -> tuple[LocationFact, ...]:
        """Return all active distinct location values when the lookup conflicts."""
        at = instant or datetime.now(UTC)
        result = self.location_resolution(entity_id, at)
        return result.conflicts if result.status == "conflict" else ()

    def entities_at(self, location_uri: str, instant: datetime | None = None) -> set[str]:
        """Entities uniquely at ``location_uri`` at an instant.

        The query checks current assertion validity so an interval ending does
        not leave an expired value in the reverse projection.
        """
        at = instant or datetime.now(UTC)
        return {
            entity_id
            for entity_id in self._location_history
            if (fact := self.location_resolution(entity_id, at)).status == "unique"
            and fact.fact is not None
            and fact.fact.location_uri == location_uri
        }

    def stats(self, now: datetime | None = None) -> dict:
        """Health numbers for monitoring: counts and projection lag in seconds."""
        from foundry.events import utc_now_iso

        current = now or parse_instant(utc_now_iso())
        lag_seconds = (
            (current - self._last_event_time).total_seconds()
            if self._last_event_time is not None
            else None
        )
        current_facts = [
            resolution.fact
            for entity_id in self._location_history
            if (resolution := self.location_resolution(entity_id, current)).status == "unique"
            and resolution.fact is not None
        ]
        locations = {fact.location_uri for fact in current_facts}
        return {
            "entities": len(self._entities),
            "with_location": len(current_facts),
            "locations": len(locations),
            "assertions": len(self._assertions),
            "tracks": len(self._tracks),
            "pending_observations": self.pending_count,
            "pending_assertions": self.pending_count,
            "last_event_time": (
                self._last_event_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                if self._last_event_time
                else None
            ),
            "lag_seconds": lag_seconds,
        }

    # -- track store (tracking vertical, Phase 4) ------------------------------

    def add_track(self, payload: dict) -> None:
        """Fold one ``TrackObserved`` into the track store."""
        self._tracks[payload["track_id"]] = _TrackRecord(
            entity_id=payload["entity_id"],
            subject_name=payload.get("subject_name", ""),
            observation_ids=tuple(payload.get("observation_ids") or ()),
            source_ids=tuple(payload.get("source_ids") or ()),
        )

    def get_track(self, track_id: str) -> dict | None:
        """Ledger entry for one track (None when unknown)."""
        record = self._tracks.get(track_id)
        if record is None:
            return None
        return {
            "track_id": track_id,
            "entity_id": record.entity_id,
            "subject_name": record.subject_name,
            "observation_ids": list(record.observation_ids),
            "source_ids": list(record.source_ids),
        }

    def tracks_of(self, entity_id: str) -> list[dict]:
        """All tracks hypothesized about one entity."""
        return [
            self.get_track(track_id)
            for track_id, record in self._tracks.items()
            if record.entity_id == entity_id
        ]

    # -- assertion ledger (docs/architecture.md §4.2) -------------------------

    def add_assertion(self, payload: dict) -> None:
        """Fold one ``AssertionMade`` into the ledger."""
        assertion_id = payload["assertion_id"]
        subject_ref = payload.get("subject_ref")
        subject_id = payload.get("subject_id") or ""
        if subject_id:
            subject_id = self._canonical_id(subject_id)
        record = _AssertionRecord(
            subject_id=subject_id,
            predicate_iri=payload["predicate_iri"],
            object=dict(payload["object"]),
            valid_from=payload["valid_from"],
            valid_until=payload.get("valid_until", payload.get("valid_to")),
            source_ids=tuple(payload.get("source_ids") or ()),
            evidence=tuple(dict(item) for item in (payload.get("evidence") or ())),
            confidence=payload.get("confidence"),
            status="asserted",
            original_subject_id=payload.get("subject_id") or "",
            subject_ref=dict(subject_ref) if subject_ref else None,
            subject_status="unresolved" if subject_ref else "resolved",
        )
        self._assertions[assertion_id] = record
        superseded = payload.get("supersedes")
        if superseded is not None and superseded in self._assertions:
            previous = self._assertions[superseded]
            previous.status = "superseded"
            if previous.subject_id:
                self._refresh_location_index(previous.subject_id)
        if record.subject_ref:
            normalized = normalize_surface(record.subject_ref["surface_form"])
            self._pending_assertions.setdefault(normalized, set()).add(assertion_id)
            self._resolve_pending_references(normalized)
        else:
            self._attach_location_claim(assertion_id, record)

    def supersede_assertion(self, assertion_id: str) -> None:
        """Mark one assertion superseded.

        Raises:
            ValueError: If the assertion is not in the ledger (log corruption).
        """
        record = self._assertions.get(assertion_id)
        if record is None:
            raise ValueError(f"assertion {assertion_id!r} not in ledger; log is corrupt")
        record.status = "superseded"
        if record.subject_id:
            self._refresh_location_index(record.subject_id)

    def get_assertion(self, assertion_id: str) -> dict | None:
        """Full ledger entry for one assertion (None when unknown)."""
        record = self._assertions.get(assertion_id)
        if record is None:
            return None
        return {
            "assertion_id": assertion_id,
            "subject_id": record.subject_id,
            "subject_ref": dict(record.subject_ref) if record.subject_ref else None,
            "subject_status": record.subject_status,
            "predicate_iri": record.predicate_iri,
            "object": dict(record.object),
            "valid_from": record.valid_from,
            "valid_until": record.valid_until,
            "source_ids": list(record.source_ids),
            "evidence": [dict(item) for item in record.evidence],
            "confidence": record.confidence,
            "status": record.status,
        }

    def active_assertions(self, subject_id: str, predicate_iri: str | None = None) -> list[dict]:
        """Non-superseded assertions for a subject, optionally by relation.

        ``predicate_iri`` is the absolute relation IRI the ledger stores
        (``core:memberOf``); a bare local name is rejected rather than
        resolved, so a query can never silently match the wrong relation.

        Raises:
            ValueError: On a blank or relative ``predicate_iri``.
        """
        wanted = (
            require_absolute_iri(predicate_iri, "predicate_iri")
            if predicate_iri is not None
            else None
        )
        return [
            entry
            for entry in (
                self.get_assertion(aid)
                for aid, record in self._assertions.items()
                if record.subject_id == subject_id
                and record.status == "asserted"
                and (wanted is None or record.predicate_iri == wanted)
            )
            if entry is not None
        ]

    def assertions_as_of(
        self,
        subject_id: str,
        instant: datetime,
        predicate_iri: str | None = None,
    ) -> list[dict]:
        """Return resolved, non-superseded assertions valid at one instant.

        Validity is half-open: ``valid_from <= instant < valid_until``. An
        omitted end has no upper bound. This temporal read applies to every
        predicate, including relationships such as ``core:memberOf``; location
        lookups use the specialized conflict-aware ``location_resolution``.
        """
        if instant.tzinfo is None:
            raise ValueError("instant must be timezone-aware")
        wanted = (
            require_absolute_iri(predicate_iri, "predicate_iri")
            if predicate_iri is not None
            else None
        )
        canonical_subject = self._canonical_id(subject_id)
        matches = []
        for assertion_id, record in self._assertions.items():
            if (
                record.subject_id != canonical_subject
                or record.status != "asserted"
                or record.subject_status != "resolved"
                or (wanted is not None and record.predicate_iri != wanted)
            ):
                continue
            start = parse_instant(record.valid_from)
            end = parse_instant(record.valid_until) if record.valid_until else None
            if start <= instant and (end is None or instant < end):
                entry = self.get_assertion(assertion_id)
                if entry is not None:
                    matches.append(entry)
        return matches

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
        """Recompute unique-current-location indexes from active assertions."""
        self._by_location = {}
        self._current_location = {}
        for entity_id in self._location_history:
            self._refresh_location_index(entity_id)
