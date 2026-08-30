"""Projector: folds the append-only event log into the read model.

Write model (events) -> projector -> read model (fast operational queries).
The projector is deliberately deterministic:

- replay sorts by (occurred_at, event_id) so out-of-order appends project the
  same final state;
- every apply() is idempotent, so replaying the same log twice must produce
  a structurally identical read model - regression-tested.

Unknown event types are counted and skipped, not fatal: older projections stay
correct when new event types land ahead of projector upgrades.
"""

from __future__ import annotations

from dataclasses import dataclass

from foundry.events import EventLog, SemanticEvent
from foundry.readmodel import ReadModel, parse_instant


@dataclass(frozen=True)
class ProjectStats:
    """Counters from one replay."""

    applied: int
    skipped: int
    by_type: dict[str, int]


class Projector:
    """Applies domain events onto a ReadModel."""

    def __init__(self, model: ReadModel) -> None:
        """Bind the projector to a target read model."""
        self._model = model

    def apply(self, event: SemanticEvent) -> bool:
        """Apply one event; returns False when the type is not handled."""
        occurred = parse_instant(event.occurred_at)
        payload = event.payload

        if event.event_type == "EntityCreated":
            self._model.upsert_entity(
                entity_id=payload["entity_id"],
                entity_type=payload["entity_type"],
                name=payload["name"],
                event_time=occurred,
            )
        elif event.event_type == "LocationObserved":
            self._model.add_location_observation(
                entity_id=payload["entity_id"],
                location_uri=payload["location_uri"],
                valid_from=parse_instant(payload["valid_from"]),
                source_ids=tuple(payload.get("source_ids") or ()),
                event_id=event.event_id,
            )
        elif event.event_type == "EntityMerged":
            self._model.merge_entities(
                survivor_id=payload["survivor_id"],
                duplicate_id=payload["duplicate_id"],
                event_time=occurred,
            )
        else:
            return False

        self._model.touch(occurred)
        return True

    def replay(self, events: list[SemanticEvent]) -> ProjectStats:
        """Fold a full event list in deterministic (occurred_at, event_id) order."""
        applied = 0
        skipped = 0
        by_type: dict[str, int] = {}
        for event in sorted(events, key=lambda e: (e.occurred_at, e.event_id)):
            by_type[event.event_type] = by_type.get(event.event_type, 0) + 1
            if self.apply(event):
                applied += 1
            else:
                skipped += 1
        self._model.rebuild_location_index()
        return ProjectStats(applied=applied, skipped=skipped, by_type=by_type)


def replay_log(log: EventLog) -> tuple[ReadModel, ProjectStats]:
    """Build a fresh read model by replaying the entire event log."""
    model = ReadModel()
    stats = Projector(model).replay(log.read_all())
    return model, stats
