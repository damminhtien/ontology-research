"""Typed reference lane for external reference data (architecture §4.6).

Wikidata/Wikipedia are *reference data* — multi-version, frequently changing —
never operational facts. The lane stores versioned, typed Parquet snapshots
under ``<root>/wikidata/{class_qid}/`` (default root ``data/reference``,
overridable via ``FOUNDRY_REFERENCE_ROOT``):

    wikidata/{class_qid}/snap-{ts}-{id}/part-00000.parquet
    wikidata/{class_qid}/manifest.json

Invariants:

- **Manifest-authoritative**: readers only ever touch files registered in the
  manifest, so a half-written or orphaned snapshot is invisible to queries
  (same discipline as the event lake, ADR-0002).
- **Atomic publication**: a snapshot becomes visible only when its manifest
  entry lands; the manifest itself is written to a temp file and renamed.
- **Never a source of truth**: the lane can be rebuilt independently from the
  upstream endpoint; ingestion reads it as one source among others, and facts
  still pass identity + SHACL gates into the event log.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from foundry.versioning import contract_version

REFERENCE_SCHEMA_VERSION = contract_version("reference_lane")
MANIFEST_NAME = "manifest.json"
PART_NAME = "part-00000.parquet"


class ReferenceError(RuntimeError):
    """Raised when the reference lane is missing, corrupt or inconsistent."""


@dataclass(frozen=True)
class ReferenceRecord:
    """One typed lane row: a normalized external reference entity.

    Attributes:
        qid: External identifier (e.g. Wikidata ``Q43229``).
        name_vi: Vietnamese label (may be empty).
        name_en: English label (may be empty).
        type_qids: External class ids the row declared.
        class_qid: The queried root class this row was fetched under.
        fetched_at: ISO-8601 UTC instant of the snapshot fetch.
    """

    qid: str
    name_vi: str
    name_en: str
    type_qids: tuple[str, ...]
    class_qid: str
    fetched_at: str


@dataclass(frozen=True)
class SnapshotInfo:
    """Manifest entry of one published snapshot."""

    id: str
    created_at: str
    path: str  # relative to the lane root
    rows: int


def default_reference_root() -> Path:
    """Resolve the lane root: env override, else ``data/reference``."""
    env = os.environ.get("FOUNDRY_REFERENCE_ROOT")
    if env:
        return Path(env)
    return Path("data") / "reference"


def _class_dir(root: Path, class_qid: str) -> Path:
    return root / "wikidata" / class_qid


def snapshot_schema():
    """Arrow schema of a lane snapshot (lazy import: pyarrow is optional)."""
    import pyarrow as pa

    return pa.schema(
        [
            ("qid", pa.string()),
            ("name_vi", pa.string()),
            ("name_en", pa.string()),
            ("type_qids", pa.list_(pa.string())),
            ("class_qid", pa.string()),
            ("fetched_at", pa.string()),
        ]
    )


def _snapshot_id(created_at: datetime) -> str:
    """Collision-free snapshot id even for two writes in the same second."""
    return f"snap-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def write_snapshot(
    records: Iterable[ReferenceRecord], *, root: Path, class_qid: str
) -> SnapshotInfo:
    """Write one typed snapshot and publish it via the manifest.

    The parquet file lands first; the manifest entry (written to a temp file
    and renamed) is the atomic publication point. Returns the entry.

    Raises:
        ReferenceError: If no usable rows were given (an empty snapshot would
            be indistinguishable from a broken fetch).
    """
    rows = [r for r in records if isinstance(r, ReferenceRecord)]
    if not rows:
        raise ReferenceError("refusing to write an empty snapshot (fetch returned nothing)")

    created_at = datetime.now(UTC)
    snap_id = _snapshot_id(created_at)
    snap_dir = _class_dir(root, class_qid) / snap_id
    snap_dir.mkdir(parents=True, exist_ok=True)
    rel_path = f"wikidata/{class_qid}/{snap_id}/{PART_NAME}"

    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pydict(
        {
            "qid": [r.qid for r in rows],
            "name_vi": [r.name_vi for r in rows],
            "name_en": [r.name_en for r in rows],
            "type_qids": [list(r.type_qids) for r in rows],
            "class_qid": [r.class_qid for r in rows],
            "fetched_at": [r.fetched_at for r in rows],
        },
        schema=snapshot_schema(),
    )
    pq.write_table(table, root / rel_path, compression="zstd")

    info = SnapshotInfo(
        id=snap_id, created_at=created_at.isoformat(), path=rel_path, rows=len(rows)
    )
    manifest_path = _class_dir(root, class_qid) / MANIFEST_NAME
    existing = read_manifest(root, class_qid) if manifest_path.exists() else None
    snapshots: list[dict[str, object]] = (
        [dict(s) for s in existing["snapshots"]] if existing else []
    )
    snapshots.append(
        {"id": info.id, "created_at": info.created_at, "path": info.path, "rows": info.rows}
    )
    _write_manifest(root, class_qid, snapshots)
    return info


def read_manifest(root: Path, class_qid: str) -> dict[str, object]:
    """Load and validate the manifest for one class.

    Raises:
        ReferenceError: On a missing or malformed manifest.
    """
    path = _class_dir(root, class_qid) / MANIFEST_NAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReferenceError(f"no reference manifest at {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReferenceError(f"corrupt reference manifest at {path}: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("snapshots"), list):
        raise ReferenceError(f"malformed reference manifest at {path}")
    return manifest


def _write_manifest(root: Path, class_qid: str, snapshots: list[dict[str, object]]) -> None:
    """Atomically publish the manifest (tmp file + rename)."""
    class_dir = _class_dir(root, class_qid)
    class_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "updated_at": datetime.now(UTC).isoformat(),
        "snapshots": snapshots,
        "latest": snapshots[-1]["id"] if snapshots else None,
    }
    tmp = class_dir / (MANIFEST_NAME + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(class_dir / MANIFEST_NAME)


def latest_snapshot_id(root: Path, class_qid: str) -> str | None:
    """Id of the newest published snapshot, or ``None`` when the lane is empty."""
    manifest = read_manifest(root, class_qid)
    latest = manifest.get("latest")
    return latest if isinstance(latest, str) else None


def read_snapshot(
    root: Path, class_qid: str, snapshot_id: str | None = None
) -> list[ReferenceRecord]:
    """Read one snapshot's rows (latest when ``snapshot_id`` is ``None``).

    Manifest-authoritative: only the registered parquet file is read; orphaned
    files in the snapshot directory are invisible.

    Raises:
        ReferenceError: On an unknown snapshot id or an unreadable file.
    """
    manifest = read_manifest(root, class_qid)
    wanted = snapshot_id or manifest.get("latest")
    entry = next(
        (s for s in manifest["snapshots"] if isinstance(s, dict) and s.get("id") == wanted), None
    )
    if entry is None:
        raise ReferenceError(f"snapshot {wanted!r} is not registered for class {class_qid}")

    import pyarrow.parquet as pq

    path = root / str(entry["path"])
    try:
        table = pq.read_table(path)
    except FileNotFoundError as exc:
        raise ReferenceError(f"snapshot file missing: {path}") from exc

    records: list[ReferenceRecord] = []
    columns = table.to_pydict()
    for i in range(table.num_rows):
        records.append(
            ReferenceRecord(
                qid=columns["qid"][i],
                name_vi=columns["name_vi"][i],
                name_en=columns["name_en"][i],
                type_qids=tuple(columns["type_qids"][i] or ()),
                class_qid=columns["class_qid"][i],
                fetched_at=columns["fetched_at"][i],
            )
        )
    return records
