#!/usr/bin/env python3
"""Measure bounded-memory RDF export on deterministic, cross-linked event logs.

The default run writes its generated logs, indexes, exports, and raw report under
an ignored ``build/`` directory. A compact reproducibility summary can also be
written to a versioned report path with ``--summary-path``.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import json
import os
import platform
import re
import resource
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "tools", REPO_ROOT):
    if _path.as_posix() not in sys.path:
        sys.path.insert(0, _path.as_posix())

from foundry.events import EVENT_SCHEMA_VERSION, EVENT_TYPE_ASSERTION_MADE, EventLog, SemanticEvent  # noqa: E402
from foundry.namespaces import CORE_LOCATED_AT  # noqa: E402
from foundry.rdf_export import open_projection_context_index  # noqa: E402

EVENT_TYPES_PER_GROUP = 4
SEGMENT_BYTES = 256 * 1024 * 1024
EXPORT_SUMMARY = re.compile(r"Wrote (\d+) RDF triple lines from (\d+) events")


def _peak_rss_bytes(who: int = resource.RUSAGE_SELF) -> int:
    """Return process high-water RSS in bytes on macOS and Linux."""
    peak = resource.getrusage(who).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _event(event_type: str, event_id: str, payload: dict) -> SemanticEvent:
    return SemanticEvent(
        event_id=event_id,
        event_type=event_type,
        schema_version=EVENT_SCHEMA_VERSION,
        occurred_at="2026-01-01T00:00:00Z",
        payload=payload,
    )


def generate_events(event_count: int):
    """Yield four valid mapper event types with distinct cross-event IDs."""
    if event_count < EVENT_TYPES_PER_GROUP or event_count % EVENT_TYPES_PER_GROUP:
        raise ValueError("event count must be a positive multiple of four")

    for index in range(event_count // EVENT_TYPES_PER_GROUP):
        suffix = f"{index:08d}"
        document_id = f"urn:document:rdf-export-benchmark:{suffix}"
        observation_id = f"urn:observation:rdf-export-benchmark:{suffix}"
        entity_id = f"urn:entity:rdf-export-benchmark:{suffix}"
        location_id = f"urn:location:rdf-export-benchmark:{suffix}"

        yield _event(
            "DocumentRegistered",
            f"rdf-export-benchmark-document-{suffix}",
            {
                "document_id": document_id,
                "title": f"Benchmark source {suffix}",
            },
        )
        yield _event(
            "ObservationRecorded",
            f"rdf-export-benchmark-observation-{suffix}",
            {
                "observation_id": observation_id,
                "sensor_entity_id": f"urn:sensor:rdf-export-benchmark:{suffix}",
                "subject_entity_id": entity_id,
                "subject_name": f"Benchmark subject {suffix}",
                "at_time": "2026-01-01T00:00:00Z",
                "observation_result_id": f"urn:result:rdf-export-benchmark:{suffix}",
                "position_uri": location_id,
                "measurements": [],
                "source_ids": [document_id],
                "evidence": [],
                "confidence": None,
            },
        )
        yield _event(
            EVENT_TYPE_ASSERTION_MADE,
            f"rdf-export-benchmark-assertion-{suffix}",
            {
                "assertion_id": f"urn:assertion:rdf-export-benchmark:{suffix}",
                "subject_id": entity_id,
                "subject_ref": None,
                "predicate_iri": CORE_LOCATED_AT,
                "object": {"kind": "location", "value": location_id},
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_until": None,
                "source_ids": [document_id],
                "evidence": [],
                "confidence": 1.0,
                "supersedes": None,
            },
        )
        yield _event(
            "TrackObserved",
            f"rdf-export-benchmark-track-{suffix}",
            {
                "track_id": f"rdf-export-benchmark-{suffix}",
                "entity_id": entity_id,
                "subject_name": f"Benchmark subject {suffix}",
                "observation_ids": [observation_id],
                "source_ids": [document_id],
            },
        )


def _generate_worker(event_count: int, log_path: Path, geometry_path: Path) -> dict:
    """Build one event log and matching geometry CSV in a fresh process."""
    start = time.perf_counter()
    written = EventLog(log_path, max_segment_bytes=SEGMENT_BYTES).extend(
        generate_events(event_count)
    )
    event_log_seconds = time.perf_counter() - start

    geometry_start = time.perf_counter()
    with geometry_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("location_iri", "longitude", "latitude", "source_iri"))
        for index in range(event_count // EVENT_TYPES_PER_GROUP):
            suffix = f"{index:08d}"
            writer.writerow(
                (
                    f"urn:location:rdf-export-benchmark:{suffix}",
                    "103.8",
                    "1.3",
                    f"urn:document:rdf-export-benchmark:{suffix}",
                )
            )
    return {
        "events_written": written,
        "event_log_seconds": round(event_log_seconds, 3),
        "geometry_rows": event_count // EVENT_TYPES_PER_GROUP,
        "geometry_seconds": round(time.perf_counter() - geometry_start, 3),
        "peak_rss_bytes": _peak_rss_bytes(),
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_event_log(log_path: Path) -> dict:
    """Hash all log segments in replay order without retaining their contents."""
    segments = EventLog(log_path).fingerprint()
    content_digest = hashlib.sha256()
    manifest_digest = hashlib.sha256()
    total_bytes = 0
    for raw_path, size, _mtime_ns in segments:
        path = Path(raw_path)
        segment_digest = hashlib.sha256()
        total_bytes += size
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                segment_digest.update(chunk)
                content_digest.update(chunk)
        manifest_digest.update(path.name.encode("utf-8"))
        manifest_digest.update(b"\0")
        manifest_digest.update(str(size).encode("ascii"))
        manifest_digest.update(b"\0")
        manifest_digest.update(segment_digest.hexdigest().encode("ascii"))
        manifest_digest.update(b"\n")
    return {
        "bytes": total_bytes,
        "segments": len(segments),
        "content_sha256": content_digest.hexdigest(),
        "segment_manifest_sha256": manifest_digest.hexdigest(),
    }


def _export_worker(
    log_path: Path,
    geometry_path: Path,
    output_path: Path,
    index_path: Path,
) -> dict:
    """Build the index and launch one isolated exporter process."""
    start = time.perf_counter()
    with open_projection_context_index(log_path, index_path) as context:
        if context.event_count < 1:
            raise RuntimeError("benchmark log unexpectedly contains no events")
    index_build_seconds = time.perf_counter() - start
    index_build_peak_rss = _peak_rss_bytes()

    export_start = time.perf_counter()
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "export_rdf.py"),
            "--event-log",
            str(log_path),
            "--profile",
            "interop-v1",
            "--output",
            str(output_path),
            "--geometry",
            str(geometry_path),
            "--context-index",
            str(index_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    export_seconds = time.perf_counter() - export_start
    if result.returncode:
        raise RuntimeError(f"export failed ({result.returncode}): {result.stderr.strip()}")
    match = EXPORT_SUMMARY.search(result.stdout)
    if match is None:
        raise RuntimeError(f"export summary was missing: {result.stdout.strip()}")

    output_hash = _hash_file(output_path)
    return {
        "index_build_seconds": round(index_build_seconds, 3),
        "index_build_peak_rss_bytes": index_build_peak_rss,
        "index_bytes": index_path.stat().st_size,
        "index_sha256": _hash_file(index_path),
        "export_seconds": round(export_seconds, 3),
        "export_peak_rss_bytes": _peak_rss_bytes(resource.RUSAGE_CHILDREN),
        "triple_lines": int(match.group(1)),
        "events_exported": int(match.group(2)),
        "output_bytes": output_path.stat().st_size,
        "output_sha256": output_hash,
    }


def _worker_call(*arguments: str) -> dict:
    """Run a fresh worker process and parse its single JSON result."""
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return json.loads(result.stdout)


def _machine_metadata() -> dict:
    """Return runtime and hardware details needed to interpret measurements."""
    import rdflib

    memory_bytes = None
    if hasattr(os, "sysconf"):
        with contextlib.suppress(ValueError, OSError):
            memory_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    machine_model = None
    if sys.platform == "darwin":
        result = subprocess.run(
            ["sysctl", "-n", "hw.model"], capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            machine_model = result.stdout.strip()
        memory = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=False
        )
        if memory.returncode == 0:
            memory_bytes = int(memory.stdout.strip())
        processor = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            check=False,
        )
        processor_name = processor.stdout.strip() if processor.returncode == 0 else None
    else:
        processor_name = platform.processor() or None
    return {
        "system": platform.platform(),
        "machine": platform.machine(),
        "model": machine_model,
        "processor": processor_name,
        "logical_cpu_count": os.cpu_count(),
        "physical_memory_bytes": memory_bytes,
        "python": platform.python_version(),
        "rdflib": rdflib.__version__,
        "sqlite": sqlite3.sqlite_version,
    }


def _run_scale(event_count: int, output_root: Path) -> dict:
    """Generate, hash, index, and export one scale in isolated processes."""
    if event_count < EVENT_TYPES_PER_GROUP or event_count % EVENT_TYPES_PER_GROUP:
        raise ValueError("every scale must be a positive multiple of four events")
    scale_dir = output_root / f"events-{event_count}"
    scale_dir.mkdir(parents=True)
    log_path = scale_dir / "events.jsonl"
    geometry_path = scale_dir / "geometry.csv"
    index_path = scale_dir / "projection-context.sqlite"
    output_path = scale_dir / "interop.trig"

    generated = _worker_call(
        "--generate-worker", str(event_count), str(log_path), str(geometry_path)
    )
    source_manifest = _hash_event_log(log_path)
    geometry_manifest = {
        "bytes": geometry_path.stat().st_size,
        "sha256": _hash_file(geometry_path),
    }
    measured = _worker_call(
        "--export-worker",
        str(log_path),
        str(geometry_path),
        str(output_path),
        str(index_path),
    )
    if generated["events_written"] != event_count:
        raise RuntimeError("generator wrote an unexpected number of events")
    if measured["events_exported"] != event_count:
        raise RuntimeError("exporter read an unexpected number of events")
    return {
        "events": event_count,
        "identifier_cardinality": {
            "documents": event_count // EVENT_TYPES_PER_GROUP,
            "observations": event_count // EVENT_TYPES_PER_GROUP,
            "locations": event_count // EVENT_TYPES_PER_GROUP,
        },
        "source_log": {**source_manifest, "generation": generated},
        "geometry_csv": geometry_manifest,
        "projection_index": {
            "bytes": measured["index_bytes"],
            "sha256": measured["index_sha256"],
            "build_seconds": measured["index_build_seconds"],
            "build_peak_rss_bytes": measured["index_build_peak_rss_bytes"],
        },
        "export": {
            "profile": "interop-v1",
            "seconds": measured["export_seconds"],
            "events_per_second": round(event_count / measured["export_seconds"], 2),
            "peak_rss_bytes": measured["export_peak_rss_bytes"],
            "triple_lines": measured["triple_lines"],
            "bytes": measured["output_bytes"],
            "sha256": measured["output_sha256"],
        },
    }


def _source_hashes() -> dict[str, str]:
    paths = (
        "tools/benchmark_rdf_export.py",
        "tools/export_rdf.py",
        "foundry/rdf_export.py",
        "foundry/events.py",
    )
    return {path: _hash_file(REPO_ROOT / path) for path in paths}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scales",
        nargs="+",
        type=int,
        default=[1_000_000, 10_000_000],
        help="event counts (each must be divisible by four)",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="new directory for generated data (default: unique build/ directory)",
    )
    parser.add_argument(
        "--summary-path",
        type=Path,
        help="optional JSON summary path, e.g. benchmarks/evaluations/reports/rdf-export-v1.json",
    )
    parser.add_argument(
        "--generate-worker",
        nargs=3,
        metavar=("EVENTS", "LOG", "GEOMETRY"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--export-worker",
        nargs=4,
        metavar=("LOG", "GEOMETRY", "OUTPUT", "INDEX"),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.generate_worker:
        count, path, geometry = args.generate_worker
        print(
            json.dumps(
                _generate_worker(int(count), Path(path), Path(geometry)),
                separators=(",", ":"),
            )
        )
        return 0
    if args.export_worker:
        log, geometry, output, index = map(Path, args.export_worker)
        print(
            json.dumps(
                _export_worker(log, geometry, output, index),
                separators=(",", ":"),
            )
        )
        return 0

    try:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_root = (
            args.output_root or REPO_ROOT / "build" / f"rdf-export-evaluation-{run_id}"
        ).resolve()
        if output_root.exists() and (not output_root.is_dir() or any(output_root.iterdir())):
            parser.error(f"output root must be new or empty: {output_root}")
        output_root.mkdir(parents=True, exist_ok=True)
        started = datetime.now(UTC).isoformat()
        results = [_run_scale(scale, output_root) for scale in args.scales]
        source_hashes = _source_hashes()
        source_tree_digest = hashlib.sha256(
            json.dumps(source_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        report = {
            "report_version": "rdf-export-benchmark-v1",
            "started_at_utc": started,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "source_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip(),
            "source_commit_scope": "source_sha256 records the measured working-tree file contents",
            "source_sha256": source_hashes,
            "source_tree_sha256": source_tree_digest,
            "command": (
                "make rdf-export-eval"
                if args.scales == [1_000_000, 10_000_000]
                else ".venv/bin/python tools/benchmark_rdf_export.py --scales "
                + " ".join(str(scale) for scale in args.scales)
            ),
            "machine": _machine_metadata(),
            "method": {
                "event_mix": {
                    "DocumentRegistered": 0.25,
                    "ObservationRecorded": 0.25,
                    "AssertionMade": 0.25,
                    "TrackObserved": 0.25,
                },
                "sqlite_page_cache_bytes": 8 * 1024 * 1024,
                "geometry_csv": "one validated CRS84 point for every distinct projected location",
                "export_process": (
                    "fresh process per scale; --context-index reuses the measured index"
                ),
                "rdf_semantics": (
                    "Small-fixture RDF dataset isomorphism is tested separately; large outputs "
                    "are hashed without materializing them for parsing."
                ),
                "timing_scope": (
                    "Index build and TriG export are separate. Export includes Python startup, "
                    "event parsing, RDF mapping, cross-reference lookups and serialization; "
                    "output and input hashing are outside the timed export."
                ),
                "repetitions": 1,
            },
            "results": results,
            "artifacts_directory": os.path.relpath(output_root, REPO_ROOT),
            "summary_path": (
                os.path.relpath(args.summary_path, REPO_ROOT)
                if args.summary_path is not None
                else None
            ),
        }
        report_path = output_root / "report.json"
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if args.summary_path is not None:
            args.summary_path.parent.mkdir(parents=True, exist_ok=True)
            args.summary_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        print(f"Raw report: {report_path}")
        if args.summary_path is not None:
            print(f"Versioned summary: {args.summary_path}")
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        parser.exit(2, f"RDF export benchmark failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
