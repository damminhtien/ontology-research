"""Tests for the profile staying on shared ontology vocabulary."""

from __future__ import annotations

import json

from check_vietnam_profile import (
    FIXTURE_PATH,
    PROFILE_PATH,
    PROFILE_ROOT,
    REPO_ROOT,
    _load_graph,
    validate_profile,
)
from ontology_utils import find_module_files, materialize_type_closure
from pyshacl import validate
from rdflib import RDF, Graph, URIRef


def test_vietnam_profile_reuses_registered_terms():
    ontology = _load_graph(find_module_files())
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))

    assert validate_profile(profile, ontology) == []


def test_vietnam_profile_rejects_a_new_local_city_class():
    ontology = _load_graph(find_module_files())
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    profile["entity_type_mappings"]["administrative_region"] = (
        "https://example.test/vietnam#VietnameseCityClass"
    )

    errors = validate_profile(profile, ontology)

    assert any("not a declared OWL class" in error for error in errors)
    assert any("must reuse the shared ontology class" in error for error in errors)


def test_role_assignment_cannot_reclassify_an_agent():
    data = _load_graph(find_module_files())
    data.parse(FIXTURE_PATH.as_posix(), format="turtle")
    role = URIRef("https://data.example/vietnam-profile/role")
    data.remove((role, RDF.type, None))
    data.add(
        (
            role,
            RDF.type,
            URIRef("https://damminhtien.github.io/ontology-research/ontology/core#Agent"),
        )
    )
    materialize_type_closure(data)
    shapes = Graph()
    for path in (
        REPO_ROOT / "shapes" / "assertion_shapes.ttl",
        PROFILE_ROOT / "affiliation_shapes.ttl",
    ):
        shapes.parse(path.as_posix(), format="turtle")

    conforms, _results, report = validate(data_graph=data, shacl_graph=shapes, advanced=True)

    assert not conforms
    assert "role assignment" in report.lower()
