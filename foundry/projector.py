"""Projector: folds the append-only event log into the read model.

Write model (events) -> projector -> read model (fast operational queries).
The projector is deliberately deterministic and safe to run incrementally:

- replay orders by the log ``sequence`` (the append order — ADR-0009), not by
  wall-clock timestamps whose one-second resolution loses causal order;
- per-event idempotency: re-applying an already-applied event is a no-op, so
  a replay that overlaps a previous one cannot double-count history;
- checkpointed: the model tracks the highest applied sequence, and
  :meth:`Projector.save_checkpoint` / :meth:`Projector.load_checkpoint` let a
  restart resume from there instead of replaying the whole log;
- every apply keeps the read model's indexes consistent on its own — there is
  no "only correct after a full replay" invariant.

Unknown event types are counted and skipped, not fatal: older projections stay
correct when new event types land ahead of projector upgrades.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from foundry.events import EventLog, SemanticEvent
from foundry.readmodel import ReadModel, parse_instant


@dataclass(frozen=True)
class ProjectStats:
    """Counters from one replay."""

    applied: int
    skipped: int
    by_type: dict[str, int]
    duplicates: int = 0


class Projector:
    """Applies domain events onto a ReadModel."""

    def __init__(self, model: ReadModel) -> None:
        """Bind the projector to a target read model."""
        self._model = model

    def apply(self, event: SemanticEvent) -> bool:
        """Apply one event; returns False when the type is not handled.

        Re-applying an event that was already folded into the model is a
        no-op (reported as handled, counted as a duplicate by :meth:`replay`).
        """
        if self._model.is_applied(event.event_id):
            return True
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
        elif event.event_type == "ExternalIdBound":
            pass  # identity-level fact; the read model holds no binding state
        else:
            return False

        self._model.touch(occurred)
        self._model.mark_applied(event.event_id, event.sequence)
        return True

    def replay(
        self, events: list[SemanticEvent], *, after_sequence: int | None = None
    ) -> ProjectStats:
        """Fold an event list in log order.

        Events are ordered by their ``sequence`` stamp (the append order);
        legacy unstamped records keep their input order and precede stamped
        ones, matching how logs are written (v1 era before v2). With
        ``after_sequence``, events at or below that checkpoint are skipped —
        the resume path for an already-populated read model.
        """
        ordered = sorted(
            enumerate(events),
            key=lambda pair: (
                -1 if pair[1].sequence is None else 0,
                pair[0] if pair[1].sequence is None else pair[1].sequence,
            ),
        )
        applied = 0
        skipped = 0
        duplicates = 0
        by_type: dict[str, int] = {}
        for _, event in ordered:
            by_type[event.event_type] = by_type.get(event.event_type, 0) + 1
            if (
                after_sequence is not None
                and event.sequence is not None
                and event.sequence <= after_sequence
            ):
                skipped += 1
                continue
            if self._model.is_applied(event.event_id):
                duplicates += 1
                continue
            if self.apply(event):
                applied += 1
            else:
                skipped += 1
        return ProjectStats(
            applied=applied, skipped=skipped, by_type=by_type, duplicates=duplicates
        )

    # -- checkpoint persistence ----------------------------------------------

    def save_checkpoint(self, path: Path) -> None:
        """Persist the resume point next to the read model's storage."""
        path.write_text(
            json.dumps(
                {
                    "last_sequence": self._model.checkpoint_sequence,
                    "applied_events": self._model.applied_event_count,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def load_checkpoint(self, path: Path) -> int | None:
        """Read a previously saved resume point (None when absent).

        Raises:
            ValueError: On a malformed checkpoint file.
        """
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        last = data.get("last_sequence")
        if last is not None and (not isinstance(last, int) or last < 0):
            raise ValueError(f"malformed checkpoint {path}: bad last_sequence {last!r}")
        return last


def replay_log(log: EventLog) -> tuple[ReadModel, ProjectStats]:
    """Build a fresh read model by replaying the entire event log."""
    model = ReadModel()
    stats = Projector(model).replay(log.read_all())
    return model, stats
