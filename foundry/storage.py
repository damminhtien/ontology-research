"""Factories for operational storage selected by deployment environment."""

from __future__ import annotations

import os

from foundry.events import EventLog
from foundry.identity import IdentityService
from foundry.merge import rebuild_identity
from foundry.postgres import PostgresDatabase, PostgresIdentityStore
from foundry.postgres_readmodel import PostgresReadModelStore


def identity_service_for_log(log: EventLog) -> tuple[IdentityService, bool]:
    """Return the configured identity store and whether callers should apply writes.

    Without PostgreSQL, rebuild the in-memory identity projection from the log
    and apply command writes after commit. With PostgreSQL, reuse the durable
    identity indexes and require the projector checkpoint to match the log
    before resolving identities. Correction tools leave projection updates to
    the worker; ingestion may apply committed identity rows locally so later
    records in the same batch can resolve against them. Worker replay is
    idempotent and remains the checkpointed recovery path.
    """
    dsn = os.environ.get("FOUNDRY_DATABASE_URL")
    if not dsn:
        return rebuild_identity(log), True
    database = PostgresDatabase(dsn)
    checkpoint = PostgresReadModelStore(database).checkpoint_sequence or 0
    last_sequence = log.last_sequence
    if checkpoint < last_sequence:
        raise RuntimeError(
            f"PostgreSQL projector is behind the event log ({checkpoint} < {last_sequence}); "
            "run tools/project_events.py before an identity command"
        )
    return IdentityService(PostgresIdentityStore(database)), False
