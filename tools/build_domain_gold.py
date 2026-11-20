#!/usr/bin/env python3
"""Extract a privacy-preserving OSINT organization identity gold pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())

from foundry.events import EventLog  # noqa: E402
from foundry.identity import (  # noqa: E402
    DEFAULT_IDENTIFIER_POLICIES,
    IdentityService,
    normalize_name,
)

DATASET_ID = "osint-organization-crosswalk-pilot-v1"


def _sha256_bytes(value: bytes) -> str:
    """Return the SHA-256 hex digest of a byte string."""
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    """Hash a source event log incrementally."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identifier_set(values: object) -> set[tuple[str, str]]:
    """Read identifiers whose namespaces have authoritative uniqueness policy."""
    if not isinstance(values, list):
        return set()
    identifiers = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        namespace = str(value.get("source") or value.get("namespace") or "").casefold()
        identifier = str(value.get("external_id") or value.get("value") or "").strip()
        policy = DEFAULT_IDENTIFIER_POLICIES.get(namespace)
        if identifier and policy is not None and policy.unique_authoritative:
            identifiers.add((namespace, identifier.casefold()))
    return identifiers


def build_gold_pilot(event_log_path: Path) -> dict[str, Any]:
    """Derive labeled record pairs from shared authoritative identifiers."""
    entities: dict[str, dict[str, Any]] = {}
    entity_identifiers: dict[str, set[tuple[str, str]]] = defaultdict(set)
    incoming_records: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    last_sequence = 0

    for event in EventLog(event_log_path).iter_all():
        last_sequence = max(last_sequence, event.sequence or 0)
        counts[event.event_type] += 1
        payload = event.payload
        if event.event_type == "EntityCreated":
            entity_id = payload.get("entity_id")
            if entity_id:
                entities[entity_id] = payload
                entity_identifiers[entity_id].update(_identifier_set(payload.get("external_ids")))
        elif event.event_type == "ExternalIdBound":
            entity_id = payload.get("entity_id")
            namespace = str(payload.get("source") or "").casefold()
            identifier = str(payload.get("external_id") or "").strip()
            policy = DEFAULT_IDENTIFIER_POLICIES.get(namespace)
            if entity_id and identifier and policy and policy.unique_authoritative:
                entity_identifiers[entity_id].add((namespace, identifier.casefold()))
        elif event.event_type == "SourceRecordMatched":
            incoming_records.append(payload)

    name_only_resolver = IdentityService()
    for entity_id, target in entities.items():
        aliases = [
            str(value)
            for value in target.get("name_aliases", [])
            if isinstance(value, str) and value
        ]
        name = target.get("name")
        if isinstance(name, str) and name:
            aliases.insert(0, name)
        name_only_resolver.register(
            entity_id=entity_id,
            entity_type=str(target.get("entity_type") or "Unknown"),
            aliases=aliases,
        )

    gold_pairs = []
    for incoming in incoming_records:
        incoming_ids = _identifier_set(incoming.get("external_ids"))
        if not incoming_ids:
            continue
        source_record_key = str(incoming.get("source_record_key") or "")
        if not source_record_key:
            continue
        incoming_name = str(incoming.get("name") or "")
        entity_type = str(incoming.get("entity_type") or "Organization")
        name_only_result = name_only_resolver.resolve(
            name=incoming_name,
            entity_type=entity_type,
            prepare_only=True,
        )
        for entity_id, target in entities.items():
            shared_ids = incoming_ids & entity_identifiers[entity_id]
            if not shared_ids:
                continue
            target_record_key = str(target.get("source_record_key") or "")
            if not target_record_key:
                continue
            left_fingerprint = _sha256_bytes(source_record_key.encode("utf-8"))
            right_fingerprint = _sha256_bytes(target_record_key.encode("utf-8"))
            pair_fingerprint = _sha256_bytes(
                "\0".join(sorted((left_fingerprint, right_fingerprint))).encode("ascii")
            )
            normalized_incoming_name = normalize_name(incoming_name)
            target_names = [str(target.get("name") or ""), *target.get("name_aliases", [])]
            exact_name_candidate = bool(normalized_incoming_name) and normalized_incoming_name in {
                normalize_name(name) for name in target_names if name
            }
            namespaces = sorted({namespace for namespace, _value in shared_ids})
            gold_pairs.append(
                {
                    "pair_fingerprint_sha256": pair_fingerprint,
                    "left_record_fingerprint_sha256": left_fingerprint,
                    "right_record_fingerprint_sha256": right_fingerprint,
                    "label": "same_entity",
                    "label_basis": "shared_authoritative_identifier",
                    "identifier_namespaces": namespaces,
                    "exact_name_or_alias_candidate": exact_name_candidate,
                    "name_only_resolver_decision": name_only_result.decision.value,
                    "name_only_resolver_candidate_count": len(name_only_result.candidates),
                    "name_only_resolver_candidate_at_5": entity_id
                    in name_only_result.candidates[:5],
                }
            )

    # Keep only distinct pairs if the log repeats a source match event.
    unique_pairs = {pair["pair_fingerprint_sha256"]: pair for pair in gold_pairs}
    pairs = [unique_pairs[key] for key in sorted(unique_pairs)]
    return {
        "dataset_id": DATASET_ID,
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source": {
            "event_log": "$FOUNDRY_DATA_ROOT/data/crawl-runs/osint-mvp/"
            "osint-mvp-20261118-live/events.jsonl",
            "sha256": _file_sha256(event_log_path),
            "last_sequence": last_sequence,
            "event_count": sum(counts.values()),
            "event_types": dict(sorted(counts.items())),
        },
        "label_policy": {
            "rule": "same authoritative unique identifier in source record and entity record",
            "human_adjudicated": False,
            "raw_names_and_identifiers_included": False,
        },
        "pair_counts": {
            "same_entity": len(pairs),
            "different_entity": 0,
            "unlabeled": None,
        },
        "pairs": pairs,
        "evaluation_limits": [
            "This is a one-positive-pair pilot and does not estimate pairwise precision or recall.",
            "No negative pairs were labeled, so false-merge rate is not estimable.",
            (
                "The authoritative-identifier label is suitable for a crosswalk check, "
                "not a blind benchmark."
            ),
            (
                "A larger human-adjudicated vessel and organization set is needed "
                "before Splink comparison."
            ),
        ],
    }


def main() -> int:
    """Write the derived gold pilot without exposing source names or IDs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.event_log.is_file():
        parser.error(f"event log does not exist: {args.event_log}")
    report = build_gold_pilot(args.event_log)
    if not report["pairs"]:
        parser.error("no shared authoritative-identifier pairs found in the event log")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
