#!/usr/bin/env python3
"""Export the append-only event log as a local TriG dataset."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.rdf_export import project_event_log  # noqa: E402


def main() -> int:
    """Parse arguments, project the log, and atomically write TriG output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-log", required=True, type=Path, help="event-log base path")
    parser.add_argument(
        "--profile",
        required=True,
        choices=("canonical-v1", "interop-v1"),
        help="canonical repository RDF or standards-aligned named graph",
    )
    parser.add_argument("--output", required=True, type=Path, help="output TriG path")
    parser.add_argument(
        "--geometry",
        type=Path,
        help="optional location_iri,longitude,latitude,source_iri CSV for interop-v1",
    )
    args = parser.parse_args()
    try:
        summary = project_event_log(
            args.event_log,
            profile=args.profile,
            geometry_path=args.geometry,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        rendered = summary.dataset.serialize(format="trig", encoding="utf-8")
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=args.output.parent, prefix=f".{args.output.name}.", delete=False
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(rendered)
                temporary.flush()
                os.fsync(temporary.fileno())
            Path(temporary_name).replace(args.output)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"RDF export failed: {exc}\n")
    statement_count = sum(1 for _ in summary.dataset.quads((None, None, None, None)))
    print(
        f"Exported {statement_count} RDF statements "
        f"from {summary.event_count} events to {args.output} ({args.profile})."
    )
    if summary.skipped_event_types:
        skipped = ", ".join(summary.skipped_event_types)
        print(f"Events without an RDF mapper were skipped: {skipped}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
