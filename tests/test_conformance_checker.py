"""Regression tests for the cross-engine ontology conformance matrix."""

from __future__ import annotations

from check_conformance import (
    REPO_ROOT,
    _bad_fixture,
    _canonical_export_data,
    _interop_export_graph,
    _interop_negative_fixtures,
    _shacl_group_paths,
)
from pyshacl import validate
from rdflib import Graph


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


def test_interop_export_and_negative_fixtures_validate_with_pyshacl(tmp_path):
    data = _interop_export_graph(tmp_path)
    shapes = Graph().parse(REPO_ROOT / "shapes" / "interop_shapes.ttl")

    conforms, _, report = validate(
        data_graph=data,
        shacl_graph=shapes,
        inference="none",
        advanced=True,
    )
    assert conforms, report

    for fixture in _interop_negative_fixtures(data).values():
        conforms, _, _ = validate(
            data_graph=fixture,
            shacl_graph=shapes,
            inference="none",
            advanced=True,
        )
        assert not conforms


def test_canonical_export_conforms_to_the_canonical_shape_groups(tmp_path):
    data = _canonical_export_data(tmp_path)
    shapes = Graph()
    for shape_path in _shacl_group_paths():
        shapes.parse(shape_path)

    conforms, _, report = validate(
        data_graph=data,
        shacl_graph=shapes,
        inference="none",
        advanced=True,
    )
    assert conforms, report
