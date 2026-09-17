"""End-to-end benchmark: measures every production stage, not just the hot path.

Architecture §4.8: the microbenchmark (tools/benchmark.py) measures in-memory
structures; this tool runs the *full* pipeline on a representative synthetic
distribution and reports per-stage throughput/latency:

  1. ingest          — records -> identity resolution + SHACL gate + event log
  2. lake_persist    — accepted events -> Parquet lake (with dedup)
  3. projector       — full log replay into the read model
  4. lake_query      — DuckDB analytical queries (p50/p95 over repeats)

The distribution is deliberately representative: bilingual VI/EN names,
re-statements of known entities (external-id hits), same-name distinct-QID
collisions (multimap ambiguity), and observations of never-minted references
(pending). Deterministic (fixed seed).

Usage:
    .venv/bin/python tools/benchmark_e2e.py [--entities 300] [--iterations 1]
    .venv/bin/python tools/benchmark_e2e.py --generate-baseline
    .venv/bin/python tools/benchmark_e2e.py --check          # regression gate
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (REPO_ROOT / "tools", REPO_ROOT):
    if _path.as_posix() not in sys.path:
        sys.path.insert(0, _path.as_posix())

from foundry.events import EventLog  # noqa: E402
from foundry.identity import IdentityService  # noqa: E402
from foundry.ingestion import IngestionPipeline  # noqa: E402
from foundry.lake import lake_query, persist_events  # noqa: E402

REPORT_PATH = REPO_ROOT / "build" / "benchmark-e2e-report.json"
BASELINE_PATH = REPO_ROOT / "benchmarks" / "baseline-e2e.json"
ONTOLOGY_PATH = REPO_ROOT / "ontology" / "core" / "core.ttl"
SHAPES_PATH = REPO_ROOT / "shapes" / "core_shapes.ttl"

# Absolute stage SLOs. The SHACL gate dominates ingest, so its floor is
# intentionally low; the other stages must never regress below these floors
# without a baseline discussion.
STAGE_SLOS = {
    "ingest": {"min_events_per_second": 20.0},
    "lake_persist": {"min_events_per_second": 150.0},
    "projector": {"min_events_per_second": 5000.0},
    "lake_query": {"max_p95_ms": 100.0},
}


@dataclass(frozen=True)
class StageResult:
    """One measured stage."""

    name: str
    count: int
    seconds: float
    extra: dict

    def to_dict(self) -> dict:
        """Serialize for the JSON report."""
        return {
            "count": self.count,
            "seconds": round(self.seconds, 4),
            "events_per_second": round(self.count / self.seconds, 1) if self.seconds else 0.0,
            **self.extra,
        }


def generate_records(n_entities: int, seed: int = 42) -> list[dict]:
    """Representative reference records: bilingual, duplicated, colliding."""
    rng = random.Random(seed)
    records: list[dict] = []
    for i in range(n_entities):
        qid = f"Q9{i:07d}"
        vi = f"Đơn vị thực tế số {i:04d}"
        en = f"Reference Entity {i:04d}"
        records.append({"qid": qid, "name": vi, "aliases": [en], "op": "create"})
        # 8% re-statements under a different registry id (external-id hit)
        if rng.random() < 0.08:
            records.append(
                {
                    "qid": qid,
                    "name": en,
                    "aliases": [vi],
                    "op": "restate",
                    "extra_src": f"alt-reg-{i}",
                }
            )
        # 2% same-surface-name collisions (distinct QIDs, identical name)
        if rng.random() < 0.02:
            records.append({"qid": f"Q9{i:07d}x", "name": vi, "aliases": [], "op": "collision"})
    return records


def observations_for(records: list[dict], n_pending: int) -> list[dict]:
    """Location observations for minted names + never-minted pending refs."""
    obs = []
    for i, record in enumerate(records):
        if record["op"] in {"create", "collision"}:
            obs.append(
                {
                    "entity_name": record["name"],
                    "location_uri": f"urn:world:location:bench-{i % 40:03d}",
                    "valid_from": f"2026-08-{1 + i % 28:02d}T03:00:00Z",
                }
            )
    for i in range(n_pending):  # never-minted references → pending observations
        obs.append(
            {
                "entity_name": f"Chưa mint {i:04d}",
                "location_uri": f"urn:world:location:pending-{i % 5:03d}",
                "valid_from": "2026-08-15T03:00:00Z",
            }
        )
    return obs


def run_ingest(records: list[dict], observations: list[dict], tmp: Path) -> StageResult:
    """Stage 1 — the full canonical ingestion path (identity + SHACL + log)."""
    pipeline = IngestionPipeline(
        identity=IdentityService(),
        log=EventLog(tmp / "e2e.jsonl"),
        ontology_path=ONTOLOGY_PATH,
        shapes_path=SHAPES_PATH,
    )
    accepted = rejected = pending = 0
    start = time.perf_counter()
    for record in records:
        result = pipeline.ingest_entity(
            name=record["name"],
            entity_type="Organization",
            source_id=f"wikidata:{record['qid']}",
            external_source="wikidata",
            external_id=record["qid"],
            aliases=record.get("aliases") or None,
        )
        if result.accepted:
            accepted += 1
            if result.pending:
                pending += 1
        else:
            rejected += 1
    for obs in observations:
        result = pipeline.ingest_location_observation(
            entity_name=obs["entity_name"],
            entity_type="Organization",
            location_uri=obs["location_uri"],
            valid_from=obs["valid_from"],
            source_ids=["https://data.example/source/e2e"],
        )
        if result.accepted:
            accepted += 1
            if result.pending:
                pending += 1
        else:
            rejected += 1
    seconds = time.perf_counter() - start
    total = len(records) + len(observations)
    return StageResult(
        "ingest",
        total,
        seconds,
        {"accepted": accepted, "rejected": rejected, "pending": pending},
    )


def run_lake_persist(log_path: Path, lake_root: Path) -> StageResult:
    """Stage 2 — accepted events into the Parquet lake."""
    events = EventLog(log_path).read_all()
    start = time.perf_counter()
    persist_events(events, lake_root)
    seconds = time.perf_counter() - start
    return StageResult("lake_persist", len(events), seconds, {})


def run_projector(log_path: Path) -> StageResult:
    """Stage 3 — full log replay into the read model."""
    from foundry.projector import Projector
    from foundry.readmodel import ReadModel

    events = EventLog(log_path).read_all()
    start = time.perf_counter()
    model = ReadModel()
    _stats = Projector(model).replay(events)
    seconds = time.perf_counter() - start
    return StageResult("projector", len(events), seconds, {"entities": model.stats()["entities"]})


def run_lake_query(lake_root: Path, runs: int = 30) -> StageResult:
    """Stage 4 — DuckDB analytical latency (p50/p95 over repeated runs)."""
    queries = [
        "SELECT count(*) FROM events",
        "SELECT event_type, count(*) AS n FROM events GROUP BY event_type ORDER BY n DESC",
        "SELECT event_date, count(*) AS n FROM events GROUP BY event_date",
    ]
    samples: list[float] = []
    for i in range(runs):
        sql = queries[i % len(queries)]
        start = time.perf_counter()
        lake_query(sql, root=lake_root)
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    p50 = samples[len(samples) // 2]
    p95 = samples[int(len(samples) * 0.95)]
    return StageResult(
        "lake_query",
        len(queries),
        sum(samples) / 1000.0,
        {"p50_ms": round(p50, 3), "p95_ms": round(p95, 3), "runs": runs},
    )


def run_e2e(n_entities: int, tmp: Path) -> dict:
    """One full end-to-end pass; returns the per-stage report."""
    records = generate_records(n_entities)
    observations = observations_for(records, n_pending=max(5, n_entities // 30))
    log_path = tmp / "e2e.jsonl"
    lake_root = tmp / "lake"

    stages: dict[str, dict] = {}
    for stage in (
        lambda: run_ingest(records, observations, tmp),
        lambda: run_lake_persist(log_path, lake_root),
        lambda: run_projector(log_path),
        lambda: run_lake_query(lake_root),
    ):
        result = stage()
        stages[result.name] = result.to_dict()

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entities": n_entities,
        "stages": stages,
        "stage_slos": STAGE_SLOS,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def _evaluate(report: dict, baseline: dict | None) -> list[str]:
    """Absolute SLOs first; then a 1/1.2x baseline ratio when one exists."""
    failures: list[str] = []
    stages = report["stages"]
    ingest_floor = STAGE_SLOS["ingest"]["min_events_per_second"]
    if stages["ingest"]["events_per_second"] < ingest_floor:
        failures.append(
            f"ingest: {stages['ingest']['events_per_second']} ev/s below floor {ingest_floor}"
        )
    for stage in ("lake_persist", "projector"):
        floor = STAGE_SLOS[stage]["min_events_per_second"]
        if stages[stage]["events_per_second"] < floor:
            failures.append(
                f"{stage}: {stages[stage]['events_per_second']} ev/s below floor {floor}"
            )
    p95 = stages["lake_query"]["p95_ms"]
    max_p95 = STAGE_SLOS["lake_query"]["max_p95_ms"]
    if p95 > max_p95:
        failures.append(f"lake_query: p95 {p95}ms exceeds SLO {max_p95}ms")
    if baseline:
        for stage, entry in baseline["stages"].items():
            if stage == "lake_query":
                continue  # latency-gated, not throughput-gated
            base = entry.get("events_per_second")
            if not base:
                continue
            cur = stages[stage]["events_per_second"]
            if cur / base < 1 / 1.2:
                failures.append(f"{stage}: {cur} ev/s under 1/1.2x of baseline {base} ev/s")
    return failures


def _median_report(n_entities: int, iterations: int) -> dict:
    """Median across iterations for stable gate numbers."""
    reports = []
    for _ in range(iterations):
        with tempfile.TemporaryDirectory() as tmp:
            reports.append(run_e2e(n_entities, Path(tmp)))
    report = reports[-1].copy()
    report["stages"] = {
        stage: {
            **entry,
            "events_per_second": round(
                statistics.median(r["stages"][stage]["events_per_second"] for r in reports), 1
            ),
        }
        for stage, entry in report["stages"].items()
    }
    report["stages"]["lake_query"]["p95_ms"] = round(
        statistics.median(r["stages"]["lake_query"]["p95_ms"] for r in reports), 3
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entities", type=int, default=300)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--generate-baseline", action="store_true")
    parser.add_argument(
        "--check", action="store_true", help="regression gate vs committed baseline"
    )
    args = parser.parse_args()

    if args.generate_baseline or args.check:
        # 5 median iterations: the gate must not trip on single-run machine noise
        report = _median_report(args.entities, max(5, args.iterations))
        print(json.dumps(report["stages"], indent=2, ensure_ascii=False))
        if args.generate_baseline:
            baseline = {"stage_slos": STAGE_SLOS, "stages": report["stages"]}
            BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
            BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
            print(f"baseline written to {BASELINE_PATH}")
            return 0
        baseline = (
            json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
            if BASELINE_PATH.exists()
            else None
        )
        failures = _evaluate(report, baseline)
        if failures:
            print("e2e SLO gate FAILED:")
            for failure in failures:
                print(f"  - {failure}")
            return 1
        print("e2e SLO gate PASSED")
        return 0

    with tempfile.TemporaryDirectory(prefix="e2e-bench-") as tmp:
        report = run_e2e(args.entities, Path(tmp))
    print(json.dumps(report["stages"], indent=2, ensure_ascii=False))
    print(f"report written to {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
