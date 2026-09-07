"""Backfill external-id bindings for pre-v2 EntityCreated payloads.

Events written before ``external_ids`` was added to the ``EntityCreated``
payload carry no machine-readable QID binding; the only trace is the
``source_id`` convention (``wikidata:Q127990``). This tool scans the log,
re-derives the missing ``("wikidata", <QID>)`` bindings and records each one
as an append-only ``ExternalIdBound`` fact so future restarts recover the
full registry from the log alone (ADR-0002: the log is never rewritten).

Usage:
    .venv/bin/python tools/backfill_external_ids.py [--log data/production.jsonl]
                                                    [--lake /path/to/lake | --no-lake]

Idempotent: a second run finds every binding already present and appends
nothing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.events import EventLog, SemanticEvent, make_event
from foundry.lake import default_lake_root, persist_events
from foundry.merge import rebuild_identity

# source_id convention written by tools/ingest_wikidata.py.
_WIKIDATA_SOURCE = re.compile(r"^wikidata:(Q\d+)$")


def collect_missing_binding_events(log: EventLog) -> list[SemanticEvent]:
    """Return one ExternalIdBound event per unrecorded ``wikidata:<QID>`` source.

    The registry is rebuilt from the log first so already-recorded bindings
    (explicit payloads or earlier backfills) are excluded.
    """
    identity = rebuild_identity(log)
    events: list[SemanticEvent] = []
    for event in log.read_all():
        if event.event_type != "EntityCreated":
            continue
        match = _WIKIDATA_SOURCE.match(event.payload.get("source_id") or "")
        if match is None:
            continue
        entity_id = event.payload["entity_id"]
        qid = match.group(1)
        _, _, bound = identity.identity(entity_id)
        if qid in bound.get("wikidata", frozenset()):
            continue
        identity.add_external_id(entity_id, "wikidata", qid)
        events.append(
            make_event(
                "ExternalIdBound",
                {"entity_id": entity_id, "source": "wikidata", "external_id": qid},
            )
        )
    return events


def main() -> int:
    """Append one ExternalIdBound fact per missing Wikidata source binding."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default="data/production.jsonl", help="event log path")
    parser.add_argument(
        "--lake", default=None, help="lake root (default $FOUNDRY_LAKE_ROOT or repo)"
    )
    parser.add_argument("--no-lake", action="store_true", help="skip the lake write")
    args = parser.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"log not found: {log_path}", file=sys.stderr)
        return 1
    log = EventLog(log_path)
    events = collect_missing_binding_events(log)
    print(f"backfill: {len(events)} missing binding(s) found in {log_path}")
    if not events:
        return 0
    log.extend(events)
    print(f"log: appended {len(events)} ExternalIdBound event(s)")
    if not args.no_lake:
        root = Path(args.lake) if args.lake else default_lake_root()
        root.mkdir(parents=True, exist_ok=True)
        files = persist_events(events, root)
        rows = sum(f.rows for f in files)
        print(f"lake: wrote {rows} event(s) to {root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
