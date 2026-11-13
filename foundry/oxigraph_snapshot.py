"""Atomic, rebuildable Oxigraph snapshots derived from the append-only event log."""

from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from rdflib import RDF, Graph, URIRef
from rdflib.namespace import OWL

from foundry.events import EventLog
from foundry.rdf_export import collect_projection_context, iter_projected_quads

REPO_ROOT = Path(__file__).resolve().parents[1]
DCTERMS_VERSION = URIRef("http://purl.org/dc/terms/version")


def _source_manifest(event_log_path: Path) -> tuple[tuple[tuple[str, int, int], ...], dict]:
    """Return a content digest and segment details for a stable event log."""
    log = EventLog(event_log_path)
    before = log.fingerprint()
    overall = hashlib.sha256()
    segments = []
    for path_text, size, _mtime_ns in before:
        path = Path(path_text)
        segment_hash = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                segment_hash.update(chunk)
        digest = segment_hash.hexdigest()
        overall.update(path.name.encode("utf-8"))
        overall.update(b"\0")
        overall.update(size.to_bytes(8, "big"))
        overall.update(bytes.fromhex(digest))
        segments.append({"name": path.name, "size_bytes": size, "sha256": digest})
    after = log.fingerprint()
    if before != after:
        raise RuntimeError("event log changed while its source fingerprint was computed")
    return before, {
        "sha256": overall.hexdigest(),
        "segments": segments,
        "size_bytes": sum(segment["size_bytes"] for segment in segments),
    }


def _ontology_versions() -> list[dict[str, str]]:
    """Read each registered ontology module's declared IRI and version."""
    modules = []
    for path in sorted((REPO_ROOT / "ontology").rglob("*.ttl")):
        graph = Graph().parse(path.as_posix(), format="turtle")
        ontology = next(graph.subjects(RDF.type, OWL.Ontology), None)
        version = next(graph.objects(ontology, DCTERMS_VERSION), None) if ontology else None
        modules.append(
            {
                "path": path.relative_to(REPO_ROOT).as_posix(),
                "iri": str(ontology or ""),
                "version": str(version or ""),
            }
        )
    return modules


def _write_quads(path: Path, quads) -> int:
    """Write RDFLib quads as streaming N-Quads and return the emitted line count."""
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for subject, predicate, object_, graph in quads:
            line = f"{subject.n3()} {predicate.n3()} {object_.n3()}"
            if graph is not None:
                line += f" {graph.n3()}"
            handle.write(f"{line} .\n")
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count


def _sync_directory(path: Path) -> None:
    """Best-effort directory sync after an atomic rename."""
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


def _publish_current(root: Path, snapshot_name: str) -> None:
    """Atomically point CURRENT at a completed snapshot directory."""
    temporary = root / f".CURRENT-{uuid.uuid4().hex}.tmp"
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{snapshot_name}\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(root / "CURRENT")
    _sync_directory(root)


def build_snapshot(
    event_log_path: Path,
    snapshot_root: Path,
    *,
    profile: str = "canonical-v1",
    geometry_path: Path | None = None,
) -> Path:
    """Build, reopen, and atomically publish one immutable Oxigraph snapshot."""
    try:
        from pyoxigraph import RdfFormat, Store
    except ImportError as exc:
        raise RuntimeError(
            "pyoxigraph is required; use `make oxigraph-eval` or the evaluation environment"
        ) from exc

    if profile not in {"canonical-v1", "interop-v1"}:
        raise ValueError(f"unknown RDF export profile {profile!r}")
    if geometry_path is not None and profile != "interop-v1":
        raise ValueError("geometry CSV is only supported by the interop-v1 profile")

    root = snapshot_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    log = EventLog(event_log_path)
    initial_fingerprint = log.fingerprint()
    context = collect_projection_context(event_log_path)
    candidate = Path(tempfile.mkdtemp(prefix=".candidate-", dir=root))
    published_path: Path | None = None
    published = False
    build_started = time.perf_counter()
    try:
        store_path = candidate / "store"
        store_path.mkdir()
        projection_path = candidate / "projection.nq"
        emitted_quad_count = _write_quads(
            projection_path,
            iter_projected_quads(
                event_log_path,
                profile=profile,
                geometry_path=geometry_path,
                context=context,
            ),
        )
        store = Store(str(store_path))
        # `load` preserves document-wide blank-node identity across input chunks.
        store.load(path=str(projection_path), format=RdfFormat.N_QUADS)
        store.flush()
        triple_count = len(store)
        del store
        gc.collect()
        projection_path.unlink()

        reopen_started = time.perf_counter()
        reopened = Store.read_only(str(store_path))
        reopened_count = len(reopened)
        # Exercise the query API before publishing, even for an empty log.
        reopened.query("ASK { ?subject ?predicate ?object }")
        del reopened
        gc.collect()
        reopen_seconds = time.perf_counter() - reopen_started
        if reopened_count != triple_count:
            raise RuntimeError(
                f"Oxigraph reopen count changed from {triple_count} to {reopened_count}"
            )

        final_fingerprint, source = _source_manifest(event_log_path)
        if initial_fingerprint != final_fingerprint:
            raise RuntimeError(
                "event log changed during snapshot construction; candidate discarded"
            )

        manifest = {
            "format_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "source": {
                **source,
                "last_sequence": context.last_sequence,
                "event_count": context.event_count,
            },
            "ontology_modules": _ontology_versions(),
            "profile": profile,
            "engine": {"name": "Oxigraph", "pyoxigraph_version": "0.5.13"},
            "projection": {
                "emitted_quad_count": emitted_quad_count,
                "triple_count": triple_count,
                "reopen_seconds": round(reopen_seconds, 6),
                "build_seconds": round(time.perf_counter() - build_started, 6),
            },
        }
        manifest_path = candidate / "manifest.json"
        with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        snapshot_name = f"snapshot-{source['sha256'][:12]}-{uuid.uuid4().hex[:8]}"
        published_path = root / snapshot_name
        candidate.replace(published_path)
        _sync_directory(root)
        _publish_current(root, snapshot_name)
        published = True
        return published_path
    except Exception:
        if candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)
        if published_path is not None and published_path.exists() and not published:
            shutil.rmtree(published_path, ignore_errors=True)
        raise


def current_snapshot(snapshot_root: Path) -> Path:
    """Resolve and validate the snapshot named by the atomic CURRENT pointer."""
    root = snapshot_root.resolve()
    current = root / "CURRENT"
    if not current.is_file():
        raise FileNotFoundError(f"no published Oxigraph snapshot at {current}")
    name = current.read_text(encoding="utf-8").strip()
    if not name or Path(name).name != name:
        raise ValueError(f"invalid snapshot name in {current}")
    snapshot = root / name
    if not (snapshot / "manifest.json").is_file() or not (snapshot / "store").is_dir():
        raise ValueError(f"CURRENT points to an incomplete snapshot: {snapshot}")
    return snapshot


def open_current_store(snapshot_root: Path):
    """Open the current snapshot with Oxigraph's read-only store API."""
    try:
        from pyoxigraph import Store
    except ImportError as exc:
        raise RuntimeError("pyoxigraph is required to open an Oxigraph snapshot") from exc
    return Store.read_only(str(current_snapshot(snapshot_root) / "store"))


def directory_size_bytes(path: Path) -> int:
    """Return the total size of regular files below a snapshot directory."""
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
