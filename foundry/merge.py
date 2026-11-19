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

import json
from dataclasses import dataclass
from typing import Any

from foundry.events import (
    EVENT_TYPE_ENTITY_CANNOT_LINK,
    EVENT_TYPE_ENTITY_CREATED,
    EVENT_TYPE_ENTITY_MERGED,
    EVENT_TYPE_ENTITY_SPLIT,
    EVENT_TYPE_EXTERNAL_ID_BOUND,
    EVENT_TYPE_SENSOR_REGISTERED,
    EVENT_TYPE_SOURCE_RECORD_MATCHED,
    EventLog,
    SemanticEvent,
    make_event,
)
from foundry.identity import IdentityEvidence, IdentityService, IdentityStore, SourceRecordIdentity
from foundry.provenance import content_sha256


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
    same_entity_evidence: IdentityEvidence,
    reason: str = "",
    actor: str = "",
    apply_projection: bool = True,
) -> MergeResult:
    """Merge two canonical entities and append the ``EntityMerged`` event.

    The event is committed before the identity projection is changed. A retry
    with the same evidence and command returns the original committed event.

    Raises:
        ValueError: On self-merge, unknown ids, an already-merged duplicate,
            or an entity-type conflict.
    """
    payload: dict[str, Any] = {
        "survivor_id": survivor_id,
        "duplicate_id": duplicate_id,
        "reason": reason,
        "actor": actor,
        "same_entity_evidence": same_entity_evidence.to_dict(),
    }
    idempotency_key = (
        f"entity-merge:{survivor_id}:{duplicate_id}:{same_entity_evidence.evidence_id}"
    )
    fingerprint = content_sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    prior = log.find_idempotency(idempotency_key, request_fingerprint=fingerprint)
    if prior is not None:
        event = prior.events[0]
        if apply_projection and not identity.merged_into(duplicate_id):
            identity.merge_entities(
                survivor_id, duplicate_id, same_entity_evidence=same_entity_evidence
            )
        moved_aliases = tuple(event.payload.get("moved_aliases") or ())
        moved_external_ids = tuple(
            (binding["source"], binding["external_id"])
            for binding in event.payload.get("moved_external_ids") or ()
        )
        return MergeResult(survivor_id, duplicate_id, moved_aliases, moved_external_ids, event)

    outcome = identity.preview_merge(
        survivor_id,
        duplicate_id,
        same_entity_evidence=same_entity_evidence,
    )
    payload.update(
        moved_aliases=list(outcome.moved_aliases),
        moved_external_ids=[
            {"source": source, "external_id": ext} for source, ext in outcome.moved_external_ids
        ],
    )
    event = make_event(EVENT_TYPE_ENTITY_MERGED, payload)
    committed = log.commit(
        [event], idempotency_key=idempotency_key, request_fingerprint=fingerprint
    ).events[0]
    applied = (
        identity.merge_entities(
            survivor_id,
            duplicate_id,
            same_entity_evidence=same_entity_evidence,
        )
        if apply_projection
        else outcome
    )
    return MergeResult(
        survivor_id=survivor_id,
        duplicate_id=duplicate_id,
        moved_aliases=applied.moved_aliases,
        moved_external_ids=applied.moved_external_ids,
        event=committed,
    )


def assert_cannot_link(
    *,
    identity: IdentityService,
    log: EventLog,
    left_id: str,
    right_id: str,
    evidence: IdentityEvidence,
) -> SemanticEvent:
    """Durably record a cannot-link before applying it to the identity store."""
    identity.validate_cannot_link(left_id, right_id)
    left, right = sorted((left_id, right_id))
    payload = {"left_id": left, "right_id": right, "evidence": evidence.to_dict()}
    key = f"entity-cannot-link:{left}:{right}:{evidence.evidence_id}"
    fingerprint = content_sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    prior = log.find_idempotency(key, request_fingerprint=fingerprint)
    if prior is not None:
        identity.add_cannot_link(left, right, evidence)
        return prior.events[0]
    event = make_event(EVENT_TYPE_ENTITY_CANNOT_LINK, payload)
    committed = log.commit([event], idempotency_key=key, request_fingerprint=fingerprint).events[0]
    identity.add_cannot_link(left, right, evidence)
    return committed


def apply_identity_event(identity: IdentityService, event: SemanticEvent) -> bool:
    """Apply one identity event atomically; return False for unrelated events."""
    payload = event.payload
    with identity.store.transaction():
        if event.event_type == EVENT_TYPE_ENTITY_CREATED:
            identity.register(
                entity_id=payload["entity_id"],
                entity_type=payload["entity_type"],
                aliases=[payload["name"], *payload.get("name_aliases", [])],
            )
            for binding in payload.get("external_ids") or []:
                identity.add_external_id(
                    payload["entity_id"], binding["source"], binding["external_id"]
                )
            if payload.get("source_id") and payload.get("source_record_id"):
                identity.store.bind_source_record(
                    SourceRecordIdentity(payload["source_id"], payload["source_record_id"]),
                    payload["entity_id"],
                )
        elif event.event_type == EVENT_TYPE_SOURCE_RECORD_MATCHED:
            identity.replay_source_record_match(
                source_record=SourceRecordIdentity(payload["source_id"], payload["record_id"]),
                entity_id=payload["entity_id"],
                aliases=[payload.get("name", ""), *(payload.get("name_aliases") or [])],
                external_ids=(
                    (binding["source"], binding["external_id"])
                    for binding in payload.get("external_ids") or []
                ),
            )
        elif event.event_type == EVENT_TYPE_ENTITY_CANNOT_LINK:
            identity.add_cannot_link(
                payload["left_id"],
                payload["right_id"],
                IdentityEvidence.from_dict(payload["evidence"]),
            )
        elif event.event_type == EVENT_TYPE_ENTITY_MERGED:
            evidence = payload.get("same_entity_evidence")
            if evidence is None:
                evidence = {
                    "evidence_id": f"legacy-merge:{event.event_id}",
                    "source": "legacy-event-log",
                    "basis": "historical-merge-event",
                    "recorded_by": payload.get("actor") or "legacy-replay",
                    "description": payload.get("reason", ""),
                }
            identity.merge_entities(
                payload["survivor_id"],
                payload["duplicate_id"],
                same_entity_evidence=IdentityEvidence.from_dict(evidence),
            )
        elif event.event_type == EVENT_TYPE_EXTERNAL_ID_BOUND:
            identity.add_external_id(
                payload["entity_id"], payload["source"], payload["external_id"]
            )
        elif event.event_type == EVENT_TYPE_SENSOR_REGISTERED:
            identity.register(
                entity_id=payload["sensor_entity_id"],
                entity_type="Artifact",
                aliases=[payload["name"]],
                external_ids={"sensor-registry": payload["sensor_id"]},
            )
            identity.register(
                entity_id=payload["platform_entity_id"],
                entity_type="Platform",
                aliases=[payload["platform_name"]],
            )
        elif event.event_type == EVENT_TYPE_ENTITY_SPLIT:
            identity.split_entity(
                payload["survivor_id"],
                payload["duplicate_id"],
                tuple(payload.get("restored_aliases") or []),
                tuple(
                    (binding["source"], binding["external_id"])
                    for binding in payload.get("restored_external_ids") or []
                ),
            )
        else:
            return False
    return True


def apply_identity_events(identity: IdentityService, events: list[SemanticEvent]) -> None:
    """Replay a bounded event group, bulk-registering independent creations."""
    if not callable(getattr(identity.store, "register_entities_batch", None)):
        for event in events:
            apply_identity_event(identity, event)
        return

    neutral_events = {
        "AssertionMade",
        "AssertionSuperseded",
        "DocumentRegistered",
        "ObservationRecorded",
        "ResolutionReviewQueued",
        "TrackObserved",
    }
    pending_creations: list[SemanticEvent] = []

    def flush_creations() -> None:
        if not pending_creations:
            return
        registrations = []
        for event in pending_creations:
            payload = event.payload
            source_record = (
                (payload["source_id"], payload["source_record_id"])
                if payload.get("source_id") and payload.get("source_record_id")
                else None
            )
            registrations.append(
                {
                    "entity_id": payload["entity_id"],
                    "entity_type": payload["entity_type"],
                    "aliases": [payload["name"], *(payload.get("name_aliases") or [])],
                    "external_ids": [
                        (binding["source"], binding["external_id"])
                        for binding in payload.get("external_ids") or []
                    ],
                    "source_record": source_record,
                }
            )
        identity.register_batch(registrations)
        pending_creations.clear()

    for event in events:
        if event.event_type == EVENT_TYPE_ENTITY_CREATED:
            pending_creations.append(event)
        elif event.event_type in neutral_events:
            continue
        else:
            flush_creations()
            apply_identity_event(identity, event)
    flush_creations()


def rebuild_identity(log: EventLog, *, store: IdentityStore | None = None) -> IdentityService:
    """Rebuild identity indexes from an event log without materializing it.

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
    identity = IdentityService(store=store)
    for event in log.iter_all():
        apply_identity_event(identity, event)
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
    latest = None
    for event in log.iter_all():
        if event.event_type != EVENT_TYPE_ENTITY_MERGED:
            continue
        payload = event.payload
        if (
            payload.get("survivor_id") == survivor_id
            and payload.get("duplicate_id") == duplicate_id
        ):
            latest = event
    return latest


def split_entities(
    *,
    identity: IdentityService,
    log: EventLog,
    survivor_id: str,
    duplicate_id: str,
    reason: str = "",
    actor: str = "",
    apply_projection: bool = True,
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
    idempotency_key = f"entity-split:{merge_event.event_id}:{actor}:{reason}"
    command = {
        "survivor_id": survivor_id,
        "duplicate_id": duplicate_id,
        "reason": reason,
        "actor": actor,
    }
    fingerprint = content_sha256(
        json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    prior = log.find_idempotency(idempotency_key, request_fingerprint=fingerprint)
    if prior is not None:
        event = prior.events[0]
        if apply_projection and identity.merged_into(duplicate_id) == survivor_id:
            identity.split_entity(
                survivor_id,
                duplicate_id,
                tuple(moved.get("moved_aliases") or []),
                tuple(
                    (b["source"], b["external_id"]) for b in moved.get("moved_external_ids") or []
                ),
            )
        return SplitResult(
            survivor_id,
            duplicate_id,
            tuple(event.payload.get("restored_aliases") or ()),
            tuple(
                (binding["source"], binding["external_id"])
                for binding in event.payload.get("restored_external_ids") or ()
            ),
            tuple(event.payload.get("retained_aliases") or ()),
            tuple(
                (binding["source"], binding["external_id"])
                for binding in event.payload.get("retained_external_ids") or ()
            ),
            event,
        )

    restore_aliases = tuple(moved.get("moved_aliases") or [])
    restore_external = tuple(
        (b["source"], b["external_id"]) for b in moved.get("moved_external_ids") or []
    )
    outcome = identity.split_entity(
        survivor_id,
        duplicate_id,
        restore_aliases,
        restore_external,
        prepare_only=True,
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
            "actor": actor,
        },
    )
    committed = log.commit(
        [event],
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    ).events[0]
    applied = (
        identity.split_entity(survivor_id, duplicate_id, restore_aliases, restore_external)
        if apply_projection
        else outcome
    )
    return SplitResult(
        survivor_id=survivor_id,
        duplicate_id=duplicate_id,
        restored_aliases=applied.restored_aliases,
        restored_external_ids=applied.restored_external_ids,
        retained_aliases=applied.retained_aliases,
        retained_external_ids=applied.retained_external_ids,
        event=committed,
    )
