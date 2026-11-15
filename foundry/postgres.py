"""PostgreSQL adapters for the operational identity registry.

The event log remains authoritative. These tables are disposable projections
that can be rebuilt by ``tools/project_events.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import TYPE_CHECKING

from foundry.identity import (
    IdentityEvidence,
    IdentityRecord,
    IdentityStore,
    SourceRecordIdentity,
    normalize_name,
)

if TYPE_CHECKING:
    from psycopg import Connection


MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "postgres"


class PostgresDatabase:
    """Small psycopg connection manager with nested transaction support."""

    def __init__(self, dsn: str) -> None:
        """Bind the adapter to a libpq connection string."""
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN must be non-empty")
        self.dsn = dsn
        self._current: ContextVar[Connection | None] = ContextVar(
            f"foundry_pg_connection_{id(self)}", default=None
        )

    def _connect(self) -> Connection:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL storage requires psycopg; install project requirements"
            ) from exc
        return psycopg.connect(self.dsn)

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        """Use the active transaction connection or open a short-lived one."""
        active = self._current.get()
        if active is not None:
            yield active
            return
        with self._connect() as connection:
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Open one transaction, reusing it for nested adapter operations."""
        active = self._current.get()
        if active is not None:
            yield active
            return
        with self._connect() as connection:
            token: Token[Connection | None] = self._current.set(connection)
            try:
                yield connection
            finally:
                self._current.reset(token)

    def migrate(self) -> None:
        """Apply each ordered SQL migration once and record its completion."""
        migrations = sorted(MIGRATIONS.glob("*.sql"))
        if not migrations:
            return
        with self.transaction() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS foundry_schema_migrations (
                       version text PRIMARY KEY,
                       applied_at timestamptz NOT NULL DEFAULT now()
                   )"""
            )
        for migration in migrations:
            with self.transaction() as connection:
                connection.execute("SELECT pg_advisory_xact_lock(628041902118)")
                exists = connection.execute(
                    "SELECT 1 FROM foundry_schema_migrations WHERE version = %s",
                    (migration.name,),
                ).fetchone()
                if exists is not None:
                    continue
                connection.execute(migration.read_text(encoding="utf-8"), prepare=False)
                connection.execute(
                    "INSERT INTO foundry_schema_migrations(version) VALUES (%s)",
                    (migration.name,),
                )

    def reset_projection(self) -> None:
        """Clear rebuildable identity and read projections, preserving the log."""
        with self.transaction() as connection:
            connection.execute(
                """TRUNCATE TABLE projection_events,
                   read_tracks, read_assertions, read_entity_names, read_entities,
                   identity_cannot_links, identity_source_records, identity_merges,
                   identity_external_ids, identity_alias_tokens, identity_aliases,
                   identity_entities"""
            )
            connection.execute(
                """UPDATE projection_state SET checkpoint_sequence = NULL,
                   applied_count = 0, last_event_time = NULL WHERE singleton = TRUE"""
            )

    def verify_projection(self, expected_sequence: int) -> dict[str, object]:
        """Check the durable checkpoint and event ledger against an event log."""
        with self.connection() as connection:
            row = connection.execute(
                """SELECT checkpoint_sequence, applied_count,
                          (SELECT count(*) FROM projection_events),
                          (SELECT max(sequence) FROM projection_events),
                          (SELECT count(*) FROM projection_events WHERE applied)
                   FROM projection_state WHERE singleton = TRUE"""
            ).fetchone()
        checkpoint, applied_count, recorded, maximum, actual_applied = row or (None, 0, 0, None, 0)
        expected_checkpoint = expected_sequence or None
        return {
            "expected_sequence": expected_sequence,
            "checkpoint_sequence": checkpoint,
            "applied_count": applied_count,
            "recorded_event_count": recorded,
            "maximum_recorded_sequence": maximum,
            "consistent": (
                (checkpoint or 0) == expected_sequence
                and maximum == expected_checkpoint
                and recorded == expected_sequence
                and applied_count == actual_applied
            ),
        }


class PostgresIdentityStore(IdentityStore):
    """PostgreSQL-backed identity indexes with no process-local state cache."""

    def __init__(self, database: PostgresDatabase) -> None:
        """Use the shared database connection manager for identity state."""
        self.database = database

    def transaction(self):
        """Share the database transaction with identity and read projections."""
        return self.database.transaction()

    def upsert(self, entity_id: str, entity_type: str) -> None:
        """Create an identity row or reject an existing type mismatch."""
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO identity_entities(entity_id, entity_type)
                   VALUES (%s, %s) ON CONFLICT (entity_id) DO NOTHING""",
                (entity_id, entity_type),
            )
            row = connection.execute(
                "SELECT entity_type FROM identity_entities WHERE entity_id = %s",
                (entity_id,),
            ).fetchone()
            if row is None or row[0] != entity_type:
                current_type = row[0] if row else None
                raise ValueError(
                    f"type conflict for {entity_id}: {current_type!r} vs {entity_type!r}"
                )

    def get(self, entity_id: str) -> IdentityRecord | None:
        """Fetch one entity and its attached identifiers."""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT entity_type FROM identity_entities WHERE entity_id = %s",
                (entity_id,),
            ).fetchone()
            if row is None:
                return None
            aliases = connection.execute(
                "SELECT surface FROM identity_aliases WHERE entity_id = %s ORDER BY surface",
                (entity_id,),
            ).fetchall()
            external = connection.execute(
                """SELECT namespace, external_id FROM identity_external_ids
                   WHERE entity_id = %s ORDER BY namespace, external_id""",
                (entity_id,),
            ).fetchall()
        external_ids: dict[str, set[str]] = {}
        for namespace, external_id in external:
            external_ids.setdefault(namespace, set()).add(external_id)
        return IdentityRecord(
            entity_type=row[0],
            aliases={surface for (surface,) in aliases},
            external_ids=external_ids,
        )

    def entity_ids(self) -> Iterable[str]:
        """List canonical and merged entity ids."""
        with self.database.connection() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT entity_id FROM identity_entities ORDER BY entity_id"
                ).fetchall()
            ]

    def live_entity_count(self) -> int:
        """Count live identities in SQL without materializing their ids."""
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT count(*) FROM identity_entities e
                   WHERE NOT EXISTS (
                       SELECT 1 FROM identity_merges m WHERE m.duplicate_id = e.entity_id
                   )"""
            ).fetchone()
        return row[0]

    def bind_alias(self, entity_id: str, alias: str) -> None:
        """Add an alias and maintain its exact and token indexes."""
        normalized = normalize_name(alias)
        if not normalized:
            return
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO identity_aliases(entity_id, normalized_alias, surface)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (entity_id, normalized, alias),
            )
            connection.cursor().executemany(
                """INSERT INTO identity_alias_tokens(token, normalized_alias)
                   VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                [(token, normalized) for token in normalized.split()],
            )

    def unbind_alias(self, alias: str, entity_id: str) -> None:
        """Remove one alias binding and prune orphaned token rows."""
        normalized = normalize_name(alias)
        with self.database.connection() as connection:
            connection.execute(
                """DELETE FROM identity_aliases
                   WHERE entity_id = %s AND normalized_alias = %s AND surface = %s""",
                (entity_id, normalized, alias),
            )
            remaining = connection.execute(
                "SELECT 1 FROM identity_aliases WHERE normalized_alias = %s LIMIT 1",
                (normalized,),
            ).fetchone()
            if remaining is None:
                connection.execute(
                    "DELETE FROM identity_alias_tokens WHERE normalized_alias = %s", (normalized,)
                )

    def alias_owners(self, alias_norm: str) -> set[str]:
        """Return every canonical id bound to a normalized alias."""
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT entity_id FROM identity_aliases WHERE normalized_alias = %s",
                (alias_norm,),
            ).fetchall()
        return {row[0] for row in rows}

    def bind_external(self, entity_id: str, source: str, external_id: str) -> None:
        """Add a namespace-scoped external identifier binding."""
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO identity_external_ids(entity_id, namespace, external_id)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (entity_id, source, external_id),
            )

    def unbind_external(self, source: str, external_id: str, entity_id: str) -> None:
        """Remove one namespace-scoped external identifier binding."""
        with self.database.connection() as connection:
            connection.execute(
                """DELETE FROM identity_external_ids
                   WHERE entity_id = %s AND namespace = %s AND external_id = %s""",
                (entity_id, source, external_id),
            )

    def external_owners(self, source: str, external_id: str) -> set[str]:
        """Return all entities claiming a possibly reused external id."""
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT entity_id FROM identity_external_ids
                   WHERE namespace = %s AND external_id = %s""",
                (source, external_id),
            ).fetchall()
        return {row[0] for row in rows}

    def detach_bindings(
        self, entity_id: str
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        """Remove all aliases and external ids, returning the moved values."""
        record = self.get(entity_id)
        if record is None:
            raise ValueError(f"unknown canonical id {entity_id}")
        aliases = tuple(sorted(record.aliases))
        external = tuple(
            sorted(
                (source, value)
                for source, values in record.external_ids.items()
                for value in values
            )
        )
        with self.database.transaction():
            for alias in aliases:
                self.unbind_alias(alias, entity_id)
            for source, value in external:
                self.unbind_external(source, value, entity_id)
        return aliases, external

    def candidate_aliases(self, tokens: set[str]) -> dict[str, int]:
        """Return normalized aliases with shared-token counts."""
        if not tokens:
            return {}
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT normalized_alias, count(DISTINCT token)::integer
                   FROM identity_alias_tokens WHERE token = ANY(%s)
                   GROUP BY normalized_alias""",
                (list(tokens),),
            ).fetchall()
        return dict(rows)

    def record_merge(self, duplicate_id: str, survivor_id: str) -> None:
        """Persist a duplicate-to-survivor redirect."""
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO identity_merges(duplicate_id, survivor_id)
                   VALUES (%s, %s) ON CONFLICT (duplicate_id)
                   DO UPDATE SET survivor_id = EXCLUDED.survivor_id""",
                (duplicate_id, survivor_id),
            )

    def clear_merge(self, duplicate_id: str) -> None:
        """Remove a redirect when a merge is split."""
        with self.database.connection() as connection:
            connection.execute(
                "DELETE FROM identity_merges WHERE duplicate_id = %s", (duplicate_id,)
            )

    def merge_redirects(self) -> Mapping[str, str]:
        """Return all persisted merge redirects."""
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT duplicate_id, survivor_id FROM identity_merges ORDER BY duplicate_id"
            ).fetchall()
        return dict(rows)

    def source_record_owner(self, source_record: SourceRecordIdentity) -> str | None:
        """Return the canonical owner of one source-local record key."""
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT entity_id FROM identity_source_records
                   WHERE source_id = %s AND record_id = %s""",
                (source_record.source_id, source_record.record_id),
            ).fetchone()
        return row[0] if row else None

    def bind_source_record(self, source_record: SourceRecordIdentity, entity_id: str) -> None:
        """Bind a source record once, rejecting any conflicting owner."""
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO identity_source_records(source_id, record_id, entity_id)
                   VALUES (%s, %s, %s) ON CONFLICT (source_id, record_id) DO NOTHING""",
                (source_record.source_id, source_record.record_id, entity_id),
            )
            row = connection.execute(
                """SELECT entity_id FROM identity_source_records
                   WHERE source_id = %s AND record_id = %s""",
                (source_record.source_id, source_record.record_id),
            ).fetchone()
            if row is None or row[0] != entity_id:
                owner = row[0] if row else "unknown"
                raise ValueError(
                    f"source record {source_record.source_id}::{source_record.record_id} "
                    f"is already assigned to {owner}"
                )

    def source_records(self) -> Mapping[SourceRecordIdentity, str]:
        """Return the source-record index for replay and audits."""
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT source_id, record_id, entity_id FROM identity_source_records"
            ).fetchall()
        return {SourceRecordIdentity(source, record): entity for source, record, entity in rows}

    def add_cannot_link(self, left_id: str, right_id: str, evidence: IdentityEvidence) -> None:
        """Persist one symmetric cannot-link pair and its evidence."""
        left, right = sorted((left_id, right_id))
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO identity_cannot_links(left_id, right_id, evidence)
                   VALUES (%s, %s, %s::jsonb) ON CONFLICT DO NOTHING""",
                (left, right, json.dumps(evidence.to_dict())),
            )

    def cannot_links(self) -> Mapping[tuple[str, str], IdentityEvidence]:
        """Return all cannot-link constraints with parsed evidence."""
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT left_id, right_id, evidence FROM identity_cannot_links"
            ).fetchall()
        return {
            (left, right): IdentityEvidence.from_dict(evidence) for left, right, evidence in rows
        }
