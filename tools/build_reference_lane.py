"""Build a versioned reference-lane snapshot from the live Wikidata endpoint.

Fetches the P31 class closure with QID-cursor paging (no overlapping LIMIT
windows), normalizes rows, and publishes one typed Parquet snapshot to the
reference lane. The lane is reference data — rebuildable at any time, never a
source of truth; ingestion reads it via ``tools/ingest_wikidata.py
--from-lane`` and facts still pass identity + SHACL into the event log.

Usage:
    .venv/bin/python tools/build_reference_lane.py [--class Q43229]
        [--page-size 2000] [--max-records 20000] [--timeout 60]
        [--root data/reference]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry import wikidata as wd  # noqa: E402
from foundry.reference import default_reference_root, write_snapshot  # noqa: E402


def main() -> int:
    """Fetch one cursor-paged pass and publish it as a lane snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--class", dest="class_qid", default="Q43229", help="root Wikidata class")
    parser.add_argument("--page-size", type=int, default=2000, help="rows per cursor page")
    parser.add_argument("--max-records", type=int, default=None, help="stop after N rows")
    parser.add_argument("--timeout", type=float, default=60.0, help="SPARQL request timeout (s)")
    parser.add_argument("--root", default=None, help="lane root (default data/reference)")
    args = parser.parse_args()

    root = Path(args.root) if args.root else default_reference_root()
    print(f"fetching P31/{args.class_qid} with cursor paging (page_size={args.page_size}) ...")
    rows = list(
        wd.iter_entities(
            class_qid=args.class_qid,
            page_size=args.page_size,
            max_records=args.max_records,
            timeout=args.timeout,
        )
    )
    usable = sum(1 for r in rows if r.name_vi or r.name_en)
    print(f"fetched {len(rows)} rows ({usable} usable)")

    info = write_snapshot(rows, root=root, class_qid=args.class_qid)
    print(f"lane: published {info.id} ({info.rows} rows) at {root / info.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
