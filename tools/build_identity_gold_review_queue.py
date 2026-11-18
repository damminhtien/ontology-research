#!/usr/bin/env python3
"""Build source-pinned identity and assertion review queues outside Git."""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.events import EventLog  # noqa: E402

VESSEL_SOURCE_URL = (
    "https://raw.githubusercontent.com/hchulkim/vessels/"
    "ac2349aacccdcada3db1b95ff9ae86aaf3800ad2/data/vessels.csv"
)
DEFAULT_VESSEL_CSV = Path(
    "/Users/macbook/Data/ontology-research/evaluations/vessel-scale-v1/sources/"
    "hchulkim-vessels/ac2349aacccdcada3db1b95ff9ae86aaf3800ad2/vessels.csv"
)
DEFAULT_ORG_LOG_ROOT = Path("/Users/macbook/Data/ontology-research/data/crawl-runs")
DEFAULT_OUTPUT = Path(
    "/Users/macbook/Data/ontology-research/evaluations/identity-gold-v1/review-queue"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_id(*values: object) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _review_state() -> dict[str, Any]:
    return {
        "status": "pending",
        "identity_label": None,
        "assertion_label": None,
        "evidence_label": None,
        "valid_time_label": None,
        "reviewer_id": None,
        "reviewer_type": None,
        "rationale": None,
        "evidence_refs": [],
    }


def _vessel_evidence(
    *, source_hash: str, row_number: int, columns: list[str], row: dict[str, str]
) -> dict[str, Any]:
    return {
        "source_uri": VESSEL_SOURCE_URL,
        "source_sha256": source_hash,
        "locator": f"csv-row:{row_number}",
        "row_number": row_number,
        "columns": columns,
        "cell_values": {name: row.get(name) for name in columns},
        "scope": "pinned consolidated crosswalk row; not the upstream registry document",
    }


def _vessel_pair(
    *,
    source_hash: str,
    left: dict[str, str],
    left_row: int,
    right: dict[str, str],
    right_row: int,
    candidate_basis: str,
    columns: list[str],
) -> dict[str, Any]:
    left_imo = (left.get("imo") or "").strip()
    right_imo = (right.get("imo") or "").strip()
    pair_key = _stable_id("vessel", left_imo, right_imo, left_row, right_row, candidate_basis)
    return {
        "task_id": pair_key,
        "task_type": "identity_pair",
        "entity_type": "Vessel",
        "candidate_basis": candidate_basis,
        "left": {
            "source_record_key": f"{source_hash}:{left_row}:crosswalk",
            "source_id": "hchulkim-vessels",
            "name": left.get("ship_name"),
            "identifiers": {"imo": left_imo, "mmsi": left.get("mmsi")},
            "source_observation_window": {
                "from": left.get("first_observation"),
                "until": left.get("last_observation"),
            },
            "evidence": _vessel_evidence(
                source_hash=source_hash,
                row_number=left_row,
                columns=columns,
                row=left,
            ),
        },
        "right": {
            "source_record_key": f"{source_hash}:{right_row}:crosswalk",
            "source_id": "hchulkim-vessels",
            "name": right.get("ship_name"),
            "identifiers": {"imo": right_imo, "mmsi": right.get("mmsi")},
            "source_observation_window": {
                "from": right.get("first_observation"),
                "until": right.get("last_observation"),
            },
            "evidence": _vessel_evidence(
                source_hash=source_hash,
                row_number=right_row,
                columns=columns,
                row=right,
            ),
        },
        "adjudication": _review_state(),
    }


def _load_vessel_pair_candidates(
    path: Path, *, source_hash: str, per_label: int, seed: int
) -> list[dict[str, Any]]:
    by_name: dict[str, list[tuple[int, dict[str, str]]]] = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        for row_number, row in enumerate(reader, start=2):
            cleaned = {key: value or "" for key, value in row.items() if key is not None}
            name = _normalize(cleaned.get("ship_name", ""))
            if name and cleaned.get("imo"):
                by_name[name].append((row_number, cleaned))

    negative_candidates = []
    for _name, group in sorted(by_name.items()):
        if len(group) < 2:
            continue
        for left_index, (left_row, left) in enumerate(group):
            for right_row, right in group[left_index + 1 :]:
                if left.get("imo") == right.get("imo"):
                    continue
                negative_candidates.append(
                    _vessel_pair(
                        source_hash=source_hash,
                        left=left,
                        left_row=left_row,
                        right=right,
                        right_row=right_row,
                        candidate_basis="same_normalized_name_distinct_imo",
                        columns=[
                            "imo",
                            "ship_name",
                            "mmsi",
                            "call_sign",
                            "first_observation",
                            "last_observation",
                            "sources",
                        ],
                    )
                )

    def sample(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(items, key=lambda item: _stable_id(seed, item["task_id"]))[:per_label]

    return sample(negative_candidates)


def _identifier(value: Any, width: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text.isdigit() or len(text) > width:
        return None
    return text.zfill(width)


def _openwaters_evidence(record: dict[str, Any], *, response_hash: str | None) -> dict[str, Any]:
    raw = record.get("raw_record") or {}
    properties = raw.get("properties") or {}
    return {
        "source_uri": record.get("source_uri"),
        "source_sha256": response_hash,
        "record_sha256": record.get("record_sha256"),
        "locator": f"geojson-feature:{record.get('record_id')}",
        "source_record_key": record.get("source_record_key"),
        "observed_at": properties.get("seen"),
        "bbox_tile": raw.get("bbox_tile"),
        "properties": {
            key: properties.get(key)
            for key in ("mmsi", "imo", "name", "callsign", "flag", "seen", "msg_type")
        },
        "scope": "Open Waters AIS snapshot; current observation, not registry proof.",
    }


def _cross_source_vessel_candidates(
    vessel_path: Path,
    *,
    vessel_hash: str,
    openwaters_run_dir: Path,
    per_label: int,
    seed: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest_path = openwaters_run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Open Waters run has no manifest: {manifest_path}")
    openwaters_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_result = next(
        (
            result
            for result in openwaters_manifest.get("source_results", [])
            if result.get("source_id") == "openwaters-vessels-vn-scs"
        ),
        None,
    )
    if not isinstance(run_result, dict) or run_result.get("status") not in {"complete", "partial"}:
        raise ValueError("Open Waters run has no usable vessel source result")
    source_hash_by_uri = {
        item.get("url"): item.get("sha256")
        for item in run_result.get("raw_snapshots", [])
        if isinstance(item, dict)
    }

    crosswalk_by_imo: dict[str, tuple[int, dict[str, str]]] = {}
    crosswalk_imo_counts: Counter[str] = Counter()
    crosswalk_by_mmsi: dict[str, tuple[int, dict[str, str]]] = {}
    crosswalk_mmsi_counts: Counter[str] = Counter()
    with vessel_path.open(encoding="utf-8-sig", newline="") as source:
        for row_number, row in enumerate(csv.DictReader(source), start=2):
            cleaned = {key: value or "" for key, value in row.items() if key is not None}
            imo = _identifier(cleaned.get("imo"), 7)
            mmsi = _identifier(cleaned.get("mmsi"), 9)
            if imo:
                crosswalk_imo_counts[imo] += 1
                crosswalk_by_imo[imo] = (row_number, cleaned)
            if mmsi:
                crosswalk_mmsi_counts[mmsi] += 1
                crosswalk_by_mmsi[mmsi] = (row_number, cleaned)

    openwaters_records: list[tuple[dict[str, Any], dict[str, Any], str | None, str | None]] = []
    openwaters_imo_mmsis: dict[str, set[str]] = defaultdict(set)
    with (openwaters_run_dir / openwaters_manifest.get("record_file", "records.jsonl")).open(
        encoding="utf-8"
    ) as records_file:
        for line in records_file:
            if not line.strip():
                continue
            record = json.loads(line)
            raw = record.get("raw_record") or {}
            properties = raw.get("properties") or {}
            name = str(properties.get("name") or "").strip()
            imo = _identifier(properties.get("imo"), 7)
            mmsi = _identifier(properties.get("mmsi"), 9)
            if not name or not mmsi or not record.get("source_record_key"):
                continue
            if imo:
                openwaters_imo_mmsis[imo].add(mmsi)
            openwaters_records.append((record, properties, imo, mmsi))

    def choose_latest(
        candidates: list[tuple[dict[str, Any], dict[str, Any], str | None, str | None]],
    ) -> dict[str, Any]:
        return max(
            candidates,
            key=lambda item: (str(item[1].get("seen") or ""), item[0]["source_record_key"]),
        )[0]

    records_by_imo: dict[
        str, list[tuple[dict[str, Any], dict[str, Any], str | None, str | None]]
    ] = defaultdict(list)
    records_by_mmsi: dict[
        str, list[tuple[dict[str, Any], dict[str, Any], str | None, str | None]]
    ] = defaultdict(list)
    for item in openwaters_records:
        _record, _properties, imo, mmsi = item
        if imo:
            records_by_imo[imo].append(item)
        if mmsi:
            records_by_mmsi[mmsi].append(item)

    candidates = []
    candidate_pool_counts: Counter[str] = Counter()
    seen_pair_keys: set[tuple[str, str]] = set()
    for imo in sorted(set(crosswalk_by_imo) & set(records_by_imo)):
        if crosswalk_imo_counts[imo] != 1 or len(openwaters_imo_mmsis[imo]) != 1:
            continue
        crosswalk_row_number, crosswalk_row = crosswalk_by_imo[imo]
        open_record = choose_latest(records_by_imo[imo])
        if not (crosswalk_row.get("ship_name") or "").strip():
            continue
        pair_key = (
            f"{vessel_hash}:{crosswalk_row_number}:crosswalk",
            open_record["source_record_key"],
        )
        seen_pair_keys.add(pair_key)
        open_properties = open_record["raw_record"]["properties"]
        name_agrees = _normalize(crosswalk_row["ship_name"]) == _normalize(
            str(open_properties.get("name") or "")
        )
        task_id = _stable_id("cross-source-vessel", *pair_key)
        candidates.append(
            {
                "task_id": task_id,
                "task_type": "identity_pair",
                "entity_type": "Vessel",
                "candidate_basis": (
                    "shared_imo_unique_in_both_sources_name_agrees"
                    if name_agrees
                    else "shared_imo_unique_in_both_sources_name_differs"
                ),
                "identity_evidence": (
                    "shared IMO is a candidate signal; human adjudication is required"
                ),
                "left": {
                    "source_record_key": pair_key[0],
                    "source_id": "hchulkim-vessels",
                    "name": crosswalk_row.get("ship_name"),
                    "identifiers": {
                        "imo": imo,
                        "mmsi": _identifier(crosswalk_row.get("mmsi"), 9),
                    },
                    "evidence": _vessel_evidence(
                        source_hash=vessel_hash,
                        row_number=crosswalk_row_number,
                        columns=[
                            "imo",
                            "mmsi",
                            "ship_name",
                            "first_observation",
                            "last_observation",
                            "sources",
                        ],
                        row=crosswalk_row,
                    ),
                },
                "right": {
                    "source_record_key": open_record["source_record_key"],
                    "source_id": "openwaters-vessels-vn-scs",
                    "name": open_properties.get("name"),
                    "identifiers": {
                        "imo": imo,
                        "mmsi": _identifier(open_properties.get("mmsi"), 9),
                        "call_sign": open_properties.get("callsign"),
                    },
                    "observed_at": open_properties.get("seen"),
                    "evidence": _openwaters_evidence(
                        open_record,
                        response_hash=source_hash_by_uri.get(open_record.get("source_uri")),
                    ),
                },
                "adjudication": _review_state(),
            }
        )
        candidate_pool_counts[candidates[-1]["candidate_basis"]] += 1

    for mmsi in sorted(set(crosswalk_by_mmsi) & set(records_by_mmsi)):
        if crosswalk_mmsi_counts[mmsi] != 1:
            continue
        crosswalk_row_number, crosswalk_row = crosswalk_by_mmsi[mmsi]
        if not (crosswalk_row.get("ship_name") or "").strip():
            continue
        open_record = choose_latest(records_by_mmsi[mmsi])
        pair_key = (
            f"{vessel_hash}:{crosswalk_row_number}:crosswalk",
            open_record["source_record_key"],
        )
        if pair_key in seen_pair_keys:
            continue
        open_properties = open_record["raw_record"]["properties"]
        task_id = _stable_id("cross-source-vessel-mmsi", *pair_key)
        candidates.append(
            {
                "task_id": task_id,
                "task_type": "identity_pair",
                "entity_type": "Vessel",
                "candidate_basis": "shared_mmsi_candidate_without_unique_shared_imo",
                "identity_evidence": (
                    "shared MMSI is a review candidate; MMSI reuse and stale data are possible"
                ),
                "left": {
                    "source_record_key": pair_key[0],
                    "source_id": "hchulkim-vessels",
                    "name": crosswalk_row.get("ship_name"),
                    "identifiers": {
                        "imo": _identifier(crosswalk_row.get("imo"), 7),
                        "mmsi": mmsi,
                    },
                    "evidence": _vessel_evidence(
                        source_hash=vessel_hash,
                        row_number=crosswalk_row_number,
                        columns=[
                            "imo",
                            "mmsi",
                            "ship_name",
                            "first_observation",
                            "last_observation",
                        ],
                        row=crosswalk_row,
                    ),
                },
                "right": {
                    "source_record_key": open_record["source_record_key"],
                    "source_id": "openwaters-vessels-vn-scs",
                    "name": open_properties.get("name"),
                    "identifiers": {
                        "imo": _identifier(open_properties.get("imo"), 7),
                        "mmsi": mmsi,
                        "call_sign": open_properties.get("callsign"),
                    },
                    "observed_at": open_properties.get("seen"),
                    "evidence": _openwaters_evidence(
                        open_record,
                        response_hash=source_hash_by_uri.get(open_record.get("source_uri")),
                    ),
                },
                "adjudication": _review_state(),
            }
        )
        candidate_pool_counts[candidates[-1]["candidate_basis"]] += 1

    candidates.sort(key=lambda item: _stable_id(seed, item["task_id"]))
    return candidates[:per_label], {
        "run_id": openwaters_manifest.get("run_id"),
        "status": run_result.get("status"),
        "record_count": run_result.get("record_count"),
        "record_file": str(
            openwaters_run_dir / openwaters_manifest.get("record_file", "records.jsonl")
        ),
        "record_file_sha256": openwaters_manifest.get("record_file_sha256"),
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "partial_snapshot": run_result.get("status") == "partial",
        "candidate_pool_counts": dict(candidate_pool_counts),
        "candidate_pool_total": sum(candidate_pool_counts.values()),
    }


def _organization_candidates(log_root: Path, *, per_label: int, seed: int) -> list[dict[str, Any]]:
    entities: dict[str, dict[str, Any]] = {}
    entity_by_id: dict[str, dict[str, Any]] = {}
    matched: dict[str, dict[str, Any]] = {}
    source_logs = sorted(log_root.rglob("events.jsonl"))
    for path in source_logs:
        for event in EventLog(path).iter_all():
            payload = event.payload
            if event.event_type == "EntityCreated" and payload.get("entity_type") == "Organization":
                key = payload.get("source_record_key")
                if key:
                    entities.setdefault(
                        key,
                        {
                            "source_record_key": key,
                            "entity_id": payload.get("entity_id"),
                            "name": payload.get("name"),
                            "aliases": payload.get("name_aliases") or [],
                            "source_id": payload.get("source_id"),
                            "external_ids": payload.get("external_ids") or [],
                            "evidence": {
                                "source_uri": payload.get("source_id"),
                                "event_id": event.event_id,
                                "event_sequence": event.sequence,
                                "event_log": str(path),
                            },
                        },
                    )
                    if payload.get("entity_id"):
                        entity_by_id.setdefault(payload["entity_id"], entities[key])
            elif event.event_type == "SourceRecordMatched":
                key = payload.get("source_record_key")
                if key:
                    matched.setdefault(
                        key,
                        {
                            "source_record_key": key,
                            "entity_id": payload.get("entity_id"),
                            "name": payload.get("name"),
                            "source_id": payload.get("source_id"),
                            "external_ids": payload.get("external_ids") or [],
                            "evidence": {
                                "source_uri": payload.get("source_id"),
                                "event_id": event.event_id,
                                "event_sequence": event.sequence,
                                "event_log": str(path),
                                "match_event_is_candidate_provenance": True,
                            },
                        },
                    )

    tasks = []
    for matched_record in matched.values():
        target = entity_by_id.get(matched_record.get("entity_id"))
        if target is None or target.get("source_id") == matched_record.get("source_id"):
            continue
        tasks.append(
            {
                "task_id": _stable_id(
                    "organization",
                    matched_record["source_record_key"],
                    target["source_record_key"],
                ),
                "task_type": "identity_pair",
                "entity_type": "Organization",
                "candidate_basis": "cross_source_match_event_review_required",
                "left": matched_record,
                "right": target,
                "adjudication": _review_state(),
            }
        )

    records = list(entities.values())
    negative_candidates = []
    for left_index, left in enumerate(records):
        left_name = str(left.get("name") or "")
        left_ids = {
            str(value.get("external_id"))
            for value in left.get("external_ids", [])
            if isinstance(value, dict) and value.get("external_id")
        }
        for right in records[left_index + 1 :]:
            right_name = str(right.get("name") or "")
            right_ids = {
                str(value.get("external_id"))
                for value in right.get("external_ids", [])
                if isinstance(value, dict) and value.get("external_id")
            }
            if left_ids & right_ids or not left_name or not right_name:
                continue
            similarity = difflib.SequenceMatcher(
                a=_normalize(left_name), b=_normalize(right_name)
            ).ratio()
            if similarity < 0.55:
                continue
            negative_candidates.append(
                {
                    "task_id": _stable_id(
                        "organization",
                        left["source_record_key"],
                        right["source_record_key"],
                    ),
                    "task_type": "identity_pair",
                    "entity_type": "Organization",
                    "candidate_basis": "similar_names_distinct_source_record_ids",
                    "name_similarity_for_sampling_only": round(similarity, 4),
                    "left": left,
                    "right": right,
                    "adjudication": _review_state(),
                }
            )
    negative_candidates.sort(
        key=lambda item: (
            -item["name_similarity_for_sampling_only"],
            _stable_id(seed, item["task_id"]),
        )
    )
    tasks.extend(negative_candidates[:per_label])
    return tasks


def _assertion_candidates(
    path: Path, *, source_hash: str, count: int, seed: int
) -> list[dict[str, Any]]:
    fields = (
        ("ship_name", "https://schema.org/name"),
        ("flag", "urn:benchmark:vessel:flag"),
        ("ship_type", "urn:benchmark:vessel:shipType"),
        ("year_built", "urn:benchmark:vessel:yearBuilt"),
    )
    tasks = []
    with path.open(encoding="utf-8-sig", newline="") as source:
        for row_number, row in enumerate(csv.DictReader(source), start=2):
            imo = (row.get("imo") or "").strip()
            if not imo:
                continue
            start = row_number % len(fields)
            field = next(
                (
                    fields[(start + offset) % len(fields)][0]
                    for offset in range(len(fields))
                    if (row.get(fields[(start + offset) % len(fields)][0]) or "").strip()
                ),
                None,
            )
            if field is None:
                continue
            predicate = dict(fields)[field]
            value = (row.get(field) or "").strip()
            evidence_columns = [field, "sources", "first_observation", "last_observation"]
            if field == "ship_name":
                evidence_columns.append("public_source_ship_name")
            conflict_column = f"conflict_latest_{field}"
            if conflict_column in row:
                evidence_columns.append(conflict_column)
            evidence = _vessel_evidence(
                source_hash=source_hash,
                row_number=row_number,
                columns=evidence_columns,
                row=row,
            )
            task_id = _stable_id("vessel-assertion", imo, predicate, value, row_number)
            tasks.append(
                {
                    "task_id": task_id,
                    "task_type": "assertion_evidence_valid_time",
                    "entity_type": "Vessel",
                    "assertion": {
                        "assertion_id": f"urn:review-assertion:{task_id}",
                        "subject_id": f"urn:vessel:imo:{imo}",
                        "predicate_iri": predicate,
                        "object": {"kind": "literal", "value": value},
                        "valid_from": None,
                        "valid_until": None,
                        "recorded_at": None,
                        "source_observation_window": {
                            "from": row.get("first_observation") or None,
                            "until": row.get("last_observation") or None,
                        },
                    },
                    "evidence": evidence,
                    "label_instructions": {
                        "assertion_label": "reported | conflicted | unresolved",
                        "evidence_label": (
                            "primary | secondary | derived | conflicting | insufficient"
                        ),
                        "valid_time_label": "grounded | unknown | conflicting",
                    },
                    "adjudication": _review_state(),
                }
            )
    return sorted(tasks, key=lambda item: _stable_id(seed, item["task_id"]))[:count]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    """Write deterministic pair and assertion review queues."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vessel-csv", type=Path, default=DEFAULT_VESSEL_CSV)
    parser.add_argument("--openwaters-run-dir", type=Path)
    parser.add_argument("--organization-log-root", type=Path, default=DEFAULT_ORG_LOG_ROOT)
    parser.add_argument("--pairs-per-label", type=int, default=100)
    parser.add_argument("--assertion-count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.pairs_per_label < 1 or args.assertion_count < 1:
        parser.error("pair and assertion review counts must be positive")
    if not args.vessel_csv.is_file():
        parser.error(f"vessel source file does not exist: {args.vessel_csv}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_names = (
        "identity-pair-review.jsonl",
        "assertion-evidence-time-review.jsonl",
        "manifest.json",
    )
    existing = [name for name in output_names if (args.output_dir / name).exists()]
    if existing:
        parser.error(f"refusing to overwrite existing review artifacts: {existing}")
    vessel_hash = _sha256(args.vessel_csv)
    vessel_pairs = _load_vessel_pair_candidates(
        args.vessel_csv,
        source_hash=vessel_hash,
        per_label=args.pairs_per_label,
        seed=args.seed,
    )
    openwaters_pairs = []
    openwaters_metadata = None
    if args.openwaters_run_dir is not None:
        openwaters_pairs, openwaters_metadata = _cross_source_vessel_candidates(
            args.vessel_csv,
            vessel_hash=vessel_hash,
            openwaters_run_dir=args.openwaters_run_dir,
            per_label=args.pairs_per_label,
            seed=args.seed,
        )
    vessel_pairs = openwaters_pairs + vessel_pairs
    organization_pairs = _organization_candidates(
        args.organization_log_root,
        per_label=args.pairs_per_label,
        seed=args.seed,
    )
    assertions = _assertion_candidates(
        args.vessel_csv,
        source_hash=vessel_hash,
        count=args.assertion_count,
        seed=args.seed,
    )
    pair_path = args.output_dir / "identity-pair-review.jsonl"
    assertion_path = args.output_dir / "assertion-evidence-time-review.jsonl"
    _write_jsonl(pair_path, vessel_pairs + organization_pairs)
    _write_jsonl(assertion_path, assertions)
    manifest = {
        "dataset_id": "identity-gold-review-queue-v1",
        "schema_version": 1,
        "seed": args.seed,
        "source_files": [
            {
                "path": str(args.vessel_csv),
                "sha256": vessel_hash,
                "source_url": VESSEL_SOURCE_URL,
            },
            *(
                [
                    {
                        "path": openwaters_metadata["record_file"],
                        "sha256": openwaters_metadata["record_file_sha256"],
                        "manifest_path": openwaters_metadata["manifest_path"],
                        "manifest_sha256": openwaters_metadata["manifest_sha256"],
                        "source_url": "https://openwaters.io/api/ais/",
                    }
                ]
                if openwaters_metadata
                else []
            ),
            *[
                {"path": str(path), "sha256": _sha256(path)}
                for path in sorted(args.organization_log_root.rglob("events.jsonl"))
            ],
        ],
        "counts": {
            "vessel_pair_candidates": len(vessel_pairs),
            "cross_source_vessel_pair_candidates": len(openwaters_pairs),
            "crosswalk_negative_vessel_pair_candidates": len(vessel_pairs) - len(openwaters_pairs),
            "organization_pair_candidates": len(organization_pairs),
            "assertion_evidence_time_candidates": len(assertions),
            "all_candidates_unadjudicated_before_review": True,
        },
        "source_snapshots": {"openwaters_vessels": openwaters_metadata},
        "files": {
            pair_path.name: _sha256(pair_path),
            assertion_path.name: _sha256(assertion_path),
        },
        "limitations": [
            (
                "The vessel CSV is a consolidated crosswalk and its rows are not raw registry "
                "documents."
            ),
            "Source observation windows do not establish assertion valid-time intervals.",
            "Codex model review is recorded separately from human confirmation.",
            (
                "Shared IMO or MMSI only generates a review candidate; "
                "it never sets the identity label."
            ),
        ],
    }
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({"manifest": str(manifest_path), **manifest["counts"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
