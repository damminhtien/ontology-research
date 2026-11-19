"""PostgreSQL-backed operational projection with indexed temporal queries."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from foundry.namespaces import CORE_LOCATED_AT, normalize_surface, require_absolute_iri
from foundry.postgres import PostgresDatabase
from foundry.query_service import QueryPage, QueryTimeoutError
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

    @contextmanager
    def query_timeout(self, timeout_ms: int) -> Iterator[object]:
        """Apply a transaction-local PostgreSQL statement timeout."""
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("PostgreSQL storage requires psycopg") from exc
        with self.database.transaction() as connection:
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)", (f"{timeout_ms}ms",)
            )
            try:
                yield connection
            except psycopg.errors.QueryCanceled as exc:
                raise QueryTimeoutError(f"PostgreSQL query exceeded {timeout_ms} ms") from exc

    def is_applied(self, event_id: str) -> bool:
        """Check the durable processed-event ledger."""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM projection_events WHERE event_id = %s", (event_id,)
            ).fetchone()
        return row is not None

    def applied_event_ids(self, event_ids: Iterable[str]) -> set[str]:
        """Return processed IDs with one bounded ledger lookup."""
        requested = tuple(event_ids)
        if not requested:
            return set()
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT event_id FROM projection_events WHERE event_id = ANY(%s)",
                (list(requested),),
            ).fetchall()
        return {row[0] for row in rows}

    def mark_processed_batch(
        self,
        entries: Iterable[tuple[str, int | None, bool]],
        *,
        last_event_time: datetime | None,
    ) -> None:
        """Write a group ledger and advance its checkpoint in one statement."""
        batch = tuple(entries)
        if not batch:
            return
        values = ", ".join("(%s::text, %s::bigint, %s::boolean)" for _entry in batch)
        parameters = tuple(value for entry in batch for value in entry)
        query = f"""WITH batch(event_id, sequence, applied) AS (VALUES {values}),
                     inserted AS (
                       INSERT INTO projection_events(event_id, sequence, applied)
                       SELECT event_id, sequence, applied FROM batch
                       ON CONFLICT (event_id) DO NOTHING
                       RETURNING sequence, applied
                     ),
                     summary AS (
                       SELECT max(sequence) AS max_sequence,
                              count(*) FILTER (WHERE applied) AS applied_count
                       FROM inserted
                     )
                     UPDATE projection_state
                     SET checkpoint_sequence = CASE
                           WHEN summary.max_sequence IS NULL
                             THEN projection_state.checkpoint_sequence
                           ELSE GREATEST(
                             COALESCE(projection_state.checkpoint_sequence, 0),
                             summary.max_sequence
                           )
                         END,
                         applied_count = projection_state.applied_count + summary.applied_count,
                         last_event_time = CASE
                           WHEN %s::timestamptz IS NULL THEN projection_state.last_event_time
                           ELSE GREATEST(projection_state.last_event_time, %s::timestamptz)
                         END
                     FROM summary
                     WHERE singleton = TRUE"""
        with self.database.connection() as connection:
            connection.execute(query, (*parameters, last_event_time, last_event_time))

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

    def upsert_entities_batch(self, entities: Iterable[dict]) -> None:
        """Upsert a bounded group of entities and names with set-based writes."""
        merged: dict[str, dict[str, object]] = {}
        name_rows: set[tuple[str, str, str]] = set()
        for entity in entities:
            entity_id = str(entity["entity_id"])
            event_time = entity["event_time"]
            record = merged.get(entity_id)
            if record is None:
                record = {
                    "entity_type": entity["entity_type"],
                    "name": entity["name"],
                    "first_seen": event_time,
                    "last_event_time": event_time,
                }
                merged[entity_id] = record
            elif event_time > record["last_event_time"]:
                record["last_event_time"] = event_time

            for surface in (entity["name"], *(entity.get("aliases") or ())):
                normalized = normalize_surface(surface)
                if normalized:
                    name_rows.add((entity_id, normalized, surface))

        if not merged:
            return
        entity_rows = [
            (
                entity_id,
                record["entity_type"],
                record["name"],
                str(record["name"]).casefold(),
                record["first_seen"].isoformat(),
                record["last_event_time"].isoformat(),
            )
            for entity_id, record in merged.items()
        ]
        entity_ids, entity_types, names, name_keys, first_seen, last_event = zip(
            *entity_rows, strict=True
        )
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO read_entities(
                       entity_id, entity_type, name, name_key, first_seen, last_event_time
                   )
                   SELECT entity_id, entity_type, name, name_key, first_seen, last_event_time
                   FROM unnest(
                     %s::text[], %s::text[], %s::text[], %s::text[],
                     %s::text[], %s::text[]
                   ) AS input(entity_id, entity_type, name, name_key, first_seen, last_event_time)
                   ON CONFLICT (entity_id) DO UPDATE SET
                     last_event_time = CASE
                       WHEN read_entities.last_event_time::timestamptz
                         < EXCLUDED.last_event_time::timestamptz
                       THEN EXCLUDED.last_event_time ELSE read_entities.last_event_time END""",
                (
                    list(entity_ids),
                    list(entity_types),
                    list(names),
                    list(name_keys),
                    list(first_seen),
                    list(last_event),
                ),
            )
            if name_rows:
                owner_ids, normalized_names, surfaces = zip(*sorted(name_rows), strict=True)
                connection.execute(
                    """INSERT INTO read_entity_names(entity_id, normalized_name, surface)
                       SELECT entity_id, normalized_name, surface
                       FROM unnest(%s::text[], %s::text[], %s::text[])
                         AS input(entity_id, normalized_name, surface)
                       ON CONFLICT DO NOTHING""",
                    (list(owner_ids), list(normalized_names), list(surfaces)),
                )
        self._resolve_pending_references_many(normalized for _, normalized, _ in name_rows)

    def _resolve_pending_references_many(self, surfaces: Iterable[str]) -> None:
        """Find references affected by a name group, then resolve only those keys."""
        normalized_surfaces = tuple(sorted(set(surfaces)))
        if not normalized_surfaces:
            return
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT DISTINCT subject_ref_surface_norm FROM read_assertions
                   WHERE subject_ref_surface_norm = ANY(%s)""",
                (list(normalized_surfaces),),
            ).fetchall()
        for (surface,) in rows:
            self._resolve_pending_references(surface)

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
            if not rows:
                return
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
        self.add_assertions_batch((payload,))

    def add_assertions_batch(self, payloads: Iterable[dict]) -> None:
        """Upsert a bounded assertion group with one set-based SQL statement."""
        assertions = tuple(payloads)
        if not assertions:
            return
        assertion_ids = [payload["assertion_id"] for payload in assertions]
        if len(set(assertion_ids)) != len(assertion_ids):
            for payload in assertions:
                self.add_assertion(payload)
            return

        staged_rows: list[tuple[object, ...]] = []
        surfaces: list[str] = []
        supersessions: list[tuple[str, str]] = []
        for payload in assertions:
            subject_ref = payload.get("subject_ref")
            subject_surface = (
                normalize_surface(subject_ref["surface_form"]) if subject_ref else None
            )
            if subject_surface:
                surfaces.append(subject_surface)
            superseded = payload.get("supersedes")
            if superseded is not None:
                supersessions.append((payload["assertion_id"], superseded))
            object_value = dict(payload["object"])
            object_entity_id = (
                str(object_value.get("value"))
                if object_value.get("kind") == "entity" and object_value.get("value") is not None
                else None
            )
            original_subject = payload.get("subject_id") or ""
            valid_from = payload["valid_from"]
            valid_until = payload.get("valid_until", payload.get("valid_to"))
            staged_rows.append(
                (
                    payload["assertion_id"],
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
                )
            )

        with self.database.connection() as connection:
            connection.execute(
                """CREATE TEMP TABLE IF NOT EXISTS assertion_projection_stage (
                       assertion_id text NOT NULL,
                       original_subject_id text NOT NULL,
                       subject_ref jsonb,
                       subject_ref_surface_norm text,
                       subject_status text NOT NULL,
                       predicate_iri text NOT NULL,
                       object jsonb NOT NULL,
                       object_entity_id text,
                       valid_from timestamptz NOT NULL,
                       valid_from_text text NOT NULL,
                       valid_until timestamptz,
                       valid_until_text text,
                       recorded_at text NOT NULL,
                       source_ids jsonb NOT NULL,
                       evidence jsonb NOT NULL,
                       model_score double precision,
                       evidence_quality text NOT NULL
                   ) ON COMMIT DELETE ROWS"""
            )
            connection.execute("TRUNCATE pg_temp.assertion_projection_stage")
            with connection.cursor().copy(
                """COPY pg_temp.assertion_projection_stage (
                       assertion_id, original_subject_id, subject_ref,
                       subject_ref_surface_norm, subject_status, predicate_iri, object,
                       object_entity_id, valid_from, valid_from_text, valid_until,
                       valid_until_text, recorded_at, source_ids, evidence,
                       model_score, evidence_quality
                   ) FROM STDIN"""
            ) as copy:
                for row in staged_rows:
                    copy.write_row(row)
            connection.execute(
                """WITH RECURSIVE lineage(start_id, entity_id, merged_into, depth) AS (
                       SELECT subjects.original_subject_id, entity.entity_id,
                              entity.merged_into, 0
                       FROM (
                         SELECT DISTINCT original_subject_id
                         FROM pg_temp.assertion_projection_stage
                         WHERE original_subject_id <> ''
                       ) AS subjects
                       JOIN read_entities AS entity
                         ON entity.entity_id = subjects.original_subject_id
                       UNION ALL
                       SELECT lineage.start_id, entity.entity_id, entity.merged_into,
                              lineage.depth + 1
                       FROM read_entities AS entity
                       JOIN lineage ON entity.entity_id = lineage.merged_into
                       WHERE lineage.depth < 100
                     ), canonical_subjects AS (
                       SELECT DISTINCT ON (start_id) start_id, entity_id
                       FROM lineage
                       WHERE merged_into IS NULL
                       ORDER BY start_id, depth DESC
                     )
                     INSERT INTO read_assertions(
                       assertion_id, subject_id, original_subject_id, subject_ref,
                       subject_ref_surface_norm, subject_status, predicate_iri, object,
                       object_entity_id, valid_from, valid_from_text, valid_until,
                       valid_until_text, recorded_at, source_ids, evidence, model_score,
                       evidence_quality, status, superseded_by, superseded_reason
                     )
                     SELECT input.assertion_id,
                            COALESCE(canonical_subjects.entity_id, input.original_subject_id),
                            input.original_subject_id, input.subject_ref,
                            input.subject_ref_surface_norm, input.subject_status,
                            input.predicate_iri, input.object, input.object_entity_id,
                            input.valid_from, input.valid_from_text, input.valid_until,
                            input.valid_until_text, input.recorded_at, input.source_ids,
                            input.evidence, input.model_score, input.evidence_quality,
                            'asserted', NULL, NULL
                     FROM pg_temp.assertion_projection_stage AS input
                     LEFT JOIN canonical_subjects
                       ON canonical_subjects.start_id = input.original_subject_id
                     ON CONFLICT (assertion_id) DO UPDATE SET
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
                       status = 'asserted', superseded_by = NULL, superseded_reason = NULL"""
            )
            if supersessions:
                connection.cursor().executemany(
                    """UPDATE read_assertions SET status = 'superseded',
                       superseded_by = %s WHERE assertion_id = %s""",
                    supersessions,
                )
        for surface in dict.fromkeys(surfaces):
            self._resolve_pending_references(surface)

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

    def assertions_as_of_page(
        self,
        subject_id: str,
        instant: datetime,
        *,
        predicate_iri: str | None = None,
        object_kind: str | None = None,
        limit: int,
        offset: int = 0,
    ) -> QueryPage[dict]:
        """Page active assertions using subject, predicate and time indexes."""
        if instant.tzinfo is None:
            raise ValueError("instant must be timezone-aware")
        canonical_subject = self._canonical_id(subject_id)
        wanted = require_absolute_iri(predicate_iri, "predicate_iri") if predicate_iri else None
        where = (
            "subject_id = %s AND status = 'asserted' AND subject_status = 'resolved' "
            "AND valid_from <= %s AND (valid_until IS NULL OR %s < valid_until)"
        )
        args: tuple = (canonical_subject, instant, instant)
        if wanted:
            where += " AND predicate_iri = %s"
            args += (wanted,)
        if object_kind:
            where += " AND object->>'kind' = %s"
            args += (object_kind,)
        with self.database.connection() as connection:
            count_row = connection.execute(
                f"SELECT count(*) FROM read_assertions WHERE {where}", args
            ).fetchone()
            rows = connection.execute(
                self._assertion_select()
                + f" WHERE {where} ORDER BY valid_from, assertion_id LIMIT %s OFFSET %s",
                (*args, limit, offset),
            ).fetchall()
        total = count_row[0] if count_row else 0
        items = tuple(self._assertion(row) for row in rows)
        return QueryPage(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        )

    def assertion_history_page(
        self,
        subject_id: str,
        *,
        include_superseded: bool = True,
        limit: int,
        offset: int,
        sort_by: str = "recorded_at",
        descending: bool = True,
    ) -> QueryPage[dict]:
        """Page assertion history with a subject index and deterministic ordering."""
        canonical_subject = self._canonical_id(subject_id)
        where = "subject_id = %s"
        args: tuple = (canonical_subject,)
        if not include_superseded:
            where += " AND status = 'asserted'"
        order_column = {"recorded_at": "recorded_at", "valid_from": "valid_from"}[sort_by]
        direction = "DESC" if descending else "ASC"
        with self.database.connection() as connection:
            count_row = connection.execute(
                f"SELECT count(*) FROM read_assertions WHERE {where}", args
            ).fetchone()
            rows = connection.execute(
                self._assertion_select()
                + f" WHERE {where} ORDER BY {order_column} {direction}, assertion_id {direction} "
                "LIMIT %s OFFSET %s",
                (*args, limit, offset),
            ).fetchall()
        total = count_row[0] if count_row else 0
        items = tuple(self._assertion(row) for row in rows)
        return QueryPage(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        )

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

    def find_entities_page(
        self,
        name: str,
        *,
        limit: int,
        offset: int,
        sort_by: str,
        descending: bool,
    ) -> QueryPage[EntityView]:
        """Page exact normalized name and alias matches through the name index."""
        normalized = normalize_surface(name)
        sort_columns = {
            "entity_id": "e.entity_id",
            "name": "e.name",
            "entity_type": "e.entity_type",
            "first_seen": "e.first_seen",
            "last_event_time": "e.last_event_time",
        }
        order = "DESC" if descending else "ASC"
        sort_column = sort_columns[sort_by]
        with self.database.connection() as connection:
            total_row = connection.execute(
                """SELECT count(DISTINCT e.entity_id)
                   FROM read_entity_names n
                   JOIN read_entities e ON e.entity_id = n.entity_id
                   WHERE n.normalized_name = %s AND e.merged_into IS NULL""",
                (normalized,),
            ).fetchone()
            rows = connection.execute(
                f"""SELECT DISTINCT e.entity_id, e.entity_type, e.name,
                          e.first_seen, e.last_event_time
                   FROM read_entity_names n
                   JOIN read_entities e ON e.entity_id = n.entity_id
                   WHERE n.normalized_name = %s AND e.merged_into IS NULL
                   ORDER BY {sort_column} {order}, e.entity_id ASC
                   LIMIT %s OFFSET %s""",
                (normalized, limit, offset),
            ).fetchall()
        total = total_row[0] if total_row else 0
        items = tuple(EntityView(*row) for row in rows)
        return QueryPage(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        )

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

    def entities_at_page(
        self, location_uri: str, instant: datetime, *, limit: int, offset: int
    ) -> QueryPage[str]:
        """Page entities uniquely at a location, ordered by canonical id."""
        if instant.tzinfo is None:
            raise ValueError("instant must be timezone-aware")
        match_cte = """WITH matches AS (
               SELECT subject_id
               FROM read_assertions
               WHERE predicate_iri = %s AND status = 'asserted'
                 AND subject_status = 'resolved' AND object->>'kind' = 'location'
                 AND object->>'value' IS NOT NULL
                 AND valid_from <= %s AND (valid_until IS NULL OR %s < valid_until)
               GROUP BY subject_id
               HAVING count(DISTINCT object->>'value') = 1
                  AND min(object->>'value') = %s
           )"""
        with self.database.connection() as connection:
            count_row = connection.execute(
                match_cte + " SELECT count(*) FROM matches",
                (CORE_LOCATED_AT, instant, instant, location_uri),
            ).fetchone()
            rows = connection.execute(
                match_cte
                + " SELECT subject_id FROM matches ORDER BY subject_id LIMIT %s OFFSET %s",
                (CORE_LOCATED_AT, instant, instant, location_uri, limit, offset),
            ).fetchall()
        total = count_row[0] if count_row else 0
        items = tuple(row[0] for row in rows)
        return QueryPage(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        )

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

    def tracks_page(self, entity_id: str, *, limit: int, offset: int) -> QueryPage[dict]:
        """Page tracks using the entity index and stable track id ordering."""
        with self.database.connection() as connection:
            total_row = connection.execute(
                "SELECT count(*) FROM read_tracks WHERE entity_id = %s", (entity_id,)
            ).fetchone()
            rows = connection.execute(
                "SELECT track_id, entity_id, subject_name, observation_ids, source_ids "
                "FROM read_tracks WHERE entity_id = %s ORDER BY track_id LIMIT %s OFFSET %s",
                (entity_id, limit, offset),
            ).fetchall()
        total = total_row[0] if total_row else 0
        items = tuple(
            {
                "track_id": row[0],
                "entity_id": row[1],
                "subject_name": row[2],
                "observation_ids": row[3],
                "source_ids": row[4],
            }
            for row in rows
        )
        return QueryPage(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=offset + len(items) < total,
        )

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
