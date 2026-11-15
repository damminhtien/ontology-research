"""PostgreSQL-backed operational projection with indexed temporal queries."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from foundry.namespaces import CORE_LOCATED_AT, normalize_surface, require_absolute_iri
from foundry.postgres import PostgresDatabase
from foundry.readmodel import (
    EntityView,
    LocationFact,
    LocationResolution,
    format_instant,
    parse_instant,
)


class PostgresReadModelStore:
    """Persistent read store used by a standalone projector and the API.

    Every event is applied under one PostgreSQL transaction. The projector
    checkpoint, idempotency row, identity facts, and operational projections
    commit together.
    """

    def __init__(self, database: PostgresDatabase) -> None:
        """Use the shared operational database."""
        self.database = database

    @contextmanager
    def transaction(self) -> Iterator[object]:
        """Serialize projectors and share one transaction across all writes."""
        with self.database.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(628041902117)")
            yield connection

    def is_applied(self, event_id: str) -> bool:
        """Check the durable processed-event ledger."""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM projection_events WHERE event_id = %s", (event_id,)
            ).fetchone()
        return row is not None

    def mark_applied(self, event_id: str, sequence: int | None) -> None:
        """Persist an applied event and advance the committed watermark."""
        self._mark_processed(event_id, sequence, applied=True)

    def mark_skipped(self, event_id: str, sequence: int | None) -> None:
        """Persist an unknown event as processed without counting it as applied."""
        self._mark_processed(event_id, sequence, applied=False)

    def _mark_processed(self, event_id: str, sequence: int | None, *, applied: bool) -> None:
        with self.database.connection() as connection:
            inserted = connection.execute(
                """INSERT INTO projection_events(event_id, sequence, applied)
                   VALUES (%s, %s, %s) ON CONFLICT (event_id) DO NOTHING
                   RETURNING event_id""",
                (event_id, sequence, applied),
            ).fetchone()
            if inserted is None:
                return
            connection.execute(
                """UPDATE projection_state
                   SET checkpoint_sequence = CASE
                         WHEN %s::bigint IS NULL THEN checkpoint_sequence
                         ELSE GREATEST(COALESCE(checkpoint_sequence, 0), %s)
                       END,
                       applied_count = applied_count + %s
                   WHERE singleton = TRUE""",
                (sequence, sequence, int(applied)),
            )

    @property
    def checkpoint_sequence(self) -> int | None:
        """Return the transactionally committed log watermark."""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT checkpoint_sequence FROM projection_state WHERE singleton = TRUE"
            ).fetchone()
        return row[0] if row else None

    @property
    def applied_event_count(self) -> int:
        """Return the number of recognized events projected so far."""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT applied_count FROM projection_state WHERE singleton = TRUE"
            ).fetchone()
        return row[0] if row else 0

    def touch(self, event_time: datetime) -> None:
        """Advance the last-event timestamp for lag reporting."""
        with self.database.connection() as connection:
            connection.execute(
                """UPDATE projection_state
                   SET last_event_time = GREATEST(last_event_time, %s)
                   WHERE singleton = TRUE""",
                (event_time,),
            )

    def upsert_entity(
        self,
        entity_id: str,
        entity_type: str,
        name: str,
        event_time: datetime,
        aliases: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Upsert a canonical entity and its searchable name surfaces."""
        event_text = event_time.isoformat()
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO read_entities
                       (entity_id, entity_type, name, name_key, first_seen, last_event_time)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (entity_id) DO UPDATE
                   SET last_event_time = CASE
                       WHEN read_entities.last_event_time::timestamptz < %s::timestamptz
                       THEN EXCLUDED.last_event_time ELSE read_entities.last_event_time END""",
                (entity_id, entity_type, name, name.casefold(), event_text, event_text, event_text),
            )
            for surface in (name, *aliases):
                normalized = normalize_surface(surface)
                if not normalized:
                    continue
                connection.execute(
                    """INSERT INTO read_entity_names(entity_id, normalized_name, surface)
                       VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                    (entity_id, normalized, surface),
                )
                self._resolve_pending_references(normalized)

    def _canonical_id(self, entity_id: str) -> str:
        with self.database.connection() as connection:
            row = connection.execute(
                """WITH RECURSIVE lineage(entity_id, merged_into, depth) AS (
                       SELECT entity_id, merged_into, 0 FROM read_entities WHERE entity_id = %s
                       UNION ALL
                       SELECT e.entity_id, e.merged_into, lineage.depth + 1
                       FROM read_entities e JOIN lineage ON e.entity_id = lineage.merged_into
                       WHERE lineage.depth < 100
                   )
                   SELECT entity_id FROM lineage WHERE merged_into IS NULL
                   ORDER BY depth DESC LIMIT 1""",
                (entity_id,),
            ).fetchone()
        return row[0] if row else entity_id

    def _resolve_pending_references(self, normalized_surface: str) -> None:
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT assertion_id, subject_ref, subject_id
                   FROM read_assertions WHERE subject_ref_surface_norm = %s""",
                (normalized_surface,),
            ).fetchall()
            owner_rows = connection.execute(
                """SELECT DISTINCT entity_id FROM read_entity_names
                   WHERE normalized_name = %s""",
                (normalized_surface,),
            ).fetchall()
        owner_ids = {self._canonical_id(owner[0]) for owner in owner_rows}
        for assertion_id, subject_ref, _previous_subject in rows:
            expected_type = subject_ref.get("entity_type") if subject_ref else None
            owners = {
                owner_id
                for owner_id in owner_ids
                if expected_type is None
                or (
                    (view := self.get_entity(owner_id)) is not None
                    and view.entity_type == expected_type
                )
            }
            subject_id = next(iter(owners)) if len(owners) == 1 else ""
            status = "resolved" if subject_id else "ambiguous" if owners else "unresolved"
            with self.database.connection() as connection:
                connection.execute(
                    """UPDATE read_assertions SET subject_id = %s, subject_status = %s
                       WHERE assertion_id = %s""",
                    (subject_id, status, assertion_id),
                )

    @property
    def pending_count(self) -> int:
        """Count unresolved or ambiguous assertion subjects."""
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT count(*) FROM read_assertions
                   WHERE subject_ref IS NOT NULL AND subject_id = ''"""
            ).fetchone()
        return row[0]

    def merge_entities(self, survivor_id: str, duplicate_id: str, event_time: datetime) -> None:
        """Fold assertions and lookups through a canonical merge."""
        with self.database.connection() as connection:
            connection.execute(
                """UPDATE read_entities SET merged_into = %s WHERE entity_id = %s""",
                (survivor_id, duplicate_id),
            )
            connection.execute(
                "UPDATE read_assertions SET subject_id = %s WHERE subject_id = %s",
                (survivor_id, duplicate_id),
            )
            surfaces = connection.execute(
                "SELECT DISTINCT normalized_name FROM read_entity_names WHERE entity_id = %s",
                (duplicate_id,),
            ).fetchall()
        for (surface,) in surfaces:
            self._resolve_pending_references(surface)
        self.touch(event_time)

    def unmerge_entities(self, survivor_id: str, duplicate_id: str, event_time: datetime) -> None:
        """Restore a split entity and assertions with its original subject."""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT merged_into FROM read_entities WHERE entity_id = %s", (duplicate_id,)
            ).fetchone()
            if row is None or row[0] != survivor_id:
                return
            connection.execute(
                "UPDATE read_entities SET merged_into = NULL WHERE entity_id = %s", (duplicate_id,)
            )
            connection.execute(
                """UPDATE read_assertions SET subject_id = %s
                   WHERE original_subject_id = %s""",
                (duplicate_id, duplicate_id),
            )
            surfaces = connection.execute(
                "SELECT DISTINCT normalized_name FROM read_entity_names WHERE entity_id = %s",
                (duplicate_id,),
            ).fetchall()
        for (surface,) in surfaces:
            self._resolve_pending_references(surface)
        self.touch(event_time)

    def is_applied_event(self, event_id: str) -> bool:
        """Compatibility alias used by inspection utilities."""
        return self.is_applied(event_id)

    def add_assertion(self, payload: dict) -> None:
        """Index one assertion with provenance and half-open valid time."""
        assertion_id = payload["assertion_id"]
        subject_ref = payload.get("subject_ref")
        original_subject = payload.get("subject_id") or ""
        subject = self._canonical_id(original_subject) if original_subject else ""
        valid_from = payload["valid_from"]
        valid_until = payload.get("valid_until", payload.get("valid_to"))
        subject_surface = normalize_surface(subject_ref["surface_form"]) if subject_ref else None
        object_value = dict(payload["object"])
        object_entity_id = (
            str(object_value.get("value"))
            if object_value.get("kind") == "entity" and object_value.get("value") is not None
            else None
        )
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO read_assertions(
                       assertion_id, subject_id, original_subject_id, subject_ref,
                       subject_ref_surface_norm, subject_status, predicate_iri, object,
                       object_entity_id, valid_from, valid_from_text, valid_until,
                       valid_until_text, recorded_at, source_ids, evidence, model_score,
                       evidence_quality, status, superseded_by, superseded_reason
                   ) VALUES (
                       %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s,
                       %s::timestamptz, %s, %s::timestamptz, %s, %s, %s::jsonb,
                       %s::jsonb, %s, %s, 'asserted', NULL, NULL
                   ) ON CONFLICT (assertion_id) DO UPDATE SET
                       subject_id = EXCLUDED.subject_id,
                       original_subject_id = EXCLUDED.original_subject_id,
                       subject_ref = EXCLUDED.subject_ref,
                       subject_ref_surface_norm = EXCLUDED.subject_ref_surface_norm,
                       subject_status = EXCLUDED.subject_status,
                       predicate_iri = EXCLUDED.predicate_iri,
                       object = EXCLUDED.object,
                       object_entity_id = EXCLUDED.object_entity_id,
                       valid_from = EXCLUDED.valid_from,
                       valid_from_text = EXCLUDED.valid_from_text,
                       valid_until = EXCLUDED.valid_until,
                       valid_until_text = EXCLUDED.valid_until_text,
                       recorded_at = EXCLUDED.recorded_at,
                       source_ids = EXCLUDED.source_ids,
                       evidence = EXCLUDED.evidence,
                       model_score = EXCLUDED.model_score,
                       evidence_quality = EXCLUDED.evidence_quality,
                       status = 'asserted', superseded_by = NULL, superseded_reason = NULL""",
                (
                    assertion_id,
                    subject,
                    original_subject,
                    json.dumps(subject_ref) if subject_ref else None,
                    subject_surface,
                    "unresolved" if subject_ref else "resolved",
                    payload["predicate_iri"],
                    json.dumps(object_value),
                    object_entity_id,
                    valid_from,
                    valid_from,
                    valid_until,
                    valid_until,
                    payload.get("recorded_at", ""),
                    json.dumps(payload.get("source_ids") or []),
                    json.dumps(payload.get("evidence") or []),
                    payload.get("model_score", payload.get("confidence")),
                    payload.get("evidence_quality", "unknown"),
                ),
            )
            superseded = payload.get("supersedes")
            if superseded is not None:
                connection.execute(
                    """UPDATE read_assertions SET status = 'superseded',
                       superseded_by = %s WHERE assertion_id = %s""",
                    (assertion_id, superseded),
                )
        if subject_surface:
            self._resolve_pending_references(subject_surface)

    def supersede_assertion(self, assertion_id: str, reason: str = "") -> None:
        """Retract an assertion without deleting its history."""
        with self.database.connection() as connection:
            cursor = connection.execute(
                """UPDATE read_assertions SET status = 'retracted', superseded_reason = %s
                   WHERE assertion_id = %s""",
                (reason, assertion_id),
            )
        if cursor.rowcount == 0:
            raise ValueError(f"assertion {assertion_id!r} not in ledger; log is corrupt")

    @staticmethod
    def _assertion(row: tuple) -> dict:
        (
            assertion_id,
            subject_id,
            subject_ref,
            subject_status,
            predicate,
            object_value,
            valid_from_text,
            valid_until_text,
            recorded_at,
            source_ids,
            evidence,
            model_score,
            evidence_quality,
            status,
            superseded_by,
            superseded_reason,
        ) = row
        return {
            "assertion_id": assertion_id,
            "subject_id": subject_id,
            "subject_ref": subject_ref,
            "subject_status": subject_status,
            "predicate_iri": predicate,
            "object": object_value,
            "valid_from": valid_from_text,
            "valid_until": valid_until_text,
            "recorded_at": recorded_at,
            "source_ids": source_ids,
            "evidence": evidence,
            "model_score": model_score,
            "evidence_quality": evidence_quality,
            "status": status,
            "superseded_by": superseded_by,
            "superseded_reason": superseded_reason,
        }

    @staticmethod
    def _assertion_select() -> str:
        return """SELECT assertion_id, subject_id, subject_ref, subject_status,
                  predicate_iri, object, valid_from_text, valid_until_text, recorded_at,
                  source_ids, evidence, model_score, evidence_quality, status,
                  superseded_by, superseded_reason FROM read_assertions"""

    def get_assertion(self, assertion_id: str) -> dict | None:
        """Return one complete assertion ledger record."""
        with self.database.connection() as connection:
            row = connection.execute(
                f"{self._assertion_select()} WHERE assertion_id = %s", (assertion_id,)
            ).fetchone()
        return self._assertion(row) if row else None

    def active_assertions(self, subject_id: str, predicate_iri: str | None = None) -> list[dict]:
        """List current non-superseded assertions for a subject."""
        wanted = require_absolute_iri(predicate_iri, "predicate_iri") if predicate_iri else None
        sql = self._assertion_select() + " WHERE subject_id = %s AND status = 'asserted'"
        args: tuple = (subject_id,)
        if wanted:
            sql += " AND predicate_iri = %s"
            args += (wanted,)
        with self.database.connection() as connection:
            rows = connection.execute(sql, args).fetchall()
        return [self._assertion(row) for row in rows]

    def assertions_as_of(
        self, subject_id: str, instant: datetime, predicate_iri: str | None = None
    ) -> list[dict]:
        """Query assertions active at an aware instant."""
        if instant.tzinfo is None:
            raise ValueError("instant must be timezone-aware")
        canonical_subject = self._canonical_id(subject_id)
        wanted = require_absolute_iri(predicate_iri, "predicate_iri") if predicate_iri else None
        sql = (
            self._assertion_select()
            + """ WHERE subject_id = %s AND status = 'asserted'
            AND subject_status = 'resolved' AND valid_from <= %s
            AND (valid_until IS NULL OR %s < valid_until)"""
        )
        args: tuple = (canonical_subject, instant, instant)
        if wanted:
            sql += " AND predicate_iri = %s"
            args += (wanted,)
        with self.database.connection() as connection:
            rows = connection.execute(sql, args).fetchall()
        return [self._assertion(row) for row in rows]

    def get_entity(self, entity_id: str) -> EntityView | None:
        """Look up one live entity without loading neighboring records."""
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT entity_id, entity_type, name, first_seen, last_event_time
                   FROM read_entities WHERE entity_id = %s AND merged_into IS NULL""",
                (entity_id,),
            ).fetchone()
        return EntityView(*row) if row else None

    def merged_into(self, entity_id: str) -> str | None:
        """Return the direct survivor of a merged-away entity."""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT merged_into FROM read_entities WHERE entity_id = %s", (entity_id,)
            ).fetchone()
        return row[0] if row else None

    def find_by_name(self, name: str) -> list[EntityView]:
        """Find live entities by case-folded primary name."""
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT entity_id, entity_type, name, first_seen, last_event_time
                   FROM read_entities WHERE name_key = %s AND merged_into IS NULL
                   ORDER BY entity_id""",
                (name.casefold(),),
            ).fetchall()
        return [EntityView(*row) for row in rows]

    def location_resolution(self, entity_id: str, instant: datetime) -> LocationResolution:
        """Resolve location claims without hiding concurrent conflicts."""
        if instant.tzinfo is None:
            raise ValueError("location lookup instant must be timezone-aware")
        canonical_subject = self._canonical_id(entity_id)
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT assertion_id, object->>'value', source_ids, valid_from_text
                   FROM read_assertions
                   WHERE subject_id = %s AND predicate_iri = %s AND status = 'asserted'
                     AND subject_status = 'resolved' AND object->>'kind' = 'location'
                     AND valid_from <= %s AND (valid_until IS NULL OR %s < valid_until)
                   ORDER BY valid_from, assertion_id""",
                (canonical_subject, CORE_LOCATED_AT, instant, instant),
            ).fetchall()
        grouped: dict[str, list[tuple]] = {}
        for row in rows:
            grouped.setdefault(row[1], []).append(row)
        facts = tuple(
            LocationFact(
                location_uri=location,
                as_of=format_instant(max(parse_instant(row[3]) for row in values)),
                source_ids=tuple(sorted({source for row in values for source in row[2]})),
                assertion_ids=tuple(sorted(row[0] for row in values)),
            )
            for location, values in sorted(grouped.items())
        )
        if not facts:
            return LocationResolution("none")
        if len(facts) == 1:
            return LocationResolution("unique", fact=facts[0])
        return LocationResolution("conflict", conflicts=facts)

    def current_location(self, entity_id: str) -> LocationFact | None:
        """Return a unique location active now."""
        return self.location_resolution(entity_id, datetime.now(UTC)).fact

    def location_as_of(self, entity_id: str, instant: datetime) -> LocationFact | None:
        """Return a unique location active at the supplied instant."""
        return self.location_resolution(entity_id, instant).fact

    def location_conflicts(
        self, entity_id: str, instant: datetime | None = None
    ) -> tuple[LocationFact, ...]:
        """Return all distinct active claims when a location conflicts."""
        result = self.location_resolution(entity_id, instant or datetime.now(UTC))
        return result.conflicts if result.status == "conflict" else ()

    def entities_at(self, location_uri: str, instant: datetime | None = None) -> set[str]:
        """Find entities uniquely at a location at one valid-time instant."""
        at = instant or datetime.now(UTC)
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT subject_id FROM read_assertions
                   WHERE predicate_iri = %s AND status = 'asserted'
                     AND subject_status = 'resolved' AND object->>'kind' = 'location'
                     AND object->>'value' IS NOT NULL
                     AND valid_from <= %s AND (valid_until IS NULL OR %s < valid_until)
                   GROUP BY subject_id
                   HAVING count(DISTINCT object->>'value') = 1
                      AND min(object->>'value') = %s""",
                (CORE_LOCATED_AT, at, at, location_uri),
            ).fetchall()
        return {row[0] for row in rows}

    def add_track(self, payload: dict) -> None:
        """Upsert the track record and its observation/source links."""
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO read_tracks(
                       track_id, entity_id, subject_name, observation_ids, source_ids
                   )
                   VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
                   ON CONFLICT (track_id) DO UPDATE SET entity_id = EXCLUDED.entity_id,
                     subject_name = EXCLUDED.subject_name,
                     observation_ids = EXCLUDED.observation_ids,
                     source_ids = EXCLUDED.source_ids""",
                (
                    payload["track_id"],
                    payload["entity_id"],
                    payload.get("subject_name", ""),
                    json.dumps(payload.get("observation_ids") or []),
                    json.dumps(payload.get("source_ids") or []),
                ),
            )

    def get_track(self, track_id: str) -> dict | None:
        """Return one track record by its stable id."""
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT entity_id, subject_name, observation_ids, source_ids
                   FROM read_tracks WHERE track_id = %s""",
                (track_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "track_id": track_id,
            "entity_id": row[0],
            "subject_name": row[1],
            "observation_ids": row[2],
            "source_ids": row[3],
        }

    def tracks_of(self, entity_id: str) -> list[dict]:
        """List tracks assigned to an entity."""
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT track_id FROM read_tracks WHERE entity_id = %s ORDER BY track_id",
                (entity_id,),
            ).fetchall()
        tracks = [self.get_track(row[0]) for row in rows]
        return [track for track in tracks if track is not None]

    def stats(self, now: datetime | None = None) -> dict:
        """Summarize entity, assertion, location, and freshness metrics."""
        from foundry.events import utc_now_iso

        current = now or parse_instant(utc_now_iso())
        with self.database.connection() as connection:
            counts = connection.execute(
                """WITH current_unique AS (
                       SELECT subject_id, min(object->>'value') AS location
                       FROM read_assertions
                       WHERE predicate_iri = %s AND status = 'asserted'
                         AND subject_status = 'resolved' AND object->>'kind' = 'location'
                         AND valid_from <= %s AND (valid_until IS NULL OR %s < valid_until)
                       GROUP BY subject_id
                       HAVING count(DISTINCT object->>'value') = 1
                   )
                   SELECT
                       (SELECT count(*) FROM read_entities WHERE merged_into IS NULL),
                       (SELECT count(*) FROM current_unique),
                       (SELECT count(DISTINCT location) FROM current_unique),
                       (SELECT count(*) FROM read_assertions),
                       (SELECT count(*) FROM read_tracks),
                       (SELECT count(*) FROM read_assertions
                         WHERE subject_ref IS NOT NULL AND subject_id = ''),
                       (SELECT last_event_time FROM projection_state WHERE singleton = TRUE)""",
                (CORE_LOCATED_AT, current, current),
            ).fetchone()
        entities, with_location, locations, assertions, tracks, pending, last_event_time = counts
        lag = (current - last_event_time).total_seconds() if last_event_time else None
        return {
            "entities": entities,
            "with_location": with_location,
            "locations": locations,
            "assertions": assertions,
            "tracks": tracks,
            "pending_observations": pending,
            "pending_assertions": pending,
            "last_event_time": (
                last_event_time.strftime("%Y-%m-%dT%H:%M:%SZ") if last_event_time else None
            ),
            "lag_seconds": lag,
        }
