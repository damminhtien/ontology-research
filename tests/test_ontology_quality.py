"""Behavior checks for the ontology quality report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from check_ontology_quality import evaluate


def _write(root: Path, file: str, text: str) -> Path:
    path = root / file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def quality_root(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        "ontology/schema.ttl",
        """
        @prefix ex: <urn:example:> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        ex:Parent a owl:Class .
        ex:Child a owl:Class ; rdfs:subClassOf ex:Parent .
        ex:label a owl:DatatypeProperty ; rdfs:domain ex:Parent ; rdfs:range rdfs:Literal .
        """,
    )
    _write(
        tmp_path,
        "benchmarks/datasets/data.ttl",
        """
        @prefix ex: <urn:example:> .
        ex:example a ex:Child ; ex:label "example" .
        [] a ex:Child ; ex:label "anonymous example" .
        """,
    )
    _write(
        tmp_path,
        "shapes/constraints.ttl",
        """
        @prefix ex: <urn:example:> .
        @prefix sh: <http://www.w3.org/ns/shacl#> .
        ex:Covered a sh:NodeShape ; sh:targetClass ex:Parent, ex:Child ;
            sh:property [ sh:path ex:label ; sh:minCount 1 ] .
        ex:Uncovered a sh:NodeShape ; sh:targetClass ex:Absent .
        ex:Disabled a sh:NodeShape ; sh:targetClass ex:Child ; sh:deactivated true .
        """,
    )
    _write(
        tmp_path,
        "requirements/competency_questions.md",
        """| CQ-001 | Q1 | Count explicit parents. | n/a | Required |
| CQ-002 | Q1 | Pending question. | n/a | Backlog |""",
    )
    _write(
        tmp_path,
        "benchmarks/queries/count.rq",
        "SELECT (COUNT(?s) AS ?count) WHERE { ?s a <urn:example:Parent> }",
    )
    _write(
        tmp_path,
        "benchmarks/expected_results/count.json",
        json.dumps(
            {
                "id": "CQ-001",
                "group": "Q1",
                "query_file": "count.rq",
                "variables": ["count"],
                "rows": [[0]],
            }
        ),
    )
    return tmp_path


def test_reports_vacuous_shapes_and_unimplemented_questions(quality_root: Path):
    report = evaluate(quality_root)

    assert report["passed"]
    assert report["shacl"]["class_target_shapes"] == 2
    assert report["shacl"]["exercised_class_target_shapes"] == 1
    coverage = {
        row["shape"]: row["focus_nodes"] for row in report["shacl"]["class_target_coverage"]
    }
    assert coverage == {"urn:example:Covered": 2, "urn:example:Uncovered": 0}
    assert report["competency"]["passed"] == 1
    assert report["competency"]["backlog"] == 1
    assert report == evaluate(quality_root)


def test_reports_schema_shacl_and_missing_cq_evidence_together(quality_root: Path):
    schema_path = quality_root / "ontology/schema.ttl"
    schema_path.write_text(
        schema_path.read_text(encoding="utf-8").replace(" ; rdfs:range rdfs:Literal", ""),
        encoding="utf-8",
    )
    _write(
        quality_root,
        "benchmarks/datasets/data.ttl",
        "<urn:example:incomplete> a <urn:example:Child> .",
    )
    (quality_root / "benchmarks/expected_results/count.json").unlink()

    report = evaluate(quality_root)

    assert not report["passed"]
    assert list(report["schema"]["missing_domain_or_range"]) == ["urn:example:label"]
    assert not report["shacl"]["conforms"]
    violation = report["shacl"]["violations"][0]
    assert violation["focus_node"] == "urn:example:incomplete"
    assert violation["path"] == "urn:example:label"
    assert report["competency"]["questions"][0]["error"] == "Missing expected result"


def test_cq_answer_mismatch_and_duplicate_spec_fail_gate(quality_root: Path):
    _write(
        quality_root,
        "benchmarks/datasets/explicit_parent.ttl",
        '<urn:example:parent> a <urn:example:Parent> ; <urn:example:label> "parent" .',
    )
    spec_dir = quality_root / "benchmarks/expected_results"
    (spec_dir / "duplicate.json").write_bytes((spec_dir / "count.json").read_bytes())

    report = evaluate(quality_root)

    assert not report["passed"]
    assert report["shacl"]["conforms"]
    assert report["competency"]["contract_errors"] == [
        "duplicate.json: duplicate expected result for CQ-001"
    ]
    assert "rows mismatch" in report["competency"]["questions"][0]["error"]
