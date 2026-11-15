#!/usr/bin/env python3
"""Export the append-only event log as a local TriG dataset."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.events import EventLog  # noqa: E402
from foundry.rdf_export import (  # noqa: E402
    iter_projected_quads,
    open_projection_context_index,
    validate_export_options,
    write_trig_stream,
)


def _sync_output_directory(path: Path) -> None:
    """Best-effort sync after the completed output is atomically published."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _validate_paths(
    event_log: Path,
    output: Path,
    geometry: Path | None,
    context_index: Path | None,
) -> None:
    """Prevent replacing any source file or geometry input with an output."""
    source_segments = {
        Path(path).resolve() for path, _size, _mtime in EventLog(event_log).fingerprint()
    }
    source_segments.add(event_log.resolve())
    output_path = output.resolve()
    if output_path in source_segments:
        raise ValueError("output path must not be the event log or one of its segments")
    if geometry is not None and output_path == geometry.resolve():
        raise ValueError("output path must not replace the geometry CSV")
    if context_index is not None:
        index_path = context_index.resolve()
        if index_path in source_segments:
            raise ValueError("context index path must not be the event log or one of its segments")
        if index_path == output_path:
            raise ValueError("context index path must not replace the RDF output")
        if geometry is not None and index_path == geometry.resolve():
            raise ValueError("context index path must not replace the geometry CSV")


def main() -> int:
    """Stream a bounded-memory RDF projection and atomically publish TriG."""
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
    parser.add_argument(
        "--context-index",
        type=Path,
        help="optional disk-backed SQLite reference index to retain and reuse",
    )
    args = parser.parse_args()
    temporary_name: str | None = None
    try:
        validate_export_options(args.profile, args.geometry)
        _validate_paths(args.event_log, args.output, args.geometry, args.context_index)
        args.output.parent.mkdir(parents=True, exist_ok=True)

        def export_with_index(index_path: Path) -> tuple[int, int, tuple[str, ...]]:
            with open_projection_context_index(args.event_log, index_path) as context:
                context.verify_source(args.event_log)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    dir=args.output.parent,
                    prefix=f".{args.output.name}.",
                    delete=False,
                ) as temporary:
                    nonlocal temporary_name
                    temporary_name = temporary.name
                    statement_lines = write_trig_stream(
                        iter_projected_quads(
                            args.event_log,
                            profile=args.profile,
                            geometry_path=args.geometry,
                            context=context,
                        ),
                        temporary,
                    )
                    temporary.flush()
                    os.fsync(temporary.fileno())
                context.verify_source(args.event_log)
                Path(temporary_name).replace(args.output)
                _sync_output_directory(args.output.parent)
                temporary_name = None
                return statement_lines, context.event_count, context.skipped_event_types

        if args.context_index is None:
            with tempfile.TemporaryDirectory(
                prefix=".rdf-export-index-", dir=args.output.parent
            ) as temporary_index_dir:
                statement_count, event_count, skipped_event_types = export_with_index(
                    Path(temporary_index_dir) / "context.sqlite"
                )
        else:
            statement_count, event_count, skipped_event_types = export_with_index(
                args.context_index
            )
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        parser.exit(2, f"RDF export failed: {exc}\n")
    print(
        f"Wrote {statement_count} RDF triple lines "
        f"from {event_count} events to {args.output} ({args.profile})."
    )
    if skipped_event_types:
        skipped = ", ".join(skipped_event_types)
        print(f"Events without an RDF mapper were skipped: {skipped}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
