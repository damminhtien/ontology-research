"""Merge two canonical entities via an append-only EntityMerged event.

Repairs under-merges (ADR-0006): two canonical ids that in fact denote one
real-world entity (e.g. a rare duplicate in the upstream source). The
identity registry is rebuilt from the event log first so the merge is
validated against real canonical state; then the correction event is
appended to the log and written to the lake for OLAP.

Usage:
    .venv/bin/python tools/merge_entities.py \
        --survivor urn:world:entity:<hex> --duplicate urn:world:entity:<hex> \
        [--reason "duplicate QID pair confirmed"] [--log data/events.jsonl] \
        [--lake /path/to/lake | --no-lake]

Exits 1 without writing anything when the merge is rejected (unknown id,
self-merge, type conflict, already merged).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.events import EventLog
from foundry.lake import default_lake_root, persist_events
from foundry.merge import merge_entities, rebuild_identity


def main() -> int:
    """Run one interactive merge correction."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--survivor", required=True, help="canonical id to keep")
    parser.add_argument("--duplicate", required=True, help="canonical id to fold away")
    parser.add_argument("--reason", default="", help="why the merge was confirmed")
    parser.add_argument("--log", default="data/events.jsonl", help="event log path")
    parser.add_argument(
        "--lake", default=None, help="lake root (default $FOUNDRY_LAKE_ROOT or repo)"
    )
    parser.add_argument("--no-lake", action="store_true", help="skip the lake write")
    args = parser.parse_args()

    log = EventLog(Path(args.log))
    identity = rebuild_identity(log)
    try:
        result = merge_entities(
            identity=identity,
            log=log,
            survivor_id=args.survivor,
            duplicate_id=args.duplicate,
            reason=args.reason,
        )
    except ValueError as exc:
        print(f"merge rejected: {exc}", file=sys.stderr)
        return 1

    print(f"merged {result.duplicate_id} into {result.survivor_id}")
    print(f"  event: {result.event.event_id} (EntityMerged)")
    print(
        f"  moved: {len(result.moved_aliases)} alias(es), "
        f"{len(result.moved_external_ids)} external id(s)"
    )
    if not args.no_lake:
        root = Path(args.lake) if args.lake else default_lake_root()
        root.mkdir(parents=True, exist_ok=True)
        files = persist_events([result.event], root)
        rows = sum(f.rows for f in files)
        print(f"lake: wrote {rows} event(s) to {root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
