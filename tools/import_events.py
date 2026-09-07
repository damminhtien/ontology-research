"""Import events from one log into another, re-stamping sequences.

A sequence is an offset assigned by the *receiving* log, so moving events
between logs re-assigns them (this is what saved the split-log incident of
2026-09-02 from becoming 185 permanent duplicates). Event ids are preserved —
they are the stable identity used by the lake and read-model dedup.

Usage:
    .venv/bin/python tools/import_events.py --src data/events.jsonl \
        --dst data/production.jsonl [--skip N] [--dry-run]

``--skip N`` drops the first N source events (e.g. seed records that must not
enter the production log).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.events import EventLog


def main() -> int:
    """Copy source events into the destination log with fresh sequences."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True, help="source log path")
    parser.add_argument("--dst", required=True, help="destination log path")
    parser.add_argument("--skip", type=int, default=0, help="skip the first N source events")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    src = EventLog(Path(args.src))
    dst = EventLog(Path(args.dst))
    events = [replace(e, sequence=None) for e in src.read_all()][args.skip :]
    print(f"import: {len(events)} event(s) from {args.src} -> {args.dst} (skip={args.skip})")
    if args.dry_run:
        return 0
    written = dst.extend(events)
    print(f"done: {written} event(s) appended; destination now has {len(dst.read_all())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
