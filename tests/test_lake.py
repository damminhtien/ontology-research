"""Tests for the Parquet+zstd lake archive (foundry.lake)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from foundry.events import SCHEMA_VERSION, SemanticEvent
from foundry.lake import LakeError, LakeWriter, lake_query, parse_occurred_at, persist_events


def _events(n: int, day: str = "2026-01-15") -> list[SemanticEvent]:
    events = []
    for i in range(n):
        etype = "EntityCreated" if i % 2 == 0 else "LocationObserved"
        events.append(
            SemanticEvent(
                event_id=f"test-{day}-{i:04d}",
                event_type=etype,
                schema_version=SCHEMA_VERSION,
                occurred_at=f"{day}T0{i % 10}:00:00Z",
                payload={"entity_id": f"e{i}", "seq": i},
            )
        )
    return events


@pytest.fixture()
def lake_root(tmp_path: Path) -> Path:
    return tmp_path / "lake"


class TestWriter:
    def test_flush_creates_partition_and_manifest(self, lake_root: Path) -> None:
        w = LakeWriter(lake_root)
        w.write_events(_events(3))
        written = w.flush()
        assert len(written) == 1
        assert written[0].path.startswith("event_date=")
        assert written[0].rows == 3
        assert (lake_root / written[0].path).exists()
        assert (lake_root / "manifest.json").exists()

    def test_rotation_splits_files(self, lake_root: Path) -> None:
        w = LakeWriter(lake_root, max_rows_per_file=2)
        w.write_events(_events(5))
        written = w.flush()
        assert [f.rows for f in written] == [2, 2, 1]
        assert w.verify() == 5

    def test_partitions_split_by_day(self, lake_root: Path) -> None:
        w = LakeWriter(lake_root)
        w.write_events(_events(2, day="2026-01-15"))
        w.write_events(_events(2, day="2026-02-20"))
        written = w.flush()
        assert sorted(f.path.split("/")[0] for f in written) == [
            "event_date=2026-01-15",
            "event_date=2026-02-20",
        ]

    def test_empty_flush_is_noop(self, lake_root: Path) -> None:
        w = LakeWriter(lake_root)
        assert w.flush() == []
        assert not (lake_root / "manifest.json").exists()

    def test_flush_clears_buffer(self, lake_root: Path) -> None:
        w = LakeWriter(lake_root)
        w.write_events(_events(2))
        w.flush()
        assert w.flush() == []

    def test_rejects_unknown_event_type(self, lake_root: Path) -> None:
        w = LakeWriter(lake_root)
        bad = SemanticEvent("x", "Bogus", 1, "2026-01-15T00:00:00Z", {})
        with pytest.raises(LakeError, match="unknown event type"):
            w.write_events([bad])

    def test_invalid_constructor_args(self, lake_root: Path) -> None:
        with pytest.raises(ValueError, match="max_rows_per_file"):
            LakeWriter(lake_root, max_rows_per_file=0)
        with pytest.raises(ValueError, match="compression_level"):
            LakeWriter(lake_root, compression_level=99)

    def test_zstd_compression_lands_on_disk(self, lake_root: Path) -> None:
        w = LakeWriter(lake_root)
        w.write_events(_events(3))
        written = w.flush()
        meta = pq.ParquetFile(lake_root / written[0].path).metadata
        assert meta.row_group(0).column(0).compression.upper() == "ZSTD"


class TestVerify:
    def test_verify_detects_missing_file(self, lake_root: Path) -> None:
        w = LakeWriter(lake_root)
        w.write_events(_events(3))
        w.flush()
        (lake_root / w.read_manifest()[0].path).unlink()
        with pytest.raises(LakeError, match="missing on disk"):
            w.verify()

    def test_verify_detects_tampered_size(self, lake_root: Path) -> None:
        w = LakeWriter(lake_root)
        w.write_events(_events(3))
        w.flush()
        target = lake_root / w.read_manifest()[0].path
        target.write_bytes(target.read_bytes() + b"x")
        with pytest.raises(LakeError, match="size mismatch"):
            w.verify()

    def test_verify_detects_unregistered_file(self, lake_root: Path) -> None:
        w = LakeWriter(lake_root)
        w.write_events(_events(3))
        w.flush()
        rogue_dir = lake_root / "event_date=1999-01-01"
        rogue_dir.mkdir()
        pq.write_table(pa.table({"a": [1]}), rogue_dir / "rogue.parquet")
        with pytest.raises(LakeError, match="unregistered"):
            w.verify()


class TestQuery:
    def test_roundtrip_and_partition_pruning(self, lake_root: Path) -> None:
        w = LakeWriter(lake_root)
        w.write_events(_events(4, day="2026-03-01"))
        w.write_events(_events(2, day="2026-03-02"))
        w.flush()

        rows = lake_query(
            "SELECT event_date, count(*) AS n FROM events GROUP BY 1 ORDER BY 1",
            root=lake_root,
        )
        assert [(r["event_date"], r["n"]) for r in rows] == [
            (date(2026, 3, 1), 4),
            (date(2026, 3, 2), 2),
        ]

    def test_payload_roundtrip(self, lake_root: Path) -> None:
        w = LakeWriter(lake_root)
        events = _events(2)
        w.write_events(events)
        w.flush()
        rows = lake_query(
            "SELECT event_id, payload_json FROM events ORDER BY event_id",
            root=lake_root,
        )
        by_id = {r["event_id"]: r["payload_json"] for r in rows}
        for event in events:
            assert json.loads(by_id[event.event_id]) == event.payload

    def test_partition_pruning_counts(self, lake_root: Path) -> None:
        w = LakeWriter(lake_root)
        w.write_events(_events(3, day="2026-05-01"))
        w.flush()
        rows = lake_query(
            "SELECT count(*) AS n FROM events WHERE event_date = DATE '2026-05-01'",
            root=lake_root,
        )
        assert rows[0]["n"] == 3

    def test_empty_lake_returns_no_rows(self, lake_root: Path) -> None:
        assert lake_query("SELECT 1 AS x FROM events", root=lake_root) == []


class TestDefaults:
    def test_parse_occurred_at(self) -> None:
        dt = parse_occurred_at("2026-01-15T10:30:00Z")
        assert dt == datetime(2026, 1, 15, 10, 30, tzinfo=UTC)

    def test_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from foundry import lake

        env_root = tmp_path / "env-lake"
        monkeypatch.setenv("FOUNDRY_LAKE_ROOT", str(env_root))
        assert lake.default_lake_root() == env_root


class TestPersistEvents:
    def test_persist_events_writes_files_and_manifest(self, tmp_path: Path) -> None:
        """Regression: callers must not be able to drop events by forgetting flush()."""
        files = persist_events(_events(5), tmp_path)
        assert len(files) == 1
        assert sum(f.rows for f in files) == 5
        writer = LakeWriter(tmp_path)
        assert writer.manifest_path().exists()
        assert writer.verify() == 5

    def test_persist_events_second_batch_appends(self, tmp_path: Path) -> None:
        persist_events(_events(3), tmp_path)
        files = persist_events(_events(2, day="2026-01-16"), tmp_path)
        assert [f.rows for f in files] == [2]
        assert LakeWriter(tmp_path).verify() == 5

    def test_persist_events_empty_batch_is_noop(self, tmp_path: Path) -> None:
        assert persist_events([], tmp_path) == []
        assert LakeWriter(tmp_path).verify() == 0
