"""Entity merge tooling: under-merge repair as an append-only correction.

ADR-0006 trades false merges for under-merges: when a trusted external id
misses, a new canonical entity is created even though the real-world entity
may already exist under another id. When an under-merge is later confirmed
(two canonical ids denote the same thing), the correction is itself a fact on
the log: an ``EntityMerged`` event that re-points every alias and external-id
binding of the duplicate onto the survivor. The log is never rewritten
(ADR-0002) — the superseding event is what makes merges replayable, auditable
and idempotent under projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from foundry.events import (
    EVENT_TYPE_ENTITY_CREATED,
    EVENT_TYPE_ENTITY_MERGED,
    EventLog,
    SemanticEvent,
    make_event,
)
from foundry.identity import IdentityService


@dataclass(frozen=True)
class MergeResult:
    """Receipt for one confirmed merge."""

    survivor_id: str
    duplicate_id: str
    moved_aliases: tuple[str, ...]
    moved_external_ids: tuple[tuple[str, str], ...]
    event: SemanticEvent


def merge_entities(
    *,
    identity: IdentityService,
    log: EventLog,
    survivor_id: str,
    duplicate_id: str,
    reason: str = "",
) -> MergeResult:
    """Merge two canonical entities and append the ``EntityMerged`` event.

    The identity registry is mutated first and the event appended second,
    matching the ingestion pipeline's ordering (identity registration precedes
    the log append). ``ValueError`` from the registry propagates unchanged so
    callers can route rejections to review.

    Raises:
        ValueError: On self-merge, unknown ids, an already-merged duplicate,
            or an entity-type conflict.
    """
    outcome = identity.merge_entities(survivor_id, duplicate_id)
    payload: dict[str, Any] = {
        "survivor_id": survivor_id,
        "duplicate_id": duplicate_id,
        "moved_aliases": list(outcome.moved_aliases),
        "moved_external_ids": [
            {"source": source, "external_id": ext} for source, ext in outcome.moved_external_ids
        ],
        "reason": reason,
    }
    event = make_event(EVENT_TYPE_ENTITY_MERGED, payload)
    log.append(event)
    return MergeResult(
        survivor_id=survivor_id,
        duplicate_id=duplicate_id,
        moved_aliases=outcome.moved_aliases,
        moved_external_ids=outcome.moved_external_ids,
        event=event,
    )


def rebuild_identity(log: EventLog) -> IdentityService:
    """Rebuild the in-memory identity registry from an event log.

    Restores entity types, names and aliases from ``EntityCreated`` events and
    replays ``EntityMerged`` corrections so offline tools (merge CLI, review
    queue) validate against real canonical state. External-id bindings are not
    persisted on ``EntityCreated`` payloads yet, so they are absent after a
    rebuild; exact alias resolution and merge validation are intact.

    Raises:
        ValueError: If the log contains records that violate the registry
            contract (type conflicts across events, unknown merge ids).
    """
    identity = IdentityService()
    for event in log.read_all():
        payload = event.payload
        if event.event_type == EVENT_TYPE_ENTITY_CREATED:
            identity.register(
                entity_id=payload["entity_id"],
                entity_type=payload["entity_type"],
                aliases=[payload["name"], *payload.get("name_aliases", [])],
            )
        elif event.event_type == EVENT_TYPE_ENTITY_MERGED:
            identity.merge_entities(payload["survivor_id"], payload["duplicate_id"])
    return identity
