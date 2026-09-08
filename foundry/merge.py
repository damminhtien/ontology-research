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
    EVENT_TYPE_ENTITY_SPLIT,
    EVENT_TYPE_EXTERNAL_ID_BOUND,
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

    Restores entity types, names, aliases and external-id bindings from
    ``EntityCreated`` events, replays ``ExternalIdBound`` binding facts and
    ``EntityMerged`` corrections so offline tools (merge CLI, ingestion
    restarts, review queue) validate against real canonical state. Logs
    written before external ids were persisted carry an empty or absent
    ``external_ids`` payload; those bindings stay unrecovered unless a
    backfill recorded them as ``ExternalIdBound`` events.

    Raises:
        ValueError: If the log contains records that violate the registry
            contract (type conflicts across events, unknown merge or
            binding ids).
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
            for binding in payload.get("external_ids") or []:
                identity.add_external_id(
                    payload["entity_id"],
                    binding["source"],
                    binding["external_id"],
                )
        elif event.event_type == EVENT_TYPE_ENTITY_MERGED:
            identity.merge_entities(payload["survivor_id"], payload["duplicate_id"])
        elif event.event_type == EVENT_TYPE_EXTERNAL_ID_BOUND:
            identity.add_external_id(
                payload["entity_id"], payload["source"], payload["external_id"]
            )
        elif event.event_type == EVENT_TYPE_ENTITY_SPLIT:
            identity.split_entity(
                payload["survivor_id"],
                payload["duplicate_id"],
                tuple(payload.get("restored_aliases") or []),
                tuple(
                    (b["source"], b["external_id"])
                    for b in payload.get("restored_external_ids") or []
                ),
            )
            # bindings a third party claimed between merge and split stay put;
            # the split outcome is already recorded in the event payload
    return identity


@dataclass(frozen=True)
class SplitResult:
    """Receipt for one confirmed un-merge."""

    survivor_id: str
    duplicate_id: str
    restored_aliases: tuple[str, ...]
    restored_external_ids: tuple[tuple[str, str], ...]
    retained_aliases: tuple[str, ...]
    retained_external_ids: tuple[tuple[str, str], ...]
    event: SemanticEvent


def find_merge_event(log: EventLog, survivor_id: str, duplicate_id: str) -> SemanticEvent | None:
    """Latest ``EntityMerged`` event for this pair, if the log records one."""
    for event in reversed(log.read_all()):
        if event.event_type != EVENT_TYPE_ENTITY_MERGED:
            continue
        payload = event.payload
        if (
            payload.get("survivor_id") == survivor_id
            and payload.get("duplicate_id") == duplicate_id
        ):
            return event
    return None


def split_entities(
    *,
    identity: IdentityService,
    log: EventLog,
    survivor_id: str,
    duplicate_id: str,
    reason: str = "",
) -> SplitResult:
    """Undo a previously recorded merge and append the ``EntitySplit`` event.

    The restore set is read from the ``EntityMerged`` event in the log — a
    split without a recorded merge is rejected, so the correction always
    references an auditable cause.

    Raises:
        ValueError: If no merge event exists for the pair, or the registry
            rejects the split (unknown ids, not merged, type conflict).
    """
    merge_event = find_merge_event(log, survivor_id, duplicate_id)
    if merge_event is None:
        raise ValueError(
            f"no EntityMerged event recorded for {duplicate_id} -> {survivor_id}; nothing to split"
        )
    moved = merge_event.payload
    outcome = identity.split_entity(
        survivor_id,
        duplicate_id,
        tuple(moved.get("moved_aliases") or []),
        tuple((b["source"], b["external_id"]) for b in moved.get("moved_external_ids") or []),
    )
    event = make_event(
        EVENT_TYPE_ENTITY_SPLIT,
        {
            "survivor_id": survivor_id,
            "duplicate_id": duplicate_id,
            "restored_aliases": list(outcome.restored_aliases),
            "restored_external_ids": [
                {"source": source, "external_id": ext}
                for source, ext in outcome.restored_external_ids
            ],
            "retained_aliases": list(outcome.retained_aliases),
            "retained_external_ids": [
                {"source": source, "external_id": ext}
                for source, ext in outcome.retained_external_ids
            ],
            "reason": reason,
        },
    )
    log.append(event)
    return SplitResult(
        survivor_id=survivor_id,
        duplicate_id=duplicate_id,
        restored_aliases=outcome.restored_aliases,
        restored_external_ids=outcome.restored_external_ids,
        retained_aliases=outcome.retained_aliases,
        retained_external_ids=outcome.retained_external_ids,
        event=event,
    )
