"""Undo a previously recorded entity merge via an append-only EntitySplit event.

Repairs a wrong merge: the bindings that the ``EntityMerged`` event moved are
moved back, and the split references the merge event as its auditable cause —
a split without a recorded merge is rejected.

Usage:
    .venv/bin/python tools/split_entity.py \
        --survivor urn:world:entity:<hex> --duplicate urn:world:entity:<hex> \
        [--reason "merge was a false positive"] [--log data/production.jsonl] \
        [--lake /path/to/lake | --no-lake]

Exits 1 without writing anything when the split is rejected.
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
from foundry.merge import rebuild_identity, split_entities


def main() -> int:
    """Run one interactive un-merge correction."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--survivor", required=True, help="canonical id the merge kept")
    parser.add_argument("--duplicate", required=True, help="canonical id to restore")
    parser.add_argument("--reason", default="", help="why the merge was undone")
    parser.add_argument("--log", default="data/production.jsonl", help="event log path")
    parser.add_argument(
        "--lake", default=None, help="lake root (default $FOUNDRY_LAKE_ROOT or repo)"
    )
    parser.add_argument("--no-lake", action="store_true", help="skip the lake write")
    args = parser.parse_args()

    log = EventLog(Path(args.log))
    identity = rebuild_identity(log)
    try:
        result = split_entities(
            identity=identity,
            log=log,
            survivor_id=args.survivor,
            duplicate_id=args.duplicate,
            reason=args.reason,
        )
    except ValueError as exc:
        print(f"split rejected: {exc}", file=sys.stderr)
        return 1

    print(f"split {result.duplicate_id} out of {result.survivor_id}")
    print(f"  event: {result.event.event_id} (EntitySplit)")
    print(
        f"  restored: {len(result.restored_aliases)} alias(es), "
        f"{len(result.restored_external_ids)} external id(s)"
    )
    if result.retained_aliases or result.retained_external_ids:
        print(
            f"  retained (claimed by other entities meanwhile): "
            f"{len(result.retained_aliases)} alias(es), "
            f"{len(result.retained_external_ids)} external id(s)"
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
