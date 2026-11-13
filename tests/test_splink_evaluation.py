"""Safety and reproducibility contracts for the isolated Splink evaluation."""

from __future__ import annotations

import json
import math

import pytest
from evaluate_splink import (
    Candidate,
    _choose_threshold,
    _filter_safe,
    _recommendations,
    _synthetic_dataset,
    _unestimated_parameters,
    split_by_entity,
)


def test_splits_are_entity_disjoint_and_scoring_rows_have_no_labels_or_trusted_ids():
    """Ground-truth IDs and external identifiers stay outside model rows."""
    bundle = _synthetic_dataset(90, seed=42)
    splits = split_by_entity(bundle, seed=42)

    truth_sets = [
        {split.truth_by_key[key] for key in split.truth_by_key} for split in splits.values()
    ]
    assert not (truth_sets[0] & truth_sets[1])
    assert not (truth_sets[0] & truth_sets[2])
    assert not (truth_sets[1] & truth_sets[2])
    for split in splits.values():
        for row in (*split.left, *split.right):
            assert set(row) == {"row_key", "name", "alias", "region"}


def test_synthetic_fixture_contains_missing_ids_aliases_types_and_cannot_links():
    """The held-out synthetic data exercises multilingual and safety cases."""
    bundle = _synthetic_dataset(120, seed=42)
    split = split_by_entity(bundle, seed=42)["test"]
    assert any(value is None for value in split.trusted_id_by_key.values())
    assert len(set(split.type_by_key.values())) > 1
    assert len(split.cannot_link_pairs) > 0
    assert any(
        "Nguyễn" in alias for aliases in split.resolver_aliases_by_key.values() for alias in aliases
    )


def test_safety_filter_removes_wrong_type_and_cannot_link_pairs():
    """Unsafe candidate pairs are removed before any recommendation selection."""
    bundle = _synthetic_dataset(90, seed=42)
    split = split_by_entity(bundle, seed=42)["test"]
    cannot_link = next(iter(split.cannot_link_pairs))
    left_row = split.left[0]
    right_by_key = {row["row_key"]: row for row in split.right}
    mismatch = next(
        (left_row["row_key"], row["row_key"])
        for row in split.right
        if split.type_by_key[left_row["row_key"]] != split.type_by_key[row["row_key"]]
    )
    raw = [Candidate(*cannot_link, score=0.99), Candidate(*mismatch, score=0.98)]

    safe, dropped = _filter_safe(raw, split)
    assert safe == []
    assert dropped == 2
    assert cannot_link[1] in right_by_key


def test_review_budget_caps_each_source_record_and_threshold_is_validation_score():
    """Recommendations respect top five and threshold selection uses supplied labels."""
    candidates = [
        Candidate("left-1", f"right-{index}", score=score)
        for index, score in enumerate((0.99, 0.8, 0.7, 0.6, 0.5, 0.4))
    ]
    truth = frozenset({("left-1", "right-0"), ("left-1", "right-2")})

    threshold = _choose_threshold(candidates, truth, budget=5)
    recommendations = _recommendations(candidates, threshold, budget=5)
    assert threshold is not None
    assert len(recommendations) <= 5
    assert math.isclose(threshold, 0.7)


def test_saved_model_parameter_gate_detects_unestimated_levels(tmp_path):
    """The benchmark refuses models that would silently use default m/u values."""
    model = {
        "comparisons": [
            {
                "output_column_name": "name",
                "comparison_levels": [
                    {"is_null_level": True},
                    {
                        "label_for_charts": "exact",
                        "m_probability": 0.8,
                        "u_probability": 0.1,
                    },
                    {"label_for_charts": "fuzzy", "m_probability": 0.2},
                ],
            }
        ]
    }
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(model), encoding="utf-8")

    assert _unestimated_parameters(path) == ["name/fuzzy/u_probability"]


def test_splink_can_score_only_a_registered_resolver_candidate_set():
    """Splink 5 can rerank supplied pairs without expanding the candidate set."""
    pytest.importorskip("splink")
    import splink.comparison_library as cl
    from splink import DuckDBAPI, Linker, SettingsCreator

    api = DuckDBAPI()
    left = api.register([{"row_key": "a1", "name": "alpha unit"}], dataset_display_name="records_a")
    right = api.register(
        [
            {"row_key": "b1", "name": "alpha unit"},
            {"row_key": "b2", "name": "different unit"},
        ],
        dataset_display_name="records_b",
    )
    linker = Linker(
        [left, right],
        SettingsCreator(
            unique_id_column_name="row_key",
            link_type="link_only",
            comparisons=[cl.NameComparison("name")],
            blocking_rules_to_generate_predictions=[],
        ),
        log_level=40,
    )
    pairs = api.register(
        [
            {
                "match_key": "resolver",
                "join_key_l": "records_a-__-a1",
                "join_key_r": "records_b-__-b2",
            }
        ],
        dataset_display_name="resolver_pairs",
    )
    linker.table_management.register_blocked_pairs_for_predict(pairs)

    output = linker.inference.predict(warning_mode="never").as_record_list()
    assert [(row["row_key_l"], row["row_key_r"]) for row in output] == [("a1", "b2")]
