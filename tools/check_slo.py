"""SLO regression gate for the read-model benchmark.

Compares the current p95 latency of the three hot queries against a committed
baseline and the absolute SLO targets from requirements/performance_slo.md.

Usage:
    .venv/bin/python tools/check_slo.py
    .venv/bin/python tools/check_slo.py --generate-baseline
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if (REPO_ROOT / "tools").as_posix() not in sys.path:
    sys.path.insert(0, (REPO_ROOT / "tools").as_posix())

from benchmark import SLO_REFERENCE, run_benchmark  # noqa: E402

BASELINE_PATH = REPO_ROOT / "benchmarks" / "baseline.json"

# Keep in sync with the parameters used to generate the committed baseline.
SCALE = 500
OBSERVATIONS = 3
RUNS = 200
ITERATIONS = 5


def _median_report() -> dict:
    """Run the benchmark several times and return a report with median latencies."""
    reports = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for _ in range(ITERATIONS):
            reports.append(run_benchmark(SCALE, OBSERVATIONS, RUNS, tmp_path))

    # Use the last report for scalar values; median the latency percentiles.
    report = reports[-1].copy()
    report["query_latency"] = {
        name: {
            metric: round(statistics.median(r["query_latency"][name][metric] for r in reports), 4)
            for metric in ("p50_ms", "p95_ms", "p99_ms")
        }
        for name in SLO_REFERENCE
    }
    return report


def generate_baseline() -> None:
    """Run the benchmark several times and persist a median baseline."""
    report = _median_report()

    baseline = {
        "scale": {
            "entities": SCALE,
            "observations_per_entity": OBSERVATIONS,
            "runs": RUNS,
            "iterations": ITERATIONS,
            "seed": 42,
        },
        "query_latency": {
            name: {
                "p95_ms": report["query_latency"][name]["p95_ms"],
                "slo_ms": SLO_REFERENCE[name],
            }
            for name in SLO_REFERENCE
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }

    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    print(f"Wrote baseline to {BASELINE_PATH}")
    _print_table(baseline["query_latency"], baseline["query_latency"])


def _load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        raise FileNotFoundError(
            f"baseline not found at {BASELINE_PATH}; "
            "run `python tools/check_slo.py --generate-baseline`"
        )
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _print_table(current: dict, baseline: dict) -> None:
    print(f"\n{'Query':<26} {'Baseline':>10} {'Current':>10} {'Ratio':>8} {'SLO':>8} {'Status':>8}")
    print("-" * 78)
    for name in SLO_REFERENCE:
        base = baseline[name]["p95_ms"]
        cur = current[name]["p95_ms"]
        ratio = cur / base if base else float("inf")
        slo = baseline[name]["slo_ms"]
        status = "OK" if ratio <= 1.2 and cur <= slo else "FAIL"
        print(f"{name:<26} {base:>10.4f} {cur:>10.4f} {ratio:>7.2f}x {slo:>8} {status:>8}")


def check() -> int:
    """Run the benchmark several times and compare median p95 to baseline."""
    baseline = _load_baseline()
    report = _median_report()

    current = {
        name: {"p95_ms": report["query_latency"][name]["p95_ms"], "slo_ms": SLO_REFERENCE[name]}
        for name in SLO_REFERENCE
    }
    _print_table(current, baseline["query_latency"])

    failures = []
    for name in SLO_REFERENCE:
        base_p95 = baseline["query_latency"][name]["p95_ms"]
        cur_p95 = current[name]["p95_ms"]
        slo_ms = baseline["query_latency"][name]["slo_ms"]
        if base_p95 and cur_p95 / base_p95 > 1.2:
            failures.append(f"{name}: p95 {cur_p95:.4f}ms > 1.2x baseline {base_p95:.4f}ms")
        if cur_p95 > slo_ms:
            failures.append(f"{name}: p95 {cur_p95:.4f}ms exceeds SLO {slo_ms}ms")

    if failures:
        print("\nSLO regression gate FAILED:")
        for msg in failures:
            print(f"  - {msg}")
        return 1

    print("\nSLO regression gate PASSED")
    return 0


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generate-baseline",
        action="store_true",
        help="run the benchmark and write a new committed baseline",
    )
    args = parser.parse_args()

    if args.generate_baseline:
        generate_baseline()
        return 0
    return check()


if __name__ == "__main__":
    sys.exit(main())
