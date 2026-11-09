"""Evaluate the versioned ontology and benchmark corpus; emit a JSON report."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from cq_runner import REPO_ROOT, run_query_spec
from ontology_utils import dump_json, materialize_type_closure
from pyshacl import validate as shacl_validate
from rdflib import RDF, RDFS, BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, SH
from validate import load

CQ_ROW = re.compile(r"\|\s*(CQ-\d+)\s*\|\s*(Q[1-6])\s*\|.+\|\s*(Required|Backlog)\s*\|")


def _schema_report(graph: Graph) -> dict:
    classes = set(graph.subjects(RDF.type, OWL.Class))
    properties = set(graph.subjects(RDF.type, OWL.ObjectProperty)) | set(
        graph.subjects(RDF.type, OWL.DatatypeProperty)
    )
    classes = {term for term in classes if isinstance(term, URIRef)}
    properties = {term for term in properties if isinstance(term, URIRef)}
    missing = {
        str(prop): [
            str(field) for field in (RDFS.domain, RDFS.range) if (prop, field, None) not in graph
        ]
        for prop in sorted(properties)
    }
    missing = {prop: fields for prop, fields in missing.items() if fields}
    return {
        "classes": len(classes),
        "properties": len(properties),
        "properties_with_domain_and_range": len(properties) - len(missing),
        "missing_domain_or_range": missing,
    }


def _term_text(term: object) -> str:
    # Blank node IDs are local to one parse, and would make reports unstable.
    if isinstance(term, BNode):
        return "_:blank"
    return str(term) if term is not None else ""


def _shacl_report(data: Graph, shapes: Graph) -> dict:
    materialize_type_closure(data)
    coverage = []
    for shape in set(shapes.subjects(SH.targetClass)):
        if Literal(True) in shapes.objects(shape, SH.deactivated):
            continue
        targets = set(shapes.objects(shape, SH.targetClass))
        focus_nodes = {
            node
            for target in targets
            for cls in data.transitive_subjects(RDFS.subClassOf, target)
            for node in data.subjects(RDF.type, cls)
        }
        coverage.append(
            {
                "shape": _term_text(shape),
                "target_classes": sorted(str(target) for target in targets),
                "focus_nodes": len(focus_nodes),
            }
        )
    conforms, result, _ = shacl_validate(
        data_graph=data, shacl_graph=shapes, inference="none", advanced=True
    )
    if not isinstance(result, Graph):
        raise RuntimeError(f"SHACL validation could not run: {result}")
    fields = {
        "focus_node": SH.focusNode,
        "path": SH.resultPath,
        "severity": SH.resultSeverity,
        "component": SH.sourceConstraintComponent,
        "value": SH.value,
    }
    violations = [
        {key: _term_text(result.value(node, predicate)) for key, predicate in fields.items()}
        for report in result.subjects(RDF.type, SH.ValidationReport)
        for node in result.objects(report, SH.result)
    ]
    return {
        "conforms": conforms,
        "violations": sorted(violations, key=dump_json),
        "class_target_shapes": len(coverage),
        "exercised_class_target_shapes": sum(row["focus_nodes"] > 0 for row in coverage),
        "class_target_coverage": sorted(
            coverage, key=lambda row: (row["shape"], row["target_classes"])
        ),
    }


def _read_cq_contract(path: Path) -> dict[str, tuple[str, str]]:
    questions: dict[str, tuple[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not re.match(r"\s*\|\s*CQ-", line):
            continue
        row = CQ_ROW.fullmatch(line.strip())
        if row is None:
            raise ValueError(f"Malformed CQ contract row: {line}")
        question, group, evidence = row.groups()
        if question in questions:
            raise ValueError(f"Duplicate CQ in {path.name}: {question}")
        questions[question] = (group, evidence)
    if not any(evidence == "Required" for _, evidence in questions.values()):
        raise ValueError(f"No required competency questions in {path.name}")
    return questions


def _cq_report(graph: Graph, root: Path) -> dict:
    questions = _read_cq_contract(root / "requirements" / "competency_questions.md")
    specs = {}
    errors = []
    benchmark = root / "benchmarks"
    queries = benchmark / "queries"
    query_files = {path.name for path in queries.glob("*.rq") if path.is_file()}
    for path in sorted((benchmark / "expected_results").glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name}: invalid JSON at line {exc.lineno}, column {exc.colno}")
            continue
        question = spec.get("id") if isinstance(spec, dict) else None
        if not isinstance(question, str) or question not in questions:
            errors.append(f"{path.name}: unknown CQ {question}")
        elif question in specs:
            errors.append(f"{path.name}: duplicate expected result for {question}")
        else:
            specs[question] = spec

    results = []
    for question, (group, evidence) in sorted(questions.items()):
        spec = specs.get(question)
        row = {"id": question, "group": group, "status": "failed", "error": None}
        if evidence == "Backlog" and spec is None:
            row["status"] = "backlog"
        elif evidence == "Backlog":
            row["error"] = "Expected result exists; mark the CQ Required in the contract"
        elif spec is None:
            row["error"] = "Missing expected result"
        elif spec.get("group") != group:
            row["error"] = f"Spec group {spec.get('group')} does not match contract group {group}"
        elif not isinstance(spec.get("query_file"), str) or spec["query_file"] not in query_files:
            row["error"] = f"Missing or invalid query file: {spec.get('query_file')}"
        else:
            result = run_query_spec(graph, spec, queries_dir=queries)
            row.update(
                status="passed" if result["passed"] else "failed",
                error=result["error"],
                query_file=result["query_file"],
                expected_rows=result["expected_rows"],
                actual_rows=len(result["actual_rows"]),
            )
        results.append(row)
    return {
        "declared": len(results),
        "required": sum(evidence == "Required" for _, evidence in questions.values()),
        "passed": sum(row["status"] == "passed" for row in results),
        "backlog": sum(row["status"] == "backlog" for row in results),
        "failed": sum(row["status"] == "failed" for row in results),
        "contract_errors": errors,
        "questions": results,
    }


def evaluate(root: Path = REPO_ROOT) -> dict:
    """Run the benchmark quality gates and report advisory coverage."""
    inputs = {
        "ontology": sorted((root / "ontology").rglob("*.ttl")),
        "data": sorted((root / "benchmarks" / "datasets").glob("*.ttl")),
        "shapes": sorted((root / "shapes").glob("*.ttl")),
    }
    for kind, paths in inputs.items():
        if not paths:
            raise ValueError(f"No {kind} files for the quality report")
    schema = _schema_report(load(inputs["ontology"]))
    graph = load(inputs["ontology"] + inputs["data"])
    # CQs use asserted types; SHACL expands types in place afterward.
    competency = _cq_report(graph, root)
    shacl = _shacl_report(graph, load(inputs["shapes"]))
    return {
        "report_version": 1,
        "passed": (
            not schema["missing_domain_or_range"]
            and shacl["conforms"]
            and not competency["failed"]
            and not competency["contract_errors"]
        ),
        "inputs": {key: [str(p.relative_to(root)) for p in paths] for key, paths in inputs.items()},
        "schema": schema,
        "shacl": shacl,
        "competency": competency,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the report to a file instead of stdout")
    args = parser.parse_args()
    try:
        report = evaluate()
        payload = dump_json(report) + "\n"
        if args.output is None:
            print(payload, end="")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
            status = "PASS" if report["passed"] else "FAIL"
            print(f"Ontology quality: {status} ({args.output})")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Ontology quality could not run: {exc}", file=sys.stderr)
        return 2
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
