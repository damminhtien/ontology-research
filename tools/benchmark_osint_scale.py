#!/usr/bin/env python3
"""Run a synthetic OSINT scale level against the PostgreSQL projector.

The workload uses typed relationship assertions, inline evidence, source skew,
hub-heavy graph edges, and a multi-year valid-time distribution. Generated logs
and reports are written outside the repository under FOUNDRY_DATA_ROOT.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import random
import resource
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())
if (REPO_ROOT / "tools").as_posix() not in sys.path:
    sys.path.insert(0, (REPO_ROOT / "tools").as_posix())

from project_events import project_once  # noqa: E402
from psycopg import connect, sql  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402

from foundry.events import EventLog, SemanticEvent  # noqa: E402
from foundry.identity import IdentityService  # noqa: E402
from foundry.namespaces import CORE_ONTOLOGY_NS, ORGANIZATION_MIDDLE_NS  # noqa: E402
from foundry.postgres import PostgresDatabase, PostgresIdentityStore  # noqa: E402
from foundry.postgres_readmodel import PostgresReadModelStore  # noqa: E402
from foundry.provenance import normalize_evidence, normalize_source_id  # noqa: E402
from foundry.query_service import QueryService  # noqa: E402
from foundry.versioning import EVENT_SCHEMA_VERSION  # noqa: E402

LEVELS = {
    "l0": (10_000, 10),
    "l1": (100_000, 10),
    "l2": (1_000_000, 10),
    "l3": (10_000_000, 10),
    "l4": (100_000_000, 10),
}
LEVEL_ORDER = tuple(LEVELS)
SOURCE_COUNT = 64
ENTITY_TYPE_BY_RESIDUE = tuple(
    ["Organization"] * 20
    + ["Person"] * 10
    + ["Platform"] * 55
    + ["Facility"] * 5
    + ["Role"] * 5
    + ["Capability"] * 5
)
CORE = CORE_ONTOLOGY_NS
PREDICATES = {
    "partOf": CORE + "partOf",
    "locatedAt": CORE + "locatedAt",
    "memberOf": CORE + "memberOf",
    "operates": CORE + "operates",
    "owns": CORE + "owns",
    "hasRole": CORE + "hasRole",
    "hasCapability": CORE + "hasCapability",
    "subsidiaryOf": ORGANIZATION_MIDDLE_NS + "subsidiaryOf",
}
QUERY_SLOS_MS = {
    "entity_lookup": 50.0,
    "relationships_as_of": 500.0,
    "assertion_provenance": 500.0,
    "traverse_relationships": 300.0,
}
SOURCE_WEIGHTS = tuple(1 / ((index + 1) ** 1.25) for index in range(SOURCE_COUNT))
SOURCE_CUMULATIVE_WEIGHTS = tuple(sum(SOURCE_WEIGHTS[: index + 1]) for index in range(SOURCE_COUNT))
ALL_ENTITY_TYPES = frozenset(ENTITY_TYPE_BY_RESIDUE)
AGENT_TYPES = frozenset({"Organization", "Person"})
ARTIFACT_TYPES = frozenset({"Platform", "Facility"})
RELATION_CHOICES = {
    "Organization": (
        ("operates", 0.28, ARTIFACT_TYPES),
        ("owns", 0.20, ALL_ENTITY_TYPES),
        ("memberOf", 0.12, AGENT_TYPES),
        ("subsidiaryOf", 0.15, frozenset({"Organization"})),
        ("partOf", 0.15, ALL_ENTITY_TYPES),
        ("locatedAt", 0.10, frozenset()),
    ),
    "Person": (
        ("memberOf", 0.30, AGENT_TYPES),
        ("owns", 0.16, ALL_ENTITY_TYPES),
        ("hasRole", 0.16, frozenset({"Role"})),
        ("partOf", 0.14, ALL_ENTITY_TYPES),
        ("hasCapability", 0.12, frozenset({"Capability"})),
        ("locatedAt", 0.12, frozenset()),
    ),
    "Platform": (
        ("partOf", 0.45, ALL_ENTITY_TYPES),
        ("hasCapability", 0.25, frozenset({"Capability"})),
        ("locatedAt", 0.30, frozenset()),
    ),
    "Facility": (
        ("partOf", 0.40, ALL_ENTITY_TYPES),
        ("hasCapability", 0.25, frozenset({"Capability"})),
        ("locatedAt", 0.35, frozenset()),
    ),
    "Role": (
        ("partOf", 0.50, ALL_ENTITY_TYPES),
        ("hasCapability", 0.30, frozenset({"Capability"})),
        ("locatedAt", 0.20, frozenset()),
    ),
    "Capability": (
        ("partOf", 0.50, ALL_ENTITY_TYPES),
        ("hasCapability", 0.30, frozenset({"Capability"})),
        ("locatedAt", 0.20, frozenset()),
    ),
}
RELATION_SAMPLERS = {}
ENTITY_RELATION_SAMPLERS = {}
for _type_name, _choices in RELATION_CHOICES.items():
    for _entity_only, _target in ((False, RELATION_SAMPLERS), (True, ENTITY_RELATION_SAMPLERS)):
        _selected = [choice for choice in _choices if choice[2] or not _entity_only]
        _running_weight = 0.0
        _sampler = []
        for _name, _weight, _target_types in _selected:
            _running_weight += _weight
            _sampler.append((_running_weight, _name, _target_types))
        _target[_type_name] = tuple(_sampler)


def _entity_id(index: int) -> str:
    """Return the canonical identifier for a generated entity."""
    return f"urn:world:entity:scale-v1-{index:010d}"


def _entity_type(index: int) -> str:
    """Assign a stable class mix across the generated entity range."""
    return ENTITY_TYPE_BY_RESIDUE[index % len(ENTITY_TYPE_BY_RESIDUE)]


def _source_id(index: int) -> str:
    """Return one synthetic source IRI."""
    return f"https://synthetic.osint.example/source/{index:03d}"


def _stable_event_id(seed: int, kind: str, index: int, slot: int = 0) -> str:
    """Create deterministic event identifiers for reproducible event logs."""
    value = f"osint-scale-v1:{seed}:{kind}:{index}:{slot}".encode("ascii")
    return hashlib.sha256(value).hexdigest()[:32]


def _recorded_and_valid_time(entity_index: int, slot: int, seed: int) -> tuple[str, str]:
    """Return correlated recorded/valid times from old, middle, and recent cohorts."""
    value = (entity_index * 1_103_515_245 + slot * 12_345 + seed * 2_654_435_761) % (2**32)
    cohort = (entity_index + slot * 3 + seed) % 10
    if cohort < 5:
        start = datetime(2023, 1, 1, tzinfo=UTC)
        span_days = 1_060
    elif cohort < 8:
        start = datetime(2018, 1, 1, tzinfo=UTC)
        span_days = 2_190
    else:
        start = datetime(2010, 1, 1, tzinfo=UTC)
        span_days = 2_920
    valid_from = start + timedelta(days=value % span_days, hours=(value // span_days) % 24)
    recorded_at = datetime(2025, 11, 18, tzinfo=UTC) + timedelta(days=value % 365)
    return recorded_at.strftime("%Y-%m-%dT%H:%M:%SZ"), valid_from.strftime("%Y-%m-%dT%H:%M:%SZ")


def _source_sample(rng: random.Random) -> int:
    """Draw a source from a long-tailed distribution with a few dominant feeds."""
    return rng.choices(range(SOURCE_COUNT), cum_weights=SOURCE_CUMULATIVE_WEIGHTS)[0]


def _target_index(rng: random.Random, entity_count: int, categories: frozenset[str]) -> int:
    """Choose a type-compatible edge target, weighted toward popular hubs."""
    hot_limit = max(1, entity_count // 100)
    for _ in range(1_000):
        candidate = rng.randrange(hot_limit) if rng.random() < 0.70 else rng.randrange(entity_count)
        if _entity_type(candidate) in categories:
            return candidate
    raise RuntimeError(f"could not choose a target from categories {sorted(categories)}")


def _relation(
    rng: random.Random,
    entity_type: str,
    entity_count: int,
    *,
    force_entity: bool = False,
) -> tuple[str, dict[str, str]]:
    """Choose a domain/range-compatible predicate and its object value."""
    available = (
        ENTITY_RELATION_SAMPLERS[entity_type] if force_entity else RELATION_SAMPLERS[entity_type]
    )
    selected_weight = rng.random() * available[-1][0]
    _, predicate_name, target_types = next(item for item in available if selected_weight < item[0])
    if not target_types:
        location = rng.randrange(max(1, entity_count // 1_000))
        return PREDICATES[predicate_name], {
            "kind": "location",
            "value": f"https://synthetic.osint.example/location/{location:06d}",
        }
    target = _target_index(rng, entity_count, target_types)
    return PREDICATES[predicate_name], {"kind": "entity", "value": _entity_id(target)}


def _make_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    seed: int,
    key_kind: str,
    index: int,
    slot: int,
    recorded_at: str,
    valid_at: str | None = None,
) -> SemanticEvent:
    """Apply canonical event normalization, then pin reproducible event time/ID."""
    normalized = dict(payload)
    if normalized.get("source_id"):
        normalized["source_id"] = normalize_source_id(normalized["source_id"])
    if event_type == "AssertionMade":
        sources, evidence = normalize_evidence(
            normalized.get("source_ids") or (), normalized.get("evidence")
        )
        normalized["source_ids"] = list(sources)
        normalized["evidence"] = list(evidence)
    return SemanticEvent(
        event_id=_stable_event_id(seed, key_kind, index, slot),
        event_type=event_type,
        schema_version=EVENT_SCHEMA_VERSION,
        occurred_at=recorded_at,
        payload=normalized,
        valid_at=valid_at,
    )


def _assertion_payload(
    *,
    entity_index: int,
    slot: int,
    entity_count: int,
    seed: int,
    rng: random.Random,
) -> tuple[dict[str, Any], str, str]:
    """Build a typed relationship/location claim with source-local evidence."""
    entity_type = _entity_type(entity_index)
    predicate, object_value = _relation(rng, entity_type, entity_count, force_entity=slot == 0)
    recorded_at, valid_from = _recorded_and_valid_time(entity_index, slot, seed)
    valid_start = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
    value = (entity_index * 2_654_435_761 + slot * 22_695_477 + seed) % (2**32)
    valid_until = valid_start + timedelta(days=90 + ((value >> 7) % 1_371))
    source_total = 1 if value % 10 < 8 else 2 if value % 10 == 8 else 3
    evidence: list[dict[str, Any]] = []
    source_ids: list[str] = []
    for _ in range(source_total):
        source = _source_id(_source_sample(rng))
        while source in source_ids:
            source = _source_id(_source_sample(rng))
        source_ids.append(source)
        exact_text = f"Synthetic source supports relation {entity_index:010d} {slot:02d}."
        evidence.append(
            {
                "source_id": source,
                "locator": f"{source}/record/{entity_index:010d}?page={1 + value % 40}",
                "span": {
                    "start": 0,
                    "end": len(exact_text),
                    "unit": "character",
                    "exact_text": exact_text,
                },
            }
        )
    payload = {
        "assertion_id": f"urn:scale-v1:assertion:{entity_index:010d}:{slot:02d}",
        "subject_id": _entity_id(entity_index),
        "subject_ref": None,
        "predicate_iri": predicate,
        "object": object_value,
        "valid_from": valid_from,
        "valid_until": valid_until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "recorded_at": recorded_at,
        "source_ids": source_ids,
        "evidence": evidence,
        "model_score": round(0.55 + ((value >> 20) % 45) / 100, 2),
        "evidence_quality": ("direct", "corroborated", "secondary")[value % 3],
        "status": "asserted",
        "supersedes": None,
    }
    return payload, recorded_at, valid_from


def generate_events(entity_count: int, assertions_per_entity: int, seed: int):
    """Yield deterministic entity and assertion events with bounded memory."""
    rng = random.Random(seed)
    for entity_index in range(entity_count):
        entity_type = _entity_type(entity_index)
        source = _source_id(_source_sample(rng))
        created_at = datetime(2025, 11, 18, tzinfo=UTC) + timedelta(
            days=(entity_index * 137 + seed) % 365
        )
        created_text = created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        yield _make_event(
            "EntityCreated",
            {
                "entity_id": _entity_id(entity_index),
                "entity_type": entity_type,
                "name": f"Synthetic {entity_type} {entity_index:010d}",
                "source_id": source,
                "source_record_id": f"record-{entity_index:010d}",
                "confidence": 1.0,
            },
            seed=seed,
            key_kind="entity",
            index=entity_index,
            slot=0,
            recorded_at=created_text,
        )
        for slot in range(assertions_per_entity):
            payload, recorded_at, valid_from = _assertion_payload(
                entity_index=entity_index,
                slot=slot,
                entity_count=entity_count,
                seed=seed,
                rng=rng,
            )
            yield _make_event(
                "AssertionMade",
                payload,
                seed=seed,
                key_kind="assertion",
                index=entity_index,
                slot=slot,
                recorded_at=recorded_at,
                valid_at=valid_from,
            )


def _event_log_digest(log: EventLog) -> tuple[str, int, list[dict[str, Any]]]:
    """Hash committed event bytes in segment order without loading the log."""
    digest = hashlib.sha256()
    size_bytes = 0
    segments = []
    for filename, byte_count, _modified_at in log.fingerprint():
        path = Path(filename)
        segment_digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
                segment_digest.update(chunk)
        size_bytes += byte_count
        segments.append(
            {"name": path.name, "bytes": byte_count, "sha256": segment_digest.hexdigest()}
        )
    return digest.hexdigest(), size_bytes, segments


def _physical_memory_bytes() -> int | None:
    """Return installed physical memory when the host exposes it cheaply."""
    if sys.platform == "darwin":
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, check=False, text=True
        )
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return int(result.stdout.strip())
    if sys.platform.startswith("linux"):
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    return None


def _sysctl_value(name: str) -> str | None:
    """Return a macOS sysctl value without failing on other hosts."""
    if sys.platform != "darwin":
        return None
    result = subprocess.run(["sysctl", "-n", name], capture_output=True, check=False, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _git_state() -> dict[str, Any]:
    """Capture the source revision without including repository contents."""
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, check=False, text=True
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, check=False, text=True
    )
    return {
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "worktree_dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
        "benchmark_runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def _append_generated_log(
    log_path: Path,
    *,
    entity_count: int,
    assertions_per_entity: int,
    seed: int,
    batch_events: int,
) -> dict[str, Any]:
    """Generate and durably append events in bounded batches."""
    log = EventLog(log_path)
    expected_events = entity_count * (assertions_per_entity + 1)
    batch: list[SemanticEvent] = []
    batch_rates = []
    event_types: Counter[str] = Counter()
    predicate_counts: Counter[str] = Counter()
    object_kinds: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    evidence_counts: Counter[int] = Counter()
    temporal_cohorts: Counter[str] = Counter()
    count = 0
    started = time.perf_counter()
    batch_started = started
    for event in generate_events(entity_count, assertions_per_entity, seed):
        batch.append(event)
        event_types[event.event_type] += 1
        if event.event_type == "AssertionMade":
            payload = event.payload
            predicate_counts[payload["predicate_iri"]] += 1
            object_kinds[payload["object"]["kind"]] += 1
            evidence_counts[len(payload["evidence"])] += 1
            for source_id in payload["source_ids"]:
                source_counts[source_id] += 1
            year = int(payload["valid_from"][:4])
            temporal_cohorts[
                "recent" if year >= 2023 else "middle" if year >= 2018 else "older"
            ] += 1
        if len(batch) < batch_events:
            continue
        log.commit(
            batch,
            request_fingerprint=hashlib.sha256(
                f"{seed}:batch:{count}:{len(batch)}".encode("ascii")
            ).hexdigest(),
        )
        elapsed = time.perf_counter() - batch_started
        count += len(batch)
        batch_rates.append(
            {
                "events": len(batch),
                "seconds": round(elapsed, 4),
                "events_per_second": round(len(batch) / elapsed),
            }
        )
        batch.clear()
        batch_started = time.perf_counter()
        if len(batch_rates) == 1 or len(batch_rates) % 25 == 0:
            print(f"  generated {count:,}/{expected_events:,} events", flush=True)
    if batch:
        log.commit(
            batch,
            request_fingerprint=hashlib.sha256(
                f"{seed}:batch:{count}:{len(batch)}".encode("ascii")
            ).hexdigest(),
        )
        elapsed = time.perf_counter() - batch_started
        count += len(batch)
        batch_rates.append(
            {
                "events": len(batch),
                "seconds": round(elapsed, 4),
                "events_per_second": round(len(batch) / elapsed),
            }
        )
    total_seconds = time.perf_counter() - started
    digest, byte_count, segments = _event_log_digest(log)
    rates = [sample["events_per_second"] for sample in batch_rates]
    midpoint = max(1, len(rates) // 2)
    first_half = rates[:midpoint]
    second_half = rates[midpoint:] or rates[-1:]
    return {
        "events": count,
        "expected_events": expected_events,
        "event_log_sha256": digest,
        "event_log_bytes": byte_count,
        "generation_seconds": round(total_seconds, 4),
        "generation_events_per_second": round(count / total_seconds),
        "append_batches": batch_rates,
        "append_rate_first_half_events_per_second": round(sum(first_half) / len(first_half)),
        "append_rate_second_half_events_per_second": round(sum(second_half) / len(second_half)),
        "append_rate_drift_percent": round(
            (sum(second_half) / len(second_half) / (sum(first_half) / len(first_half)) - 1) * 100,
            2,
        ),
        "segments": segments,
        "event_type_counts": dict(sorted(event_types.items())),
        "predicate_counts": dict(sorted(predicate_counts.items())),
        "object_kind_counts": dict(sorted(object_kinds.items())),
        "evidence_cardinality_counts": {
            str(size): evidence_counts[size] for size in sorted(evidence_counts)
        },
        "source_reference_counts": dict(sorted(source_counts.items())),
        "valid_time_cohort_counts": dict(sorted(temporal_cohorts.items())),
    }


def _percentile(values: list[float], fraction: float) -> float:
    """Return the nearest-rank percentile for a non-empty sample."""
    ordered = sorted(values)
    return ordered[max(0, int(fraction * len(ordered) + 0.999999) - 1)]


def _measure_query_operation(
    service: QueryService,
    operation: str,
    entity_count: int,
    *,
    runs: int,
    concurrency: int,
    seed: int,
) -> dict[str, Any]:
    """Measure one operational query through QueryService and psycopg."""
    rng = random.Random(seed + concurrency + sum(operation.encode("ascii")))
    samples = [rng.randrange(entity_count) for _ in range(runs)]
    durations: list[float] = []
    errors: Counter[str] = Counter()

    def request(index: int) -> tuple[float, str | None]:
        entity_index = samples[index]
        entity_id = _entity_id(entity_index)
        started = time.perf_counter_ns()
        try:
            if operation == "entity_lookup":
                result = service.entity_lookup(entity_id, timeout_ms=250)
                if result is None:
                    raise RuntimeError("missing generated entity")
            elif operation == "relationships_as_of":
                _recorded_at, valid_at = _recorded_and_valid_time(entity_index, 0, seed)
                instant = datetime.fromisoformat(valid_at.replace("Z", "+00:00"))
                result = service.relationships_as_of(entity_id, instant, limit=100, timeout_ms=500)
                if not result.items:
                    raise RuntimeError("expected at least one relationship at its valid_from")
            elif operation == "assertion_provenance":
                result = service.assertion_provenance(
                    f"urn:scale-v1:assertion:{entity_index:010d}:00", timeout_ms=500
                )
                if result is None:
                    raise RuntimeError("missing generated assertion provenance")
            elif operation == "traverse_relationships":
                _recorded_at, valid_at = _recorded_and_valid_time(entity_index, 0, seed)
                instant = datetime.fromisoformat(valid_at.replace("Z", "+00:00"))
                result = service.traverse_relationships(
                    entity_id, instant, max_hops=2, limit=25, timeout_ms=500
                )
            else:
                raise ValueError(f"unknown query operation: {operation}")
            return (time.perf_counter_ns() - started) / 1_000_000, None
        except Exception as exc:  # report query timeouts and consistency failures separately
            return (time.perf_counter_ns() - started) / 1_000_000, type(exc).__name__

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(request, index) for index in range(runs)]
        for future in concurrent.futures.as_completed(futures):
            latency, error = future.result()
            durations.append(latency)
            if error:
                errors[error] += 1
    successful = len(durations) - sum(errors.values())
    slo = QUERY_SLOS_MS.get(operation)
    return {
        "samples": runs,
        "concurrency": concurrency,
        "successes": successful,
        "errors": dict(sorted(errors.items())),
        "p50_ms": round(_percentile(durations, 0.50), 4),
        "p95_ms": round(_percentile(durations, 0.95), 4),
        "p99_ms": round(_percentile(durations, 0.99), 4),
        "slo_p95_ms": slo,
        "within_p95_slo": None
        if slo is None
        else not errors and _percentile(durations, 0.95) <= slo,
    }


def _run_concurrent_append_probe(
    log: EventLog,
    database: PostgresDatabase,
    model: PostgresReadModelStore,
    service: QueryService,
    *,
    entity_count: int,
    seed: int,
    append_count: int,
    batch_size: int = 20,
) -> dict[str, Any]:
    """Append/project a tail while bounded operational readers stay active."""
    if append_count <= 0:
        return {"append_count": 0, "reader_calls": 0, "reader_errors": {}, "reader_p95_ms": None}
    subject_index = 0
    subject_id = _entity_id(subject_index)
    instant = datetime(2025, 1, 1, tzinfo=UTC)
    start = threading.Event()
    stop = threading.Event()
    metrics_lock = threading.Lock()
    latencies: list[float] = []
    errors: Counter[str] = Counter()
    reader_calls = 0

    def reader() -> None:
        nonlocal reader_calls
        start.wait()
        while not stop.is_set():
            began = time.perf_counter_ns()
            try:
                page = service.relationships_as_of(subject_id, instant, limit=50, timeout_ms=500)
                if any(
                    item["object"]["kind"] == "entity" and not item["object"]["value"]
                    for item in page.items
                ):
                    raise RuntimeError("reader observed an empty entity target")
                error = None
            except Exception as exc:
                error = type(exc).__name__
            elapsed = (time.perf_counter_ns() - began) / 1_000_000
            with metrics_lock:
                reader_calls += 1
                latencies.append(elapsed)
                if error:
                    errors[error] += 1

    reader_threads = 8
    with concurrent.futures.ThreadPoolExecutor(max_workers=reader_threads + 1) as executor:
        readers = [executor.submit(reader) for _ in range(reader_threads)]
        start.set()
        append_started = time.perf_counter()
        for batch_start in range(0, append_count, batch_size):
            batch_end = min(append_count, batch_start + batch_size)
            events = []
            for index in range(batch_start, batch_end):
                source = _source_id(index % SOURCE_COUNT)
                evidence_text = f"Concurrent append evidence {index:08d}."
                payload = {
                    "assertion_id": f"urn:scale-v1:concurrent:{index:010d}",
                    "subject_id": subject_id,
                    "subject_ref": None,
                    "predicate_iri": PREDICATES["partOf"],
                    "object": {"kind": "entity", "value": _entity_id((index + 1) % entity_count)},
                    "valid_from": "2024-01-01T00:00:00Z",
                    "valid_until": None,
                    "recorded_at": "2026-11-18T00:00:00Z",
                    "source_ids": [source],
                    "evidence": [
                        {
                            "source_id": source,
                            "locator": f"{source}/append/{index:010d}",
                            "span": {
                                "start": 0,
                                "end": len(evidence_text),
                                "exact_text": evidence_text,
                            },
                        }
                    ],
                    "model_score": 0.8,
                    "evidence_quality": "direct",
                    "status": "asserted",
                    "supersedes": None,
                }
                events.append(
                    _make_event(
                        "AssertionMade",
                        payload,
                        seed=seed,
                        key_kind="concurrent-assertion",
                        index=index,
                        slot=0,
                        recorded_at="2026-11-18T00:00:00Z",
                        valid_at="2024-01-01T00:00:00Z",
                    )
                )
            log.commit(
                events,
                request_fingerprint=hashlib.sha256(
                    f"{seed}:concurrent:{batch_start}:{batch_end}".encode("ascii")
                ).hexdigest(),
            )
            project_once(
                log, IdentityService(PostgresIdentityStore(database)), model, limit=len(events)
            )
        append_seconds = time.perf_counter() - append_started
        stop.set()
        for reader_future in readers:
            reader_future.result()
    return {
        "append_count": append_count,
        "append_seconds": round(append_seconds, 4),
        "append_and_project_events_per_second": round(append_count / append_seconds),
        "reader_workers": reader_threads,
        "reader_calls": reader_calls,
        "reader_errors": dict(sorted(errors.items())),
        "reader_p50_ms": round(_percentile(latencies, 0.50), 4) if latencies else None,
        "reader_p95_ms": round(_percentile(latencies, 0.95), 4) if latencies else None,
        "reader_p99_ms": round(_percentile(latencies, 0.99), 4) if latencies else None,
    }


def _verify_in_new_process(log_path: Path, database_url: str) -> tuple[float, str]:
    """Measure reopening and checkpoint verification in a fresh Python process."""
    command = [
        sys.executable,
        str(REPO_ROOT / "tools" / "project_events.py"),
        "--event-log",
        str(log_path),
        "--database-url",
        database_url,
        "--verify",
    ]
    started = time.perf_counter()
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, check=False, text=True)
    elapsed = time.perf_counter() - started
    if result.returncode:
        raise RuntimeError(f"checkpoint reopen verification failed: {result.stderr.strip()}")
    return elapsed, result.stdout.strip()


def _database_measurements(database_url: str) -> dict[str, Any]:
    """Read database and projection table sizes without materializing rows."""
    with connect(database_url) as connection:
        version = connection.execute("SELECT version()").fetchone()[0]
        counts = connection.execute(
            """SELECT (SELECT count(*) FROM read_entities),
                      (SELECT count(*) FROM identity_entities),
                      (SELECT count(*) FROM identity_source_records),
                      (SELECT count(*) FROM read_assertions),
                      (SELECT count(*) FROM projection_events),
                      (SELECT checkpoint_sequence FROM projection_state WHERE singleton = TRUE)"""
        ).fetchone()
        database_bytes = connection.execute(
            "SELECT pg_database_size(current_database())"
        ).fetchone()[0]
        table_sizes = {}
        for table in (
            "read_entities",
            "read_entity_names",
            "read_assertions",
            "projection_events",
            "identity_entities",
            "identity_aliases",
            "identity_source_records",
        ):
            row = connection.execute(
                "SELECT pg_total_relation_size(%s::regclass)", (table,)
            ).fetchone()
            table_sizes[table] = row[0]
    return {
        "postgresql_version": version,
        "database_bytes": database_bytes,
        "table_and_index_bytes": table_sizes,
        "read_entities": counts[0],
        "identity_entities": counts[1],
        "source_records": counts[2],
        "assertions": counts[3],
        "projection_events": counts[4],
        "checkpoint_sequence": counts[5],
    }


def _run_level(
    *,
    level: str,
    entity_count: int,
    assertions_per_entity: int,
    seed: int,
    data_root: Path,
    database_admin_url: str,
    append_batch_events: int,
    query_runs: int,
    concurrency_levels: tuple[int, ...],
    tail_appends: int,
) -> dict[str, Any]:
    """Generate, project, query and recover one synthetic scale level."""
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{uuid.uuid4().hex[:8]}"
    level_root = data_root / "evaluations" / "osint-scale-ladder-v1" / run_id / level
    level_root.mkdir(parents=True, exist_ok=False)
    log_path = level_root / "events.jsonl"
    expected_assertions = entity_count * assertions_per_entity
    expected_events = entity_count + expected_assertions
    estimated_event_bytes = int(expected_events * 1_350)
    free_before = shutil.disk_usage(level_root).free
    if free_before < estimated_event_bytes * 2:
        raise RuntimeError(
            f"insufficient free disk for estimated event log plus database: "
            f"need {estimated_event_bytes * 2:,}, have {free_before:,} bytes"
        )

    print(
        f"Generating {level}: {entity_count:,} entities / {expected_assertions:,} assertions",
        flush=True,
    )
    generation = _append_generated_log(
        log_path,
        entity_count=entity_count,
        assertions_per_entity=assertions_per_entity,
        seed=seed,
        batch_events=append_batch_events,
    )
    if generation["events"] != expected_events:
        raise RuntimeError(f"generated {generation['events']} events, expected {expected_events}")
    log = EventLog(log_path)

    database_name = f"ontology_scale_{entity_count}_{uuid.uuid4().hex[:12]}"
    admin_url = make_conninfo(database_admin_url, dbname="postgres")
    database_url = make_conninfo(database_admin_url, dbname=database_name)
    with connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    db = PostgresDatabase(database_url)
    report: dict[str, Any] = {
        "level": level,
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": _git_state(),
        "target": {"entities": entity_count, "assertions": expected_assertions},
        "workload": {
            "seed": seed,
            "assertions_per_entity": assertions_per_entity,
            "event_count": expected_events,
            "relationship_predicates": sorted(PREDICATES.values()),
            "entity_type_distribution": {
                name: sum(value == name for value in ENTITY_TYPE_BY_RESIDUE) / 100
                for name in sorted(set(ENTITY_TYPE_BY_RESIDUE))
            },
            "source_count": SOURCE_COUNT,
            "edge_target_distribution": (
                "70% sampled from the top 1% hub range; 30% across all entities"
            ),
            "valid_time_distribution": (
                "valid windows start in 2010, 2018, or 2023 and extend for 3-8 years; "
                "reported year buckets can overlap; recorded from 2025-2026"
            ),
            "evidence": (
                "one to three source references per assertion, with locator and "
                "exact synthetic text span"
            ),
            "event_log_sha256": generation["event_log_sha256"],
            "event_log_bytes": generation["event_log_bytes"],
            "generation": generation,
        },
        "artifacts": {
            "event_log": str(log_path),
            "output_directory": str(level_root),
        },
        "database": {"temporary_database": database_name},
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "hardware_model": _sysctl_value("hw.model"),
            "processor": _sysctl_value("machdep.cpu.brand_string") or platform.processor(),
            "logical_cpus": os.cpu_count(),
            "physical_memory_bytes": _physical_memory_bytes(),
            "data_root_free_bytes_before": free_before,
        },
    }

    try:
        migration_started = time.perf_counter()
        db.migrate()
        migration_seconds = time.perf_counter() - migration_started
        identity = IdentityService(PostgresIdentityStore(db))
        model = PostgresReadModelStore(db)
        projection_rates = []
        projection_started = time.perf_counter()
        while (model.checkpoint_sequence or 0) < expected_events:
            batch_started = time.perf_counter()
            batch_result = project_once(log, identity, model, limit=append_batch_events)
            elapsed = time.perf_counter() - batch_started
            processed = batch_result["applied"] + batch_result["skipped"]
            if processed <= 0:
                raise RuntimeError("projector stopped before reaching the event-log tail")
            projection_rates.append(
                {
                    "events": processed,
                    "seconds": round(elapsed, 4),
                    "events_per_second": round(processed / elapsed),
                    "checkpoint": batch_result["checkpoint_sequence"],
                }
            )
            if len(projection_rates) == 1 or len(projection_rates) % 25 == 0:
                checkpoint = batch_result["checkpoint_sequence"]
                print(f"  projected {checkpoint:,}/{expected_events:,} events", flush=True)
        projection_seconds = time.perf_counter() - projection_started
        expected_checkpoint = log.last_sequence
        verification = db.verify_projection(expected_checkpoint)
        if verification["consistent"] is not True:
            raise RuntimeError(f"initial projection is inconsistent: {verification}")

        analyze_started = time.perf_counter()
        with db.connection() as connection:
            for table in (
                "read_entities",
                "read_entity_names",
                "read_assertions",
                "projection_events",
                "identity_entities",
            ):
                connection.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))
        analyze_seconds = time.perf_counter() - analyze_started

        service = QueryService(model)
        query_results = {}
        for operation in (
            "entity_lookup",
            "relationships_as_of",
            "assertion_provenance",
            "traverse_relationships",
        ):
            query_results[operation] = [
                _measure_query_operation(
                    service,
                    operation,
                    entity_count,
                    runs=query_runs,
                    concurrency=concurrency,
                    seed=seed,
                )
                for concurrency in concurrency_levels
            ]

        index_rebuilds = []
        with db.connection() as connection:
            for table in (
                "identity_entities",
                "identity_aliases",
                "identity_source_records",
                "read_entities",
                "read_entity_names",
                "read_assertions",
                "projection_events",
            ):
                started = time.perf_counter()
                connection.execute(sql.SQL("REINDEX TABLE {}").format(sql.Identifier(table)))
                index_rebuilds.append(
                    {"table": table, "seconds": round(time.perf_counter() - started, 4)}
                )
        index_rebuild_seconds = sum(item["seconds"] for item in index_rebuilds)

        concurrent_append = _run_concurrent_append_probe(
            log,
            db,
            model,
            service,
            entity_count=entity_count,
            seed=seed,
            append_count=tail_appends,
        )
        final_log_sha256, final_log_bytes, final_log_segments = _event_log_digest(log)
        verification_seconds, verification_output = _verify_in_new_process(log_path, database_url)
        final = _database_measurements(database_url)
        if final["read_entities"] != entity_count:
            raise RuntimeError(
                f"read entity count mismatch: {final['read_entities']} != {entity_count}"
            )
        if final["assertions"] != expected_assertions + tail_appends:
            expected_final_assertions = expected_assertions + tail_appends
            raise RuntimeError(
                f"assertion count mismatch: {final['assertions']} != {expected_final_assertions}"
            )
        if final["checkpoint_sequence"] != log.last_sequence:
            raise RuntimeError(
                "projection checkpoint does not match the event log after concurrent writes"
            )

        rates = [item["events_per_second"] for item in projection_rates]
        midpoint = max(1, len(rates) // 2)
        first_half = rates[:midpoint]
        second_half = rates[midpoint:] or rates[-1:]
        combined_bytes = final_log_bytes + final["database_bytes"]
        report["results"] = {
            "migration_and_initial_index_setup_seconds": round(migration_seconds, 4),
            "index_analyze_seconds": round(analyze_seconds, 4),
            "projection_seconds": round(projection_seconds, 4),
            "projection_events_per_second": round(expected_events / projection_seconds),
            "projection_batches": projection_rates,
            "projection_rate_first_half_events_per_second": round(
                sum(first_half) / len(first_half)
            ),
            "projection_rate_second_half_events_per_second": round(
                sum(second_half) / len(second_half)
            ),
            "projection_rate_drift_percent": round(
                (sum(second_half) / len(second_half) / (sum(first_half) / len(first_half)) - 1)
                * 100,
                2,
            ),
            "query_boundary": (
                "QueryService + PostgreSQL + psycopg + Python result materialization; no HTTP"
            ),
            "queries": query_results,
            "index_rebuild_seconds": round(index_rebuild_seconds, 4),
            "index_rebuilds": index_rebuilds,
            "restart_checkpoint_verify_seconds": round(verification_seconds, 4),
            "restart_checkpoint_verify_output": verification_output,
            "concurrent_append_read": concurrent_append,
            "storage": {
                "event_log_bytes": final_log_bytes,
                "event_log_generated_bytes": generation["event_log_bytes"],
                "database_bytes": final["database_bytes"],
                "database_to_event_log_amplification": round(
                    final["database_bytes"] / max(generation["event_log_bytes"], 1), 3
                ),
                "combined_bytes_per_million_assertions": round(
                    combined_bytes * 1_000_000 / final["assertions"]
                ),
                "table_and_index_bytes": final["table_and_index_bytes"],
            },
            "consistency": {
                "initial_projection": verification,
                "final_entity_count": final["read_entities"],
                "final_identity_count": final["identity_entities"],
                "final_source_record_count": final["source_records"],
                "final_assertion_count": final["assertions"],
                "final_projection_event_count": final["projection_events"],
                "final_checkpoint_sequence": final["checkpoint_sequence"],
                "expected_final_checkpoint": log.last_sequence,
            },
            "database_version": final["postgresql_version"],
            "python_process_peak_rss_bytes": _process_peak_rss_bytes(),
            "memory_scope": (
                "Python process high-water RSS only; PostgreSQL server and other system processes "
                "are excluded"
            ),
            "data_root_free_bytes_after": shutil.disk_usage(level_root).free,
        }
        report["artifacts"]["event_log_after_tail_append"] = {
            "sha256": final_log_sha256,
            "bytes": final_log_bytes,
            "segments": final_log_segments,
            "last_sequence": log.last_sequence,
        }
        return report
    finally:
        with connect(admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )


def _process_peak_rss_bytes() -> int:
    """Return this benchmark process high-water RSS in bytes."""
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _parse_concurrency(value: str) -> tuple[int, ...]:
    """Parse positive, distinct query concurrency levels."""
    try:
        levels = tuple(int(item) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "concurrency must be comma-separated positive integers"
        ) from exc
    if not levels or any(level < 1 for level in levels) or len(set(levels)) != len(levels):
        raise argparse.ArgumentTypeError("concurrency values must be positive and unique")
    return levels


def main() -> int:
    """Run a selected OSINT scale level and persist its reproducible report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--level", choices=LEVEL_ORDER, default="l0")
    parser.add_argument("--entities", type=int, help="override the selected ladder level")
    parser.add_argument("--assertions-per-entity", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--append-batch-events", type=int, default=10_000)
    parser.add_argument("--query-runs", type=int, default=100)
    parser.add_argument("--concurrency", type=_parse_concurrency, default=(1, 8, 32))
    parser.add_argument("--tail-appends", type=int, default=100)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("FOUNDRY_DATA_ROOT", "/Users/macbook/Data/ontology-research")),
    )
    parser.add_argument("--database-url", default=os.environ.get("FOUNDRY_DATABASE_URL"))
    parser.add_argument("--output", type=Path, help="optional report output path")
    args = parser.parse_args()
    if args.entities is None:
        entities, planned_assertions = LEVELS[args.level]
    else:
        entities, planned_assertions = args.entities, args.assertions_per_entity
    if entities < 100 or planned_assertions < 1 or entities > 100_000_000:
        parser.error("entities must be 100..100,000,000 and assertions-per-entity must be positive")
    if args.append_batch_events < 1 or args.query_runs < 1 or args.tail_appends < 0:
        parser.error("batch size and query runs must be positive; tail appends cannot be negative")
    if not args.database_url:
        parser.error(
            "--database-url or FOUNDRY_DATABASE_URL is required for the PostgreSQL scale run"
        )
    args.data_root.mkdir(parents=True, exist_ok=True)
    try:
        report = _run_level(
            level=args.level if args.entities is None else "custom",
            entity_count=entities,
            assertions_per_entity=planned_assertions,
            seed=args.seed,
            data_root=args.data_root,
            database_admin_url=args.database_url,
            append_batch_events=args.append_batch_events,
            query_runs=args.query_runs,
            concurrency_levels=args.concurrency,
            tail_appends=args.tail_appends,
        )
    except Exception as exc:
        parser.error(str(exc))
    command = [
        "env",
        "FOUNDRY_DATABASE_URL=<PostgreSQL admin DSN>",
        str(sys.executable),
        str(Path(__file__).resolve()),
        "--level",
        args.level,
        "--seed",
        str(args.seed),
        "--append-batch-events",
        str(args.append_batch_events),
        "--query-runs",
        str(args.query_runs),
        "--concurrency",
        ",".join(str(value) for value in args.concurrency),
        "--tail-appends",
        str(args.tail_appends),
        "--data-root",
        str(args.data_root),
    ]
    if args.entities is not None:
        command.extend(
            [
                "--entities",
                str(args.entities),
                "--assertions-per-entity",
                str(args.assertions_per_entity),
            ]
        )
    if args.output is not None:
        command.extend(["--output", str(args.output)])
    report["reproduction"] = {
        "command_template": command,
        "note": "Replace the DSN placeholder; the DSN value is intentionally not saved.",
    }
    output_path = args.output or Path(report["artifacts"]["output_directory"]) / "scale-report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "report": str(output_path),
                "event_log": report["artifacts"]["event_log"],
                "entities": report["target"]["entities"],
                "assertions": report["target"]["assertions"],
                "projection_events_per_second": report["results"]["projection_events_per_second"],
                "database_bytes": report["results"]["storage"]["database_bytes"],
                "python_process_peak_rss_bytes": report["results"]["python_process_peak_rss_bytes"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
