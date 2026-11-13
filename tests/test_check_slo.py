"""Tests for portable read-model SLO regression comparisons."""

from __future__ import annotations

import check_slo


def _baseline() -> dict:
    return {
        "query_latency": {
            name: {"p95_ms": measured, "slo_ms": check_slo.SLO_REFERENCE[name]}
            for name, measured in {
                "q1_entity_lookup_ms": 0.0005,
                "q_current_location_ms": 0.0672,
                "q4_temporal_ms": 0.0675,
            }.items()
        }
    }


def _report(q1_p95_ms: float) -> dict:
    return {
        "query_latency": {
            "q1_entity_lookup_ms": {"p95_ms": q1_p95_ms},
            "q_current_location_ms": {"p95_ms": 0.0038},
            "q4_temporal_ms": {"p95_ms": 0.0040},
        }
    }


def test_q1_runner_jitter_uses_portable_relative_floor(monkeypatch, capsys):
    monkeypatch.setattr(check_slo, "_load_baseline", _baseline)
    monkeypatch.setattr(check_slo, "_median_report", lambda: _report(0.0012))

    assert check_slo.check() == 0
    output = capsys.readouterr().out
    assert "Gate base" in output
    assert "portable relative-comparison floor 0.0038ms" in output

    monkeypatch.setattr(check_slo, "_median_report", lambda: _report(0.0047))
    assert check_slo.check() == 1
    assert "comparison baseline 0.0038ms" in capsys.readouterr().out


def test_other_queries_keep_the_measured_relative_baseline():
    assert check_slo._comparison_baseline_ms("q_current_location_ms", 0.0672) == 0.0672
