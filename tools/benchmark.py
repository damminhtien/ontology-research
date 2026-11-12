"""Synthetic ingestion/projection/query benchmark (Phase 2-3 measurement).

Deterministic generator (fixed seed) -> measures three things and writes a
JSON report to build/:

1. ingestion throughput  - events/s through raw log appends and the identity
                           resolution hot path
2. projection throughput - events/s replaying the log through the Projector
3. query latency         - p50/p95/p99 on the read model (Q1/Q2-style/Q4),
                           >= 100 warm runs per the SLO measurement rules

Scaffold note: the event source is streamed, but identity and read-model indexes
remain in memory. Numbers are machine-dependent measurements, not evidence of
the Phase 6 distributed-storage targets; they do not gate CI.

Usage: .venv/bin/python tools/benchmark.py [--scale 1000] [--runs 200]
"""

from __future__ import annotations

import argparse
import json
import random
import resource
import sys
import time
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (REPO_ROOT / "tools", REPO_ROOT):
    if _path.as_posix() not in sys.path:
        sys.path.insert(0, _path.as_posix())

from foundry.events import EventLog, SemanticEvent, make_event  # noqa: E402
from foundry.identity import IdentityService  # noqa: E402
from foundry.namespaces import CORE_LOCATED_AT  # noqa: E402
from foundry.projector import replay_log  # noqa: E402
from foundry.readmodel import parse_instant  # noqa: E402

SLO_REFERENCE = {
    "q1_entity_lookup_ms": 50,
    "q_current_location_ms": 100,
    "q4_temporal_ms": 500,
}
QUERY_REPETITIONS = 100


def generate_events(
    n_entities: int, observations_per: int, seed: int = 42
) -> Iterator[SemanticEvent]:
    """Yield deterministic synthetic events without retaining the event set."""
    rng = random.Random(seed)
    for i in range(n_entities):
        entity_id = f"urn:world:entity:bench-{i:08d}"
        yield make_event(
            "EntityCreated",
            {
                "entity_id": entity_id,
                "entity_type": rng.choice(["Platform", "Organization", "Facility"]),
                "name": f"Synthetic Entity {i}",
                "source_id": "https://data.example/source/benchmark",
                "confidence": 1.0,
            },
        )
        for j in range(observations_per):
            yield make_event(
                "AssertionMade",
                {
                    "assertion_id": f"urn:assert:bench:{i}:{j}",
                    "subject_id": entity_id,
                    "subject_ref": None,
                    "predicate_iri": CORE_LOCATED_AT,
                    "object": {
                        "kind": "location",
                        "value": f"https://data.example/entity/loc-{rng.randint(0, 99):03d}",
                    },
                    "valid_from": f"2026-08-{10 + j:02d}T08:00:00Z",
                    "valid_until": (
                        f"2026-08-{11 + j:02d}T08:00:00Z" if j + 1 < observations_per else None
                    ),
                    "source_ids": ["https://data.example/source/benchmark"],
                    "evidence": [
                        {
                            "source_id": "https://data.example/source/benchmark",
                            "locator": None,
                            "span": None,
                        }
                    ],
                    "confidence": 0.8,
                    "supersedes": None,
                },
            )


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile for a non-empty value list."""
    ordered = sorted(values)
    rank = max(1, round(fraction * len(ordered)))
    return ordered[rank - 1]


def run_benchmark(n_entities: int, observations_per: int, runs: int, out_dir: Path) -> dict:
    """Execute the benchmark suite and return the report dict."""
    if n_entities < 1 or observations_per < 0 or runs < 1:
        raise ValueError("entities and runs must be positive; observations cannot be negative")
    total_events = n_entities * (observations_per + 1)

    # 1a. synthetic event production plus durable log append (no SHACL gate)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "benchmark-events.jsonl"
    for segment in out_dir.glob("benchmark-events-*.jsonl"):
        segment.unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)
    start = time.perf_counter()
    EventLog(log_path).extend(generate_events(n_entities, observations_per))
    synthetic_ingest_secs = time.perf_counter() - start
    synthetic_ingest_rate = total_events / synthetic_ingest_secs

    # 1b. identity-resolution hot path (exact-name hits on a populated registry)
    identity = IdentityService()
    for index in range(n_entities):
        identity.register(
            entity_id=f"urn:world:entity:bench-{index:08d}",
            entity_type="Platform",
            aliases=[f"Synthetic Entity {index}"],
        )
    name = f"Synthetic Entity {0}"
    lookup_count = n_entities * observations_per
    exact_hits = 0
    start = time.perf_counter()
    for _ in range(lookup_count):
        if identity.resolve(name=name, entity_type="Platform").method == "alias":
            exact_hits += 1
    id_secs = time.perf_counter() - start
    id_rate = lookup_count / id_secs if id_secs else float("inf")

    # 2. projection throughput (full replay)
    log = EventLog(log_path)
    start = time.perf_counter()
    model, proj_stats = replay_log(log)
    proj_secs = time.perf_counter() - start
    proj_rate = total_events / proj_secs

    # 3. query latency on the warm read model
    step = max(1, n_entities // runs)
    probe_indices = range(0, n_entities, step)
    probe_ids = [f"urn:world:entity:bench-{index:08d}" for index in probe_indices][:runs]
    as_of = parse_instant("2026-08-12T00:00:00Z")

    def measure(fn) -> dict:
        timings = []
        for entity_id in probe_ids:
            fn(entity_id)  # warm this probe before timing
            begin = time.perf_counter_ns()
            for _ in range(QUERY_REPETITIONS):
                fn(entity_id)
            elapsed_ns = time.perf_counter_ns() - begin
            timings.append(elapsed_ns / QUERY_REPETITIONS / 1_000_000.0)
        return {
            "runs": len(timings),
            "repetitions_per_sample": QUERY_REPETITIONS,
            "p50_ms": round(percentile(timings, 0.50), 4),
            "p95_ms": round(percentile(timings, 0.95), 4),
            "p99_ms": round(percentile(timings, 0.99), 4),
        }

    query_latencies = {
        "q1_entity_lookup_ms": measure(model.get_entity),
        "q_current_location_ms": measure(model.current_location),
        "q4_temporal_ms": measure(lambda eid: model.location_as_of(eid, as_of)),
    }

    log_fingerprint = log.fingerprint()
    report = {
        "scale": {
            "entities": n_entities,
            "observations_per_entity": observations_per,
            "total_events": total_events,
            "seed": 42,
        },
        "ingestion": {
            "synthetic_ingest_events_per_s": round(synthetic_ingest_rate),
            "synthetic_ingest_secs": round(synthetic_ingest_secs, 4),
            "identity_lookup_per_s": round(id_rate),
            "identity_exact_alias_hits": exact_hits,
            "identity_lookup_count": lookup_count,
            "slo_reference_events_per_s": "10^4 - 10^5",
        },
        "projection": {
            "events_per_s": round(proj_rate),
            "secs": round(proj_secs, 4),
            "index_build_secs": round(proj_secs, 4),
            "applied": proj_stats.applied,
            "skipped": proj_stats.skipped,
            "read_model": model.stats(now=parse_instant("2099-01-01T00:00:00Z")),
        },
        "recovery": {
            "full_replay_secs": round(proj_secs, 4),
            "scope": "fresh in-memory projection; excludes snapshot restore",
        },
        "resources": {
            "peak_rss_bytes": _peak_rss_bytes(),
            "peak_rss_scope": "process high-water mark; run standalone for comparison",
            "event_log_bytes": sum(size for _, size, _ in log_fingerprint),
            "event_log_segments": len(log_fingerprint),
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
    for segment in out_dir.glob("benchmark-events-*.jsonl"):
        segment.unlink(missing_ok=True)
    return report


def _peak_rss_bytes() -> int:
    """Return process max RSS in bytes on macOS and Linux."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def print_report(report: dict) -> None:
    """Human-readable summary table."""
    scale = report["scale"]
    print(
        f"\nBenchmark: {scale['total_events']} events "
        f"({scale['entities']} entities x {scale['observations_per_entity']} observations)\n"
    )
    ing = report["ingestion"]
    print(f"  synthetic ingest: {ing['synthetic_ingest_events_per_s']:>7,} events/s")
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
