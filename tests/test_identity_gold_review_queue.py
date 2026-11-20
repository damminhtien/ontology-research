"""Tests for source-pinned, unadjudicated identity review candidates."""

from __future__ import annotations

import json
from pathlib import Path

from tools.build_identity_gold_review_queue import (
    _assertion_candidates,
    _cross_source_vessel_candidates,
    _load_vessel_pair_candidates,
)


def test_single_consolidated_source_does_not_create_cross_source_positive_pairs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "vessels.csv"
    source.write_text(
        "imo,ship_name,public_source_ship_name,public_source_mmsi,mmsi,call_sign,"
        "first_observation,last_observation,sources\n"
        "1000019,LADY K II,LADY K II,235095435,235095435,3FOV,2020,2025,local|public\n"
        "1000021,MONTKAJ,,,,,2020,2025,local\n"
        "1000031,MONTKAJ,,,,,2021,2025,local\n"
        "1000041,TEST NAME,TEST NAME II,,,,2021,2025,local|public\n",
        encoding="utf-8",
    )

    tasks = _load_vessel_pair_candidates(source, source_hash="a" * 64, per_label=10, seed=9)
    negative = [
        task for task in tasks if task["candidate_basis"] == "same_normalized_name_distinct_imo"
    ]

    assert len(tasks) == 1
    assert len(negative) == 1
    assert negative[0]["left"]["name"] == negative[0]["right"]["name"] == "MONTKAJ"
    assert negative[0]["left"]["identifiers"]["imo"] != negative[0]["right"]["identifiers"]["imo"]
    assert all(task["adjudication"]["status"] == "pending" for task in tasks)
    assert negative[0]["left"]["source_record_key"] != negative[0]["right"]["source_record_key"]
    assert all(task["left"]["source_id"] == task["right"]["source_id"] for task in tasks)


def test_openwaters_generates_cross_source_identity_candidates_with_lineage(tmp_path: Path) -> None:
    vessel_csv = tmp_path / "vessels.csv"
    vessel_csv.write_text(
        "imo,ship_name,mmsi,first_observation,last_observation,sources\n"
        "1000019,LADY K II,235095435,2020,2025,registry-a\n"
        "1000021,MONTKAJ,235095436,2020,2025,registry-a\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "openwaters"
    run_dir.mkdir()
    source_url = "https://ais.openwaters.io/v1/vessels?bbox=5,98,15,108"
    records = [
        {
            "source_id": "openwaters-vessels-vn-scs",
            "source_record_key": "urn:source:openwaters:imo",
            "record_id": "235095435:2026-11-18:ShipStaticData",
            "record_sha256": "c" * 64,
            "source_uri": source_url,
            "raw_record": {
                "bbox_tile": "5,98,15,108",
                "properties": {
                    "mmsi": 235095435,
                    "imo": 1000019,
                    "name": "LADY K II",
                    "seen": "2026-11-18T10:00:00Z",
                    "msg_type": "ShipStaticData",
                },
            },
        },
        {
            "source_id": "openwaters-vessels-vn-scs",
            "source_record_key": "urn:source:openwaters:mmsi",
            "record_id": "235095436:2026-11-18:ShipStaticData",
            "record_sha256": "d" * 64,
            "source_uri": source_url,
            "raw_record": {
                "bbox_tile": "5,98,15,108",
                "properties": {
                    "mmsi": 235095436,
                    "name": "MONTKAJ",
                    "seen": "2026-11-18T10:01:00Z",
                    "msg_type": "ShipStaticData",
                },
            },
        },
    ]
    record_path = run_dir / "records.jsonl"
    record_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "fixture",
                "record_file": "records.jsonl",
                "record_file_sha256": "b" * 64,
                "source_results": [
                    {
                        "source_id": "openwaters-vessels-vn-scs",
                        "status": "complete",
                        "record_count": len(records),
                        "raw_snapshots": [{"url": source_url, "sha256": "a" * 64}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    tasks, metadata = _cross_source_vessel_candidates(
        vessel_csv,
        vessel_hash="e" * 64,
        openwaters_run_dir=run_dir,
        per_label=10,
        seed=7,
    )

    assert len(tasks) == 2
    assert metadata["status"] == "complete"
    imo_pair = next(task for task in tasks if "shared_imo" in task["candidate_basis"])
    mmsi_pair = next(task for task in tasks if "shared_mmsi" in task["candidate_basis"])
    assert imo_pair["left"]["source_id"] == "hchulkim-vessels"
    assert imo_pair["right"]["source_id"] == "openwaters-vessels-vn-scs"
    assert imo_pair["right"]["evidence"]["source_sha256"] == "a" * 64
    assert mmsi_pair["candidate_basis"] == "shared_mmsi_candidate_without_unique_shared_imo"
    assert all(task["adjudication"]["status"] == "pending" for task in tasks)


def test_assertion_review_keeps_observation_window_out_of_valid_time(tmp_path: Path) -> None:
    source = tmp_path / "vessels.csv"
    source.write_text(
        "imo,ship_name,flag,ship_type,year_built,sources,first_observation,last_observation,"
        "conflict_latest_ship_name\n"
        "1000019,LADY K II,VN,Cargo,2001,local,2020-01-01,2025-01-01,FALSE\n"
        "1000021,MONTKAJ,SG,Tanker,1999,public,2021-01-01,2025-01-01,TRUE\n",
        encoding="utf-8",
    )

    tasks = _assertion_candidates(source, source_hash="b" * 64, count=20, seed=3)

    assert len({task["assertion"]["predicate_iri"] for task in tasks}) > 1
    assert all(task["assertion"]["valid_from"] is None for task in tasks)
    assert all(task["adjudication"]["valid_time_label"] is None for task in tasks)
    assert all(task["assertion"]["source_observation_window"]["from"] for task in tasks)
