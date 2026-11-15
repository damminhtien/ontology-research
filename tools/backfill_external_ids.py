"""Backfill external-id bindings for pre-v2 EntityCreated payloads.

Events written before ``external_ids`` was added to the ``EntityCreated``
payload carry no machine-readable QID binding; the only trace is the
``source_id`` convention (``wikidata:Q127990``). This tool scans the log,
re-derives the missing ``("wikidata", <QID>)`` bindings and records each one
as an append-only ``ExternalIdBound`` fact so future restarts recover the
full registry from the log alone (ADR-0002: the log is never rewritten).

Usage:
    .venv/bin/python tools/backfill_external_ids.py \
        [--log ~/Data/ontology-research/data/production.jsonl]
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
from foundry.identity import IdentityService
from foundry.lake import default_lake_root, persist_events
from foundry.paths import production_event_log_path
from foundry.storage import identity_service_for_log

# source_id convention written by tools/ingest_wikidata.py.
_WIKIDATA_SOURCE = re.compile(r"^wikidata:(Q\d+)$")


def collect_missing_binding_events(
    log: EventLog, *, identity: IdentityService | None = None
) -> list[SemanticEvent]:
    """Return one ExternalIdBound event per unrecorded ``wikidata:<QID>`` source.

    The registry is rebuilt from the log first so already-recorded bindings
    (explicit payloads or earlier backfills) are excluded.
    """
    if identity is None:
        identity, _apply_projection = identity_service_for_log(log)
    events: list[SemanticEvent] = []
    pending: set[tuple[str, str]] = set()
    for event in log.iter_all():
        if event.event_type != "EntityCreated":
            continue
        match = _WIKIDATA_SOURCE.match(event.payload.get("source_id") or "")
        if match is None:
            continue
        entity_id = event.payload["entity_id"]
        qid = match.group(1)
        key = (entity_id, qid)
        if key in pending or entity_id in identity.store.external_owners("wikidata", qid):
            continue
        pending.add(key)
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
    parser.add_argument("--log", default=production_event_log_path(), help="event log path")
    parser.add_argument(
        "--lake", default=None, help="lake root (default $FOUNDRY_LAKE_ROOT or external Data root)"
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
