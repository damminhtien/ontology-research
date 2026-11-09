"""Regression tests for the ontology design gate and its report."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from check_ontology_design import evaluate, main
from conftest import REPO_ROOT
from rdflib import OWL, RDFS, Graph, Namespace, URIRef

CORE = "ontology/core/core.ttl"
TRACKING = "ontology/domain/tracking.ttl"
BASE = "https://damminhtien.github.io/ontology-research/ontology"
CORE_NS = Namespace(f"{BASE}/core#")
TRACKING_NS = Namespace(f"{BASE}/domain/tracking#")


@pytest.fixture
def design_root(tmp_path: Path) -> Path:
    shutil.copytree(REPO_ROOT / "ontology", tmp_path / "ontology")
    return tmp_path


def _append(root: Path, module: str, triples: str) -> None:
    with (root / module).open("a", encoding="utf-8") as stream:
        stream.write("\n" + triples + "\n")


def _replace(
    root: Path,
    module: str,
    triple: tuple[URIRef, URIRef, URIRef],
    new_object: URIRef | None = None,
) -> None:
    path = root / module
    graph = Graph().parse(path, format="turtle")
    assert triple in graph
    graph.remove(triple)
    if new_object is not None:
        graph.add((triple[0], triple[1], new_object))
    graph.serialize(path, format="turtle")


def test_valid_schema_reports_advisory_warnings_and_stable_json(design_root: Path):
    _append(design_root, CORE, "core:Unlabelled a owl:Class .")
    _replace(
        design_root,
        CORE,
        (CORE_NS.memberOf, RDFS.domain, CORE_NS.Agent),
        CORE_NS.Organization,
    )
    output = design_root / "build/report.json"
    assert main(["--output", str(output)], root=design_root) == 0
    first = output.read_bytes()
    assert main(["--output", str(output)], root=design_root) == 0
    assert output.read_bytes() == first

    report = json.loads(first)
    assert report["passed"]
    assert not report["errors"]
    assert {"missing-label", "missing-description", "inverse-signature"} <= {
        row["code"] for row in report["warnings"]
    }
    core = next(row for row in report["modules"] if row["file"] == CORE)
    assert core["with_label"] == core["terms"] - 1


def test_asserted_cycles_fail_and_still_emit_report(design_root: Path):
    _append(
        design_root,
        CORE,
        "core:Agent rdfs:subClassOf core:Person . core:Facility rdfs:subClassOf core:Facility .",
    )
    output = design_root / "build/report.json"
    assert main(["--output", str(output)], root=design_root) == 1

    report = json.loads(output.read_bytes())
    assert not report["passed"]
    cycles = [row for row in report["errors"] if row["code"] == "subclass-cycle"]
    assert {tuple(row["iris"]) for row in cycles} == {
        (f"{BASE}/core#Agent", f"{BASE}/core#Person"),
        (f"{BASE}/core#Facility",),
    }
    assert all(row["files"] == [CORE] for row in cycles)


def test_empty_classes_follow_named_equivalence_and_subclasses(design_root: Path):
    _append(
        design_root,
        TRACKING,
        """
        tracking:EventAlias a owl:Class ; owl:equivalentClass core:Event .
        tracking:ImpossibleTrack a owl:Class ; rdfs:subClassOf tracking:Track, tracking:EventAlias .
        tracking:Never a owl:Class ; rdfs:subClassOf owl:Nothing .
        """,
    )

    report = evaluate(design_root)
    assert {row["code"] for row in report["errors"]} == {"empty-class"}
    by_iri = {row["iris"][0]: row for row in report["errors"]}
    collision = by_iri[f"{BASE}/domain/tracking#ImpossibleTrack"]
    assert collision["files"] == [CORE, TRACKING]
    assert any("equivalentClass" in line for line in collision["evidence"])
    assert any(
        "owl#Nothing" in line for line in by_iri[f"{BASE}/domain/tracking#Never"]["evidence"]
    )


def test_import_visibility_and_declaration_kind(design_root: Path):
    _replace(
        design_root,
        TRACKING,
        (URIRef(f"{BASE}/domain/tracking"), OWL.imports, URIRef(f"{BASE}/core")),
        URIRef(f"{BASE}/middle/assertion"),
    )
    assert evaluate(design_root)["passed"]  # core is available through assertion
    _append(
        design_root,
        TRACKING,
        f"""
        tracking:Track rdfs:subClassOf <{BASE}/domain/sensor#Sensor> .
        tracking:Track rdfs:subClassOf core:hasPart .
        tracking:Track rdfs:subClassOf core:Typo .
        <{BASE}/domain/tracking> owl:imports <https://example.invalid/ontology> .
        """,
    )
    report = evaluate(design_root)
    assert {row["code"]: row["iris"] for row in report["errors"]} == {
        "missing-import": [f"{BASE}/domain/sensor#Sensor"],
        "wrong-term-kind": [f"{BASE}/core#hasPart"],
        "undeclared-term": [f"{BASE}/core#Typo"],
        "unresolved-import": [f"{BASE}/domain/tracking"],
    }


def test_checks_expected_kinds_in_property_axioms(design_root: Path):
    _append(
        design_root,
        CORE,
        """
        core:hasPart rdfs:domain core:hasMember .
        core:name rdfs:range core:Entity .
        core:hasPart owl:inverseOf core:name .
        """,
    )
    report = evaluate(design_root)
    assert {row["code"] for row in report["errors"]} == {"wrong-term-kind"}
    assert {tuple(row["iris"]) for row in report["errors"]} == {
        (f"{BASE}/core#hasMember",),
        (f"{BASE}/core#Entity",),
        (f"{BASE}/core#name",),
    }


def test_missing_design_invariants_fail(design_root: Path):
    _replace(
        design_root,
        TRACKING,
        (TRACKING_NS.Track, RDFS.subClassOf, CORE_NS.InformationObject),
        CORE_NS.Entity,
    )
    _replace(design_root, CORE, (CORE_NS.InformationObject, OWL.disjointWith, CORE_NS.Event))

    report = evaluate(design_root)
    assert {row["code"] for row in report["errors"]} == {"required-subclass", "required-disjoint"}


def test_invalid_turtle_is_an_execution_error(design_root: Path, capsys: pytest.CaptureFixture):
    _append(design_root, CORE, "not valid Turtle")
    output = design_root / "build/report.json"
    assert main(["--output", str(output)], root=design_root) == 2
    assert not output.exists()
    assert "core.ttl" in capsys.readouterr().err
