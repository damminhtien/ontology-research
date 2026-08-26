"""Synthetic ingestion/projection/query benchmark (Phase 2-3 measurement).

Deterministic generator (fixed seed) -> measures three things and writes a
JSON + markdown report to build/:

1. ingestion throughput  - events/s through raw log appends and the identity
                           resolution hot path
2. projection throughput - events/s replaying the log through the Projector
3. query latency         - p50/p95/p99 on the read model (Q1/Q2-style/Q4),
                           >= 100 warm runs per the SLO measurement rules

Scaffold note: numbers are machine-dependent floors measured on in-memory
structures; the report compares against roadmap SLOs as information, it does
not gate CI. Real-store benchmarks arrive with Phase 6.

Usage: .venv/bin/python tools/benchmark.py [--scale 1000] [--runs 200]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (REPO_ROOT / "tools", REPO_ROOT):
    if _path.as_posix() not in sys.path:
        sys.path.insert(0, _path.as_posix())

from foundry.events import EventLog, make_event  # noqa: E402
from foundry.identity import IdentityService  # noqa: E402
from foundry.projector import replay_log  # noqa: E402
from foundry.readmodel import parse_instant  # noqa: E402

SLO_REFERENCE = {
    "q1_entity_lookup_ms": 50,
    "q_current_location_ms": 100,
    "q4_temporal_ms": 500,
}


def generate_events(n_entities: int, observations_per: int, seed: int = 42) -> list:
    """Deterministic synthetic event set: entities then location observations."""
    rng = random.Random(seed)
    events = []
    for i in range(n_entities):
        entity_id = f"urn:world:entity:bench-{i:08d}"
        events.append(
            make_event(
                "EntityCreated",
                {
                    "entity_id": entity_id,
                    "entity_type": rng.choice(["Platform", "Organization", "Facility"]),
                    "name": f"Synthetic Entity {i}",
                    "source_id": "https://data.example/source/benchmark",
                    "confidence": 1.0,
                },
            )
        )
        for j in range(observations_per):
            events.append(
                make_event(
                    "LocationObserved",
                    {
                        "entity_id": entity_id,
                        "location_uri": f"https://data.example/entity/loc-{rng.randint(0, 99):03d}",
                        "valid_from": f"2026-08-{10 + j:02d}T08:{rng.randint(0, 59):02d}:00Z",
                        "source_ids": ["https://data.example/source/benchmark"],
                        "confidence": 0.8,
                    },
                )
            )
    return events


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile for a non-empty value list."""
    ordered = sorted(values)
    rank = max(1, round(fraction * len(ordered)))
    return ordered[rank - 1]


def run_benchmark(n_entities: int, observations_per: int, runs: int, out_dir: Path) -> dict:
    """Execute the benchmark suite and return the report dict."""
    events = generate_events(n_entities, observations_per)
    entity_ids = [e.payload["entity_id"] for e in events if e.event_type == "EntityCreated"]

    # 1a. raw append throughput (no validation)
    log_path = out_dir / "benchmark-events.jsonl"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path.unlink(missing_ok=True)
    start = time.perf_counter()
    EventLog(log_path).extend(events)
    raw_append_secs = time.perf_counter() - start
    raw_rate = len(events) / raw_append_secs

    # 1b. identity-resolution hot path (exact-name hits on a populated registry)
    identity = IdentityService()
    for eid in entity_ids:
        identity.register(entity_id=eid, entity_type="Platform", aliases=[])
    name = f"Synthetic Entity {0}"
    lookup_events = [e for e in events if e.event_type == "LocationObserved"]
    start = time.perf_counter()
    for _event in lookup_events:
        identity.resolve(name=name, entity_type="Platform")
    id_secs = time.perf_counter() - start
    id_rate = len(lookup_events) / id_secs if id_secs else float("inf")

    # 2. projection throughput (full replay)
    log = EventLog(log_path)
    start = time.perf_counter()
    model, proj_stats = replay_log(log)
    proj_secs = time.perf_counter() - start
    proj_rate = len(events) / proj_secs

    # 3. query latency on the warm read model
    step = max(1, len(entity_ids) // runs)
    probe_ids = entity_ids[::step][:runs]
    as_of = parse_instant("2026-08-12T00:00:00Z")

    def measure(fn) -> dict:
        timings = []
        for entity_id in probe_ids:
            begin = time.perf_counter()
            fn(entity_id)
            timings.append((time.perf_counter() - begin) * 1000.0)
        return {
            "runs": len(timings),
            "p50_ms": round(percentile(timings, 0.50), 4),
            "p95_ms": round(percentile(timings, 0.95), 4),
            "p99_ms": round(percentile(timings, 0.99), 4),
        }

    query_latencies = {
        "q1_entity_lookup_ms": measure(model.get_entity),
        "q_current_location_ms": measure(model.current_location),
        "q4_temporal_ms": measure(lambda eid: model.location_as_of(eid, as_of)),
    }

    report = {
        "scale": {
            "entities": n_entities,
            "observations_per_entity": observations_per,
            "total_events": len(events),
            "seed": 42,
        },
        "ingestion": {
            "raw_append_events_per_s": round(raw_rate),
            "raw_append_secs": round(raw_append_secs, 4),
            "identity_lookup_per_s": round(id_rate),
            "slo_reference_events_per_s": "10^4 - 10^5",
        },
        "projection": {
            "events_per_s": round(proj_rate),
            "secs": round(proj_secs, 4),
            "applied": proj_stats.applied,
            "skipped": proj_stats.skipped,
            "read_model": model.stats(now=parse_instant("2099-01-01T00:00:00Z")),
        },
        "query_latency": query_latencies,
        "slo_comparison": {
            name: {
                "p95_ms": query_latencies[name]["p95_ms"],
                "slo_ms": SLO_REFERENCE[name],
                "within_slo": query_latencies[name]["p95_ms"] <= SLO_REFERENCE[name],
            }
            for name in SLO_REFERENCE
        },
    }

    (out_dir / "benchmark-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    log_path.unlink(missing_ok=True)
    return report


def print_report(report: dict) -> None:
    """Human-readable summary table."""
    scale = report["scale"]
    print(
        f"\nBenchmark: {scale['total_events']} events "
        f"({scale['entities']} entities x {scale['observations_per_entity']} observations)\n"
    )
    ing = report["ingestion"]
    print(f"  raw append   : {ing['raw_append_events_per_s']:>10,} events/s")
    print(f"  identity hit : {ing['identity_lookup_per_s']:>10,} lookups/s")
    proj = report["projection"]
    print(f"  projection   : {proj['events_per_s']:>10,} events/s")
    print("\n  query latency (warm, p50/p95/p99 ms):")
    for name, metric in report["query_latency"].items():
        slo = report["slo_comparison"][name]
        verdict = "within SLO" if slo["within_slo"] else "OVER SLO"
        print(
            f"    {name:<24} {metric['p50_ms']:>7.4f} / {metric['p95_ms']:>7.4f} / "
            f"{metric['p99_ms']:>7.4f}  (SLO {slo['slo_ms']}ms, {verdict})"
        )


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", type=int, default=1000, help="number of synthetic entities")
    parser.add_argument("--observations", type=int, default=3, help="observations per entity")
    parser.add_argument("--runs", type=int, default=200, help="latency measurement runs")
    parser.add_argument("--out", default="build", help="output directory")
    args = parser.parse_args()

    out_dir = REPO_ROOT / args.out
    report = run_benchmark(args.scale, args.observations, args.runs, out_dir)
    print_report(report)
    print(f"\nWrote {out_dir / 'benchmark-report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
