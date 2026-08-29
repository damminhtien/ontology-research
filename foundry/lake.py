"""Columnar lake archive for the canonical event log.

The append-only JSONL log (``foundry.events``) is the operational write path.
This module projects the same events into a compressed columnar lake for
long-term archival and analytical queries:

- **Parquet + zstd** — column layout plus strong compression keeps ~10x
  reduction against raw JSONL on disk (the lake lives on a small local volume).
- **Hive-style partitioning by event date** — analytical scans prune whole
  days without reading them.
- **Manifest** — every data file is registered with row/byte counts, so
  integrity can be verified and queries target exact file sets.

Design invariants:

- The lake is a *derived* store: rebuilding it from the event log must be
  lossless, so the log stays the single source of truth (ADR-0002).
- Writers never rewrite existing files; each flush creates a new immutable
  file, mirroring the append-only discipline of the event log.

Querying uses DuckDB (optional dependency, imported lazily): zero-copy scans
over Parquet via ``read_parquet`` with ``hive_partitioning=true``.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from foundry.events import EVENT_TYPES, SemanticEvent

SCHEMA_VERSION = 1
LAKE_VERSION = 1

MANIFEST_NAME = "manifest.json"

# Fallback when the archival volume is unavailable (e.g. no /data mount).
FALLBACK_ROOT_PARENT = "data"


def default_lake_root() -> Path:
    """Resolve the lake root: env override, else /data, else repo ``data/``."""
    env = os.environ.get("FOUNDRY_LAKE_ROOT")
    if env:
        return Path(env)
    preferred = Path("/data/ontology-lake")
    try:
        preferred.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(FALLBACK_ROOT_PARENT) / "lake"
    return preferred


def parse_occurred_at(value: str) -> datetime:
    """Parse the xsd:dateTime lexical form used by the event contract."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class LakeError(RuntimeError):
    """Raised when the lake cannot be written or fails verification."""


@dataclass(frozen=True)
class LakeFile:
    """One immutable Parquet file registered in the manifest."""

    path: str  # relative to lake root, POSIX style
    rows: int
    bytes: int
    min_occurred_at: str
    max_occurred_at: str
    written_at: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the manifest JSON."""
        return {
            "path": self.path,
            "rows": self.rows,
            "bytes": self.bytes,
            "min_occurred_at": self.min_occurred_at,
            "max_occurred_at": self.max_occurred_at,
            "written_at": self.written_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LakeFile:
        """Rebuild a manifest entry from its JSON form."""
        return cls(
            path=str(data["path"]),
            rows=int(data["rows"]),
            bytes=int(data["bytes"]),
            min_occurred_at=str(data["min_occurred_at"]),
            max_occurred_at=str(data["max_occurred_at"]),
            written_at=str(data["written_at"]),
        )


class LakeWriter:
    """Buffer events and flush them as compressed Parquet partitions.

    Usage::

        writer = LakeWriter(Path("/data/ontology-lake"))
        writer.write_events(events)   # buffers in memory
        writer.flush()                # writes one file per affected partition
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        max_rows_per_file: int = 500_000,
        compression_level: int = 7,
    ) -> None:
        """Create a writer; ``root`` defaults to ``$FOUNDRY_LAKE_ROOT`` or the repo default."""
        if max_rows_per_file < 1:
            raise ValueError("max_rows_per_file must be >= 1")
        if not 1 <= compression_level <= 22:
            raise ValueError("compression_level must be within zstd's 1..22 range")
        self._root = root if root is not None else default_lake_root()
        self._max_rows = max_rows_per_file
        self._level = compression_level
        self._buffer: list[SemanticEvent] = []

    @property
    def root(self) -> Path:
        """Lake root directory (all paths in the manifest are relative to it)."""
        return self._root

    def manifest_path(self) -> Path:
        """Absolute path of the manifest file."""
        return self._root / MANIFEST_NAME

    def write_events(self, events: list[SemanticEvent]) -> int:
        """Buffer events for the next flush; validates the contract on entry."""
        for event in events:
            if event.event_type not in EVENT_TYPES:
                raise LakeError(f"unknown event type {event.event_type!r}")
        self._buffer.extend(events)
        return len(events)

    def flush(self) -> list[LakeFile]:
        """Write buffered events to partitioned Parquet files; update manifest.

        Splitting happens per partition (event_date) and per
        ``max_rows_per_file`` so files stay compact and independently
        compressible. An empty buffer is a no-op.
        """
        if not self._buffer:
            return []

        import pyarrow as pa
        import pyarrow.parquet as pq

        by_date: dict[str, list[SemanticEvent]] = {}
        for event in self._buffer:
            by_date.setdefault(event.occurred_at[:10], []).append(event)
        self._buffer.clear()

        schema = pa.schema(
            [
                ("event_id", pa.string()),
                ("event_type", pa.string()),
                ("schema_version", pa.int8()),
                ("occurred_at", pa.string()),
                ("payload_json", pa.string()),
            ]
        )
        manifest = self._read_manifest()
        written: list[LakeFile] = []
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        for day in sorted(by_date):
            events = by_date[day]
            for start in range(0, len(events), self._max_rows):
                chunk = events[start : start + self._max_rows]
                table = pa.Table.from_pydict(
                    {
                        "event_id": [e.event_id for e in chunk],
                        "event_type": [e.event_type for e in chunk],
                        "schema_version": [e.schema_version for e in chunk],
                        "occurred_at": [e.occurred_at for e in chunk],
                        "payload_json": [
                            json.dumps(e.payload, ensure_ascii=False, sort_keys=True) for e in chunk
                        ],
                    },
                    schema=schema,
                )
                rel_path = f"event_date={day}/events-{uuid.uuid4().hex[:12]}.parquet"
                abs_path = self._root / rel_path
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                pq.write_table(table, abs_path, compression="zstd", compression_level=self._level)
                timestamps = [e.occurred_at for e in chunk]
                entry = LakeFile(
                    path=rel_path,
                    rows=table.num_rows,
                    bytes=abs_path.stat().st_size,
                    min_occurred_at=min(timestamps),
                    max_occurred_at=max(timestamps),
                    written_at=now,
                )
                manifest["files"].append(entry.to_dict())
                written.append(entry)

        self._write_manifest(manifest)
        return written

    # -- manifest ---------------------------------------------------------

    def _read_manifest(self) -> dict[str, Any]:
        path = self.manifest_path()
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return {
            "lake_version": LAKE_VERSION,
            "files": list(data.get("files", [])),
        }

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.manifest_path())

    def read_manifest(self) -> list[LakeFile]:
        """Parsed manifest entries (empty when the lake has no data yet)."""
        return [LakeFile.from_dict(f) for f in self._read_manifest()["files"]]

    def verify(self) -> int:
        """Check manifest integrity; returns total rows verified.

        Every manifest entry must exist on disk with the recorded size and
        row count; every Parquet file on disk must be registered. Raises
        :class:`LakeError` on the first mismatch.
        """
        import pyarrow.parquet as pq

        entries = self.read_manifest()
        registered = {entry.path for entry in entries}
        total = 0
        for entry in entries:
            abs_path = self._root / entry.path
            if not abs_path.exists():
                raise LakeError(f"manifest entry missing on disk: {entry.path}")
            actual_bytes = abs_path.stat().st_size
            if actual_bytes != entry.bytes:
                raise LakeError(
                    f"{entry.path}: size mismatch (manifest {entry.bytes}, disk {actual_bytes})"
                )
            table = pq.read_table(abs_path)
            if table.num_rows != entry.rows:
                raise LakeError(
                    f"{entry.path}: row mismatch (manifest {entry.rows}, disk {table.num_rows})"
                )
            total += table.num_rows
        on_disk = {p.relative_to(self._root).as_posix() for p in self._root.rglob("*.parquet")}
        unregistered = on_disk - registered
        if unregistered:
            raise LakeError(f"unregistered parquet files: {sorted(unregistered)[:5]}")
        return total


def lake_query(sql: str, root: Path | None = None) -> list[dict[str, Any]]:
    """Run one DuckDB SQL query over the whole lake; returns rows as dicts.

    The SQL sees a single relation ``events`` covering every registered
    Parquet file, with Hive partition columns materialized (``event_date``).
    DuckDB is an optional dependency; a clear error is raised when missing.
    """
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - depends on env
        raise LakeError(
            "duckdb is required for lake queries; install it with the lake extras"
        ) from exc

    writer = LakeWriter(root) if root is not None else LakeWriter(default_lake_root())
    files = [entry.path for entry in writer.read_manifest()]
    if not files:
        return []
    glob = (writer.root / "**" / "*.parquet").as_posix().replace("'", "''")
    con = duckdb.connect()
    con.execute(
        f"CREATE VIEW events AS SELECT * FROM read_parquet('{glob}', "
        f"hive_partitioning=true, union_by_name=true)"
    )
    result = con.execute(sql)
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
