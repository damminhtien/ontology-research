"""Regression tests for the cross-engine ontology conformance matrix."""

from __future__ import annotations

from check_conformance import REPO_ROOT, _bad_fixture, _shacl_group_paths


def test_conformance_matrix_covers_the_four_contract_groups_only():
    paths = _shacl_group_paths()
    assert {path.name for path in paths} == {
        "core_shapes.ttl",
        "identity_shapes.ttl",
        "domain_shapes.ttl",
        "assertion_shapes.ttl",
    }
    assert (REPO_ROOT / "shapes" / "interop_shapes.ttl").is_file()
    assert all(_bad_fixture(path.name) for path in paths)
