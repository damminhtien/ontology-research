"""Smoke tests for the benchmark harness (runs tiny scale, fast)."""

from __future__ import annotations

import json

from benchmark import generate_events, run_benchmark


class TestEventGenerator:
    def test_deterministic_for_fixed_seed(self):
        first = generate_events(5, 2, seed=42)
        second = generate_events(5, 2, seed=42)
        # event ids differ per call (uuid4) but payload content must match
        assert [e.payload for e in first] == [e.payload for e in second]
        assert [e.event_type for e in first] == [e.event_type for e in second]

    def test_scale_counts(self):
        events = generate_events(10, 3)
        assert len(events) == 10 + 30
        assert sum(1 for e in events if e.event_type == "EntityCreated") == 10


class TestBenchmarkReport:
    def test_report_shape_and_slo_flags(self, tmp_path):
        report = run_benchmark(n_entities=30, observations_per=2, runs=20, out_dir=tmp_path)

        assert report["scale"]["total_events"] == 90
        assert report["ingestion"]["raw_append_events_per_s"] > 0
        assert report["projection"]["applied"] == 90
        assert report["projection"]["skipped"] == 0

        for name in ("q1_entity_lookup_ms", "q_current_location_ms", "q4_temporal_ms"):
            metric = report["query_latency"][name]
            assert metric["runs"] == 20
            assert 0 <= metric["p50_ms"] <= metric["p95_ms"] <= metric["p99_ms"]
            comparison = report["slo_comparison"][name]
            assert set(comparison) == {"p95_ms", "slo_ms", "within_slo"}

        written = json.loads((tmp_path / "benchmark-report.json").read_text(encoding="utf-8"))
        assert written["scale"]["seed"] == 42

    def test_read_model_is_consistent_after_benchmark(self, tmp_path):
        report = run_benchmark(n_entities=30, observations_per=2, runs=20, out_dir=tmp_path)
        model_stats = report["projection"]["read_model"]
        assert model_stats["entities"] == 30
        assert model_stats["with_location"] == 30
