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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from foundry.events import EventLog, SemanticEvent
from foundry.readmodel import ReadModel, ReadModelStore, parse_instant


@dataclass(frozen=True)
class ProjectStats:
    """Counters from one replay."""

    applied: int
    skipped: int
    by_type: dict[str, int]
    duplicates: int = 0


class Projector:
    """Applies domain events onto a ReadModel."""

    def __init__(self, model: ReadModelStore) -> None:
        """Bind the projector to a target read model."""
        self._model = model

    def apply(self, event: SemanticEvent) -> bool:
        """Apply one event; returns False when the type is not handled.

        Re-applying an event that was already folded into the model is a
        no-op (reported as handled, counted as a duplicate by :meth:`replay`).
        """
        with self._model.transaction():
            return self._apply(event)

    def _apply(
        self,
        event: SemanticEvent,
        *,
        check_applied: bool = True,
        record: bool = True,
    ) -> bool:
        """Apply one event inside the store transaction scope."""
        if check_applied and self._model.is_applied(event.event_id):
            return True
        occurred = parse_instant(event.occurred_at)
        payload = event.payload

        if event.event_type == "EntityCreated":
            self._model.upsert_entity(
                entity_id=payload["entity_id"],
                entity_type=payload["entity_type"],
                name=payload["name"],
                event_time=occurred,
                aliases=payload.get("name_aliases") or (),
            )
        elif event.event_type == "EntityMerged":
            self._model.merge_entities(
                survivor_id=payload["survivor_id"],
                duplicate_id=payload["duplicate_id"],
                event_time=occurred,
            )
        elif event.event_type == "EntitySplit":
            self._model.unmerge_entities(
                survivor_id=payload["survivor_id"],
                duplicate_id=payload["duplicate_id"],
                event_time=occurred,
            )
        elif event.event_type == "ExternalIdBound":
            pass  # identity-level fact; the read model holds no binding state
        elif event.event_type == "SourceRecordMatched":
            pass  # identity-level source-record mapping; no operational read projection
        elif event.event_type == "EntityCannotLinkAsserted":
            pass  # identity constraint; no operational read projection
        elif event.event_type == "ResolutionReviewQueued":
            pass  # durable review-queue fact; drained by review tooling, not the read model
        elif event.event_type == "AssertionMade":
            self._model.add_assertion(payload)
        elif event.event_type == "AssertionSuperseded":
            self._model.supersede_assertion(payload["assertion_id"], payload.get("reason", ""))
        elif event.event_type == "DocumentRegistered":
            pass  # provenance source; served from the log/lake, not the read model
        elif event.event_type == "SensorRegistered":
            # the sensor is an entity (an Artifact) — enter the entity index
            self._model.upsert_entity(
                entity_id=payload["sensor_entity_id"],
                entity_type="Artifact",
                name=payload["name"],
                event_time=occurred,
            )
        elif event.event_type == "ObservationRecorded":
            pass  # detection fact; counted, its track hypothesis lands below
        elif event.event_type == "TrackObserved":
            self._model.add_track(payload)
        else:
            return False

        if record:
            self._model.touch(occurred)
            self._model.mark_applied(event.event_id, event.sequence)
        return True

    def apply_batch(
        self,
        events: Iterable[SemanticEvent],
        *,
        before_apply: Callable[[SemanticEvent], None] | None = None,
        before_apply_batch: Callable[[list[SemanticEvent]], None] | None = None,
    ) -> ProjectStats:
        """Apply one bounded group in a single store transaction.

        Store adapters with bulk methods can coalesce entity, assertion, ledger,
        and checkpoint writes. Callbacks run inside the same transaction, so
        identity and read projections commit together.
        """
        if before_apply is not None and before_apply_batch is not None:
            raise ValueError("choose one before-apply callback")
        batch = list(events)
        if not batch:
            return ProjectStats(applied=0, skipped=0, by_type={})

        applied = skipped = duplicates = 0
        by_type: dict[str, int] = {}
        processed: list[tuple[SemanticEvent, bool]] = []
        handled_times = []
        pending_entities: list[SemanticEvent] = []
        pending_assertions: list[SemanticEvent] = []
        pending_assertion_ids: set[str] = set()
        event_ids = [event.event_id for event in batch]
        bulk_lookup = getattr(self._model, "applied_event_ids", None)
        bulk_mark = getattr(self._model, "mark_processed_batch", None)
        bulk_assertions = getattr(self._model, "add_assertions_batch", None)
        bulk_entities = getattr(self._model, "upsert_entities_batch", None)

        def flush_entities() -> None:
            nonlocal applied
            if not pending_entities:
                return
            bulk_entities(
                [
                    {
                        "entity_id": event.payload["entity_id"],
                        "entity_type": event.payload["entity_type"],
                        "name": event.payload["name"],
                        "event_time": parse_instant(event.occurred_at),
                        "aliases": event.payload.get("name_aliases") or (),
                    }
                    for event in pending_entities
                ]
            )
            for pending in pending_entities:
                applied += 1
                processed.append((pending, True))
                handled_times.append(parse_instant(pending.occurred_at))
            pending_entities.clear()

        def flush_assertions() -> None:
            nonlocal applied
            if not pending_assertions:
                return
            bulk_assertions([event.payload for event in pending_assertions])
            for pending in pending_assertions:
                applied += 1
                processed.append((pending, True))
                handled_times.append(parse_instant(pending.occurred_at))
            pending_assertions.clear()
            pending_assertion_ids.clear()

        with self._model.transaction():
            already_applied = bulk_lookup(event_ids) if callable(bulk_lookup) else set()
            seen: set[str] = set()
            new_events = []
            for event in batch:
                by_type[event.event_type] = by_type.get(event.event_type, 0) + 1
                previously_processed = (
                    event.event_id in already_applied
                    if callable(bulk_lookup)
                    else self._model.is_applied(event.event_id)
                )
                if event.event_id in seen or previously_processed:
                    duplicates += 1
                    continue
                seen.add(event.event_id)
                new_events.append(event)

            if before_apply_batch is not None:
                before_apply_batch(new_events)

            for event in new_events:
                if before_apply is not None:
                    before_apply(event)

                if callable(bulk_entities) and event.event_type == "EntityCreated":
                    pending_entities.append(event)
                    continue

                if (
                    callable(bulk_assertions)
                    and event.event_type == "AssertionMade"
                    and not event.payload.get("supersedes")
                ):
                    assertion_id = event.payload["assertion_id"]
                    if assertion_id not in pending_assertion_ids:
                        pending_assertions.append(event)
                        pending_assertion_ids.add(assertion_id)
                        continue
                    flush_assertions()
                elif event.event_type in {"AssertionMade", "AssertionSuperseded"}:
                    flush_assertions()

                if event.event_type in {"EntityMerged", "EntitySplit", "SensorRegistered"}:
                    flush_entities()
                    flush_assertions()

                handled = self._apply(event, check_applied=False, record=False)
                if handled:
                    applied += 1
                    handled_times.append(parse_instant(event.occurred_at))
                else:
                    skipped += 1
                processed.append((event, handled))

            flush_entities()
            flush_assertions()

            if callable(bulk_mark):
                bulk_mark(
                    ((event.event_id, event.sequence, handled) for event, handled in processed),
                    last_event_time=max(handled_times) if handled_times else None,
                )
            else:
                for event, handled in processed:
                    if handled:
                        self._model.touch(parse_instant(event.occurred_at))
                        self._model.mark_applied(event.event_id, event.sequence)
                    else:
                        self._model.mark_skipped(event.event_id, event.sequence)

        return ProjectStats(
            applied=applied,
            skipped=skipped,
            by_type=by_type,
            duplicates=duplicates,
        )

    def replay(
        self, events: Iterable[SemanticEvent], *, after_sequence: int | None = None
    ) -> ProjectStats:
        """Fold an event iterable in log order.

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
        return self.replay_in_order((event for _, event in ordered), after_sequence=after_sequence)

    def replay_in_order(
        self, events: Iterable[SemanticEvent], *, after_sequence: int | None = None
    ) -> ProjectStats:
        """Fold events already supplied in log order without sorting/materializing.

        Use :meth:`EventLog.iter_all` for a streamed, sequence-validated input.
        Callers passing another iterable are responsible for preserving order.
        """
        applied = 0
        skipped = 0
        duplicates = 0
        by_type: dict[str, int] = {}
        for event in events:
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
                self._model.mark_skipped(event.event_id, event.sequence)
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
    """Build a fresh read model by streaming the entire event log."""
    model = ReadModel()
    stats = Projector(model).replay_in_order(log.iter_all())
    return model, stats
