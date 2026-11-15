"""Tests for the end-to-end benchmark (architecture §4.8)."""

from __future__ import annotations

import benchmark_e2e as e2e


class TestStageGeneration:
    def test_records_are_deterministic_and_representative(self):
        first = e2e.generate_records(50)
        second = e2e.generate_records(50)
        assert first == second
        ops = [r["op"] for r in first]
        assert ops.count("create") >= 40
        assert any(op == "restate" for op in ops)  # external-id hits
        assert any(op == "collision" for op in ops)  # multimap ambiguity
        # bilingual: every create carries a VI name and an EN alias
        for record in first:
            if record["op"] == "create":
                assert record["name"].startswith("Đơn vị")
                assert record["aliases"][0].startswith("Reference Entity")

    def test_observations_include_pending_refs(self):
        records = e2e.generate_records(20)
        obs = e2e.observations_for(records, n_pending=5)
        names = {o["entity_name"] for o in obs}
        assert sum(1 for n in names if n.startswith("Chưa mint")) == 5


class TestRunE2E:
    def test_full_pass_reports_all_stages(self, tmp_path):
        report = e2e.run_e2e(30, tmp_path)
        stages = report["stages"]
        assert set(stages) == {"ingest", "lake_persist", "projector", "lake_query"}
        assert stages["ingest"]["accepted"] > 0
        assert stages["ingest"]["pending"] > 0  # pending observations exercised
        assert stages["projector"]["entities"] > 0
        assert stages["projector"]["replay_passes"] > 0
        assert stages["projector"]["warmup_passes"] == e2e.PROJECTOR_WARMUP_PASSES
        assert stages["projector"]["seconds"] >= e2e.PROJECTOR_MIN_MEASURE_SECONDS
        assert stages["projector"]["count"] == (
            stages["projector"]["events_per_pass"] * stages["projector"]["replay_passes"]
        )
        assert 0 < stages["lake_query"]["p95_ms"] < e2e.STAGE_SLOS["lake_query"]["max_p95_ms"] * 5
        # the report also landed at the committed path
        assert e2e.REPORT_PATH.exists()

    def test_evaluate_passes_a_healthy_report(self, tmp_path):
        report = e2e.run_e2e(30, tmp_path)
        assert e2e._evaluate(report, baseline=None) == []


class TestGate:
    def test_evaluate_flags_stages_below_floor(self):
        report = {
            "stages": {
                "ingest": {"events_per_second": 1.0},
                "lake_persist": {"events_per_second": 10.0},
                "projector": {"events_per_second": 2.0},
                "lake_query": {"p95_ms": 999.0},
            }
        }
        failures = e2e._evaluate(report, baseline=None)
        assert len(failures) == 4
        assert any("ingest" in f for f in failures)
        assert any("lake_query" in f for f in failures)

    def test_evaluate_flags_baseline_regression(self):
        report = {
            "stages": {
                "ingest": {"events_per_second": 80.0},
                "lake_persist": {"events_per_second": 300.0},
                "projector": {"events_per_second": 10000.0},
                "lake_query": {"p95_ms": 20.0},
            }
        }
        baseline = {
            "stages": {
                "ingest": {"events_per_second": 150.0},  # current is half the baseline
                "lake_persist": {"events_per_second": 300.0},
                "projector": {"events_per_second": 10000.0},
                "lake_query": {"p95_ms": 20.0},
            }
        }
        failures = e2e._evaluate(report, baseline=baseline)
        assert len(failures) == 1
        assert "ingest" in failures[0]

    def test_median_report_keeps_rate_and_seconds_consistent(self, monkeypatch):
        durations = iter((0.01, 0.02, 0.03))

        def fake_run_e2e(_n_entities, _tmp):
            seconds = next(durations)
            stages = {
                name: {
                    "count": 10,
                    "seconds": seconds,
                    "events_per_second": 10 / seconds,
                    "p50_ms": 4.0,
                    "p95_ms": 5.0,
                }
                for name in ("ingest", "lake_persist", "projector", "lake_query")
            }
            return {"stages": stages}

        monkeypatch.setattr(e2e, "run_e2e", fake_run_e2e)
        report = e2e._median_report(1, 3)

        projector = report["stages"]["projector"]
        assert projector["events_per_second"] == 500.0
        assert projector["seconds"] == 0.02

    def test_median_report_uses_matching_dynamic_projector_sample(self, monkeypatch):
        projector_samples = iter(
            (
                {"count": 1000, "seconds": 1.0, "events_per_second": 1000.0},
                {"count": 600, "seconds": 1.0, "events_per_second": 600.0},
                {"count": 800, "seconds": 1.0, "events_per_second": 800.0},
                {"count": 1200, "seconds": 1.0, "events_per_second": 1200.0},
                {"count": 400, "seconds": 1.0, "events_per_second": 400.0},
            )
        )

        def fake_run_e2e(_n_entities, _tmp):
            projector = next(projector_samples)
            stages = {
                name: {"count": 10, "seconds": 1.0, "events_per_second": 10.0}
                for name in ("ingest", "lake_persist", "projector", "lake_query")
            }
            stages["projector"] = projector
            stages["lake_query"].update({"p50_ms": 5.0, "p95_ms": 8.0})
            return {"stages": stages}

        monkeypatch.setattr(e2e, "run_e2e", fake_run_e2e)
        report = e2e._median_report(1, 5)

        projector = report["stages"]["projector"]
        assert projector["events_per_second"] == 800.0
        assert projector["count"] == 800
        assert projector["seconds"] == 1.0
        assert projector["count"] / projector["seconds"] == projector["events_per_second"]
