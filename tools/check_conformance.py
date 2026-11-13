#!/usr/bin/env python3
"""Independent OWL, SHACL, SPARQL and RDF canonicalization checks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlparse
from xml.etree.ElementTree import Element, ElementTree, SubElement

import cq_runner
from ontology_utils import find_module_files, materialize_type_closure
from pyshacl import validate as pyshacl_validate
from rdflib import RDF, BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, SH

REPO_ROOT = Path(__file__).resolve().parents[1]
ROBOT_URL = "https://github.com/ontodev/robot/releases/download/v1.9.11/robot.jar"
ROBOT_SHA256 = "53cd275c8e4c0dad1fc2c1c34174876795cd7d6b6926e1444adcbb0cb3c7ab26"
JENA_URL = (
    "https://repo.maven.apache.org/maven2/org/apache/jena/apache-jena/6.2.0/"
    "apache-jena-6.2.0.tar.gz"
)
JENA_SHA512 = (
    "2b467d2ff940c207aea935e012ecbb0f0ab3e6bf59b3fc4b3fbc6cf9cea11f1"
    "428e97882ec078a5ffc57e114e0f5c88ea98c4575e687cc1dd232f99324b7b2a2"
)
RDF_CANON_COMMIT = "15619df2fda7a4ca88308733789b6774517f9638"
RDF_CANON_URL = f"https://codeload.github.com/w3c/rdf-canon/tar.gz/{RDF_CANON_COMMIT}"
CORE = Namespace("https://damminhtien.github.io/ontology-research/ontology/core#")
IDENTITY = Namespace("https://damminhtien.github.io/ontology-research/ontology/middle/identity#")
ASSERTION = Namespace("https://damminhtien.github.io/ontology-research/ontology/middle/assertion#")
TRACKING = Namespace("https://damminhtien.github.io/ontology-research/ontology/domain/tracking#")
EX = Namespace("urn:ontology-research:conformance:")


def _download(url: str, destination: Path, digest: str | None = None) -> None:
    """Download one pinned tool artifact and optionally verify its digest."""
    request = urllib.request.Request(url, headers={"User-Agent": "ontology-research-conformance"})
    hasher = hashlib.sha256() if digest and len(digest) == 64 else hashlib.sha512()
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            hasher.update(chunk)
            output.write(chunk)
    if digest and hasher.hexdigest() != digest:
        raise RuntimeError(f"checksum mismatch for {url}: {hasher.hexdigest()}")


def _run(command: list[str], *, env: dict[str, str] | None = None) -> str:
    """Run a conformance command, preserving full output on failure."""
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    if result.returncode:
        rendered = " ".join(command)
        raise RuntimeError(f"command failed ({result.returncode}): {rendered}\n{output}")
    return output


def _java_executable() -> tuple[Path, Path]:
    """Return the first available Java 25 path from the configured candidates."""
    candidates: list[Path] = []
    configured = os.environ.get("JAVA_HOME")
    if configured:
        candidates.append(Path(configured))
    if Path("/usr/libexec/java_home").is_file():
        result = subprocess.run(
            ["/usr/libexec/java_home", "-v", "25"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            candidates.append(Path(result.stdout.strip()))
    homebrew = shutil.which("brew")
    if homebrew:
        result = subprocess.run(
            [homebrew, "--prefix", "openjdk@25"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            candidates.append(Path(result.stdout.strip()))
    system_java = shutil.which("java")
    if system_java:
        candidates.append(Path(system_java).resolve().parents[1])
    failures = []
    for java_home in dict.fromkeys(candidates):
        java = java_home / "bin" / "java"
        if not java.is_file():
            failures.append(f"no Java binary under {java_home}")
            continue
        result = subprocess.run(
            [str(java), "-version"], check=False, capture_output=True, text=True
        )
        version_text = result.stdout + result.stderr
        match = re.search(r'(?:openjdk|java)\s+version\s+"?(\d+)', version_text, re.IGNORECASE)
        if result.returncode == 0 and match and int(match.group(1)) >= 25:
            return java_home, java
        failures.append(version_text.strip() or f"Java is unavailable under {java_home}")
    raise RuntimeError("Java 25 or newer is required; candidates reported: " + "; ".join(failures))


def _write_ontology_root(directory: Path, module_paths: list[Path]) -> tuple[Path, Path]:
    """Write an import-closure root and local OASIS XML catalog."""
    catalog = Element("catalog", xmlns="urn:oasis:names:tc:entity:xmlns:xml:catalog")
    module_iris: list[str] = []
    for path in sorted(module_paths):
        graph = Graph().parse(path)
        module_iri = graph.value(predicate=RDF.type, object=OWL.Ontology)
        if not isinstance(module_iri, URIRef):
            raise ValueError(f"ontology module has no unique ontology IRI: {path}")
        module_iris.append(str(module_iri))
        SubElement(catalog, "uri", name=str(module_iri), uri=path.resolve().as_uri())
    root = directory / "root.ttl"
    imports = ", ".join(f"<{module_iri}>" for module_iri in module_iris)
    root.write_text(
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        f"<urn:ontology-research:conformance-root> a owl:Ontology ; owl:imports {imports} .\n",
        encoding="utf-8",
    )
    catalog_path = directory / "catalog-v001.xml"
    ElementTree(catalog).write(catalog_path, encoding="utf-8", xml_declaration=True)
    return root, catalog_path


def _base_commit() -> str:
    """Select the reviewed base commit for the supplemental OWL diff."""
    configured = os.environ.get("ONTOLOGY_CONFORMANCE_BASE_SHA")
    if configured:
        return configured
    result = subprocess.run(
        ["git", "merge-base", "origin/main", "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return _run(["git", "rev-parse", "HEAD^"], env=dict(os.environ)).strip()


def _write_base_modules(directory: Path, base_sha: str) -> list[Path]:
    """Materialize only the ontology modules from the selected base commit."""
    paths: list[Path] = []
    for current in find_module_files():
        relative = current.relative_to(REPO_ROOT).as_posix()
        text = _run(["git", "show", f"{base_sha}:{relative}"])
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths


def _robot_checks(tmp: Path, robot_jar: Path, java: Path) -> None:
    """Run OWL 2 DL validation, HermiT consistency and semantic diff."""
    current_dir = tmp / "current-ontology"
    base_dir = tmp / "base-ontology"
    current_dir.mkdir()
    base_dir.mkdir()
    current_paths = find_module_files()
    current_root, current_catalog = _write_ontology_root(current_dir, current_paths)
    base_paths = _write_base_modules(base_dir, _base_commit())
    base_root, base_catalog = _write_ontology_root(base_dir, base_paths)
    profile_report = tmp / "owl2-dl-profile.txt"
    _run(
        [
            java,
            "-jar",
            str(robot_jar),
            "validate-profile",
            "--profile",
            "DL",
            "--input",
            str(current_root),
            "--catalog",
            str(current_catalog),
            "--output",
            str(profile_report),
        ]
    )
    if "in profile" not in profile_report.read_text(encoding="utf-8"):
        raise RuntimeError("ROBOT did not confirm that the ontology closure is in OWL 2 DL")
    _run(
        [
            java,
            "-jar",
            str(robot_jar),
            "reason",
            "--reasoner",
            "HermiT",
            "--input",
            str(current_root),
            "--catalog",
            str(current_catalog),
            "--output",
            str(tmp / "reasoned.owl"),
        ]
    )
    diff_report = tmp / "robot-diff.txt"
    _run(
        [
            java,
            "-jar",
            str(robot_jar),
            "diff",
            "--left",
            str(base_root),
            "--left-catalog",
            str(base_catalog),
            "--right",
            str(current_root),
            "--right-catalog",
            str(current_catalog),
            "--output",
            str(diff_report),
        ]
    )
    print(f"ROBOT OWL 2 DL, HermiT and diff passed ({_base_commit()[:12]} baseline).")


def _load_data_graph() -> Graph:
    """Load the ontology and the same seed datasets used by make validate."""
    graph = Graph()
    for path in find_module_files():
        graph.parse(path)
    for name in ("sample_data.ttl", "domain_tracking.ttl"):
        graph.parse(REPO_ROOT / "benchmarks" / "datasets" / name)
    materialize_type_closure(graph)
    return graph


def _bad_fixture(shape_name: str) -> Graph:
    """Create one deliberately invalid node for each SHACL contract group."""
    graph = Graph()
    for path in find_module_files():
        graph.parse(path)
    bad = EX[f"bad-{shape_name.removesuffix('_shapes.ttl')}"]
    if shape_name == "core_shapes.ttl":
        graph.add((bad, RDF.type, CORE.Observation))
        graph.add((bad, CORE.observes, EX["observed-entity"]))
        graph.add((bad, CORE.hasConfidence, Literal("1.5")))
    elif shape_name == "identity_shapes.ttl":
        graph.add((bad, RDF.type, IDENTITY.Identifier))
    elif shape_name == "domain_shapes.ttl":
        graph.add((bad, RDF.type, TRACKING.Track))
    elif shape_name == "assertion_shapes.ttl":
        graph.add((bad, RDF.type, ASSERTION.Assertion))
    else:
        raise ValueError(f"unknown SHACL shape group {shape_name}")
    materialize_type_closure(graph)
    return graph


def _jena_conforms(jena_shacl: Path, shapes: Path, data: Path, env: dict[str, str]) -> bool:
    """Read sh:conforms from Jena's RDF validation report, not the CLI exit code."""
    output = _run(
        [
            str(jena_shacl),
            "validate",
            "--shapes",
            str(shapes),
            "--data",
            str(data),
        ],
        env=env,
    )
    report = Graph().parse(data=output, format="turtle")
    conforms = next(report.objects(None, SH.conforms), None)
    if conforms is None:
        raise RuntimeError(f"Jena report does not contain sh:conforms for {shapes}:\n{output}")
    return bool(conforms.toPython())


def _shacl_checks(tmp: Path, jena_home: Path, java_home: Path) -> None:
    """Require pySHACL and Jena to agree on positive and negative fixtures."""
    env = dict(os.environ, JAVA_HOME=str(java_home), JENA_HOME=str(jena_home))
    jena_shacl = jena_home / "bin" / "shacl"
    positive = _load_data_graph()
    cases: list[tuple[str, Graph, bool]] = [("seed", positive, True)]
    for shape_path in sorted((REPO_ROOT / "shapes").glob("*.ttl")):
        cases.append((shape_path.name, _bad_fixture(shape_path.name), False))
    for index, (case_name, graph, expected) in enumerate(cases):
        shapes = (REPO_ROOT / "shapes" / case_name) if case_name.endswith("_shapes.ttl") else None
        data_path = tmp / f"shacl-{index}.ttl"
        data_path.write_text(graph.serialize(format="turtle"), encoding="utf-8")
        for shape_path in (
            sorted((REPO_ROOT / "shapes").glob("*.ttl")) if shapes is None else [shapes]
        ):
            shape_graph = Graph().parse(shape_path)
            conforms, _, _ = pyshacl_validate(
                data_graph=graph,
                shacl_graph=shape_graph,
                inference="none",
                advanced=True,
            )
            jena_value = _jena_conforms(jena_shacl, shape_path, data_path, env)
            if conforms is not expected or jena_value is not expected:
                raise RuntimeError(
                    f"SHACL conformance mismatch for {case_name} vs {shape_path.name}: "
                    f"pySHACL={conforms}, Jena={jena_value}, expected={expected}"
                )
    print("pySHACL and Jena agree on the seed and four invalid SHACL fixtures.")


def _jena_query(
    query_file: Path,
    query_text: str,
    spec: dict,
    data_file: Path,
    arq: Path,
    tmp: Path,
    env: dict[str, str],
) -> tuple[list[str], list[list[object]]]:
    """Run a competency query through ARQ and normalize its SPARQL JSON rows."""
    bindings = cq_runner._query_bindings(spec)
    if bindings:
        match = re.search(r"\bWHERE\s*\{", query_text, flags=re.IGNORECASE)
        if match is None:
            raise ValueError(f"query has bindings but no WHERE group: {query_file}")
        variables = " ".join(f"?{name}" for name in bindings)
        terms = " ".join(term.n3() for term in bindings.values())
        query_text = (
            query_text[: match.end()]
            + f" VALUES ({variables}) {{ ({terms}) }} "
            + query_text[match.end() :]
        )
    if query_file.name == "q31_sensor_provenance.rq":
        query_text = query_text.replace("FILTER (?observation = ?wantedObservation)", "")
        query_text = query_text.replace("FILTER (?trackId = ?wantedTrackId)", "")
        query_text = query_text.replace(
            "  ?sensor core:name ?sensorName .",
            "  ?sensor core:name ?sensorName .\n"
            "  FILTER ((BOUND(?wantedObservation) && !BOUND(?trackId) && "
            "?observation = ?wantedObservation) || (BOUND(?wantedTrackId) && "
            "BOUND(?trackId) && ?trackId = ?wantedTrackId))",
        )
    rendered = tmp / f"{spec['id']}.rq"
    rendered.write_text(query_text, encoding="utf-8")
    output = _run(
        [
            str(arq),
            "--data",
            str(data_file),
            "--query",
            str(rendered),
            "--results=JSON",
        ],
        env=env,
    )
    result = json.loads(output)
    variables = result["head"]["vars"]
    rows: list[list[object]] = []
    for binding in result["results"]["bindings"]:
        row: list[object] = []
        for name in spec["variables"]:
            value = binding.get(name)
            if value is None:
                row.append(None)
            elif value["type"] == "uri":
                row.append(cq_runner._normalize_cell(URIRef(value["value"])))
            elif value["type"] == "bnode":
                row.append(cq_runner._normalize_cell(BNode(value["value"])))
            else:
                row.append(
                    cq_runner._normalize_cell(
                        Literal(
                            value["value"],
                            datatype=URIRef(value["datatype"]) if value.get("datatype") else None,
                            lang=value.get("xml:lang"),
                        )
                    )
                )
        rows.append(row)
    return variables, rows


def _query_checks(tmp: Path, jena_home: Path, java_home: Path) -> None:
    """Compare all 41 competency queries between RDFLib and Jena ARQ."""
    env = dict(os.environ, JAVA_HOME=str(java_home), JENA_HOME=str(jena_home))
    arq = jena_home / "bin" / "arq"
    graph = cq_runner.load_benchmark_graph()
    data_path = tmp / "competency-seed.nt"
    data_path.write_text(graph.serialize(format="nt"), encoding="utf-8")
    specs = sorted(cq_runner.EXPECTED_DIR.glob("*.json"))
    if len(specs) != 41:
        raise RuntimeError(f"expected 41 competency query specs, found {len(specs)}")
    for spec_path in specs:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        query_path = cq_runner.QUERIES_DIR / spec["query_file"]
        query_text = query_path.read_text(encoding="utf-8")
        variables, actual_rows = _jena_query(query_path, query_text, spec, data_path, arq, tmp, env)
        if variables != spec["variables"]:
            raise RuntimeError(f"{spec['id']}: Jena variables {variables} != {spec['variables']}")
        expected_rows = spec["rows"]
        if re.search(r"\bORDER\s+BY\b", query_text, flags=re.IGNORECASE):
            matches = actual_rows == expected_rows
        else:
            matches = Counter(map(repr, actual_rows)) == Counter(map(repr, expected_rows))
        if not matches:
            raise RuntimeError(
                f"{spec['id']}: Jena rows differ from expected results\n"
                f"expected={expected_rows}\nactual={actual_rows}"
            )
    print(f"RDFLib and Jena ARQ agree on {len(specs)}/41 competency queries.")


def _rdf_canon_checks(tmp: Path) -> None:
    """Run the pinned W3C RDFC-1.0 evaluation vectors with rdf-canonize 5.0.0."""
    archive = tmp / "rdf-canon-tests.tar.gz"
    _download(RDF_CANON_URL, archive)
    test_dir = tmp / "rdf-canon-tests"
    test_dir.mkdir()
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(test_dir, filter="data")
    manifests = list(test_dir.glob("*/tests/manifest.ttl"))
    if len(manifests) != 1:
        raise RuntimeError(f"expected one W3C canonicalization manifest, found {len(manifests)}")
    manifest = Graph().parse(manifests[0])
    test_vocab = Namespace("https://w3c.github.io/rdf-canon/tests/vocab#")
    mf = Namespace("http://www.w3.org/2001/sw/DataAccess/tests/test-manifest#")
    test_cases = sorted(manifest.subjects(RDF.type, test_vocab.RDFC10EvalTest), key=str)
    cases = []
    for case in test_cases:
        action = manifest.value(case, mf.action)
        expected = manifest.value(case, mf.result)
        if action is None or expected is None:
            raise RuntimeError(f"W3C evaluation test is missing an action/result: {case}")
        cases.append(
            {
                "name": str(case).rsplit("#", maxsplit=1)[-1],
                "input": unquote(urlparse(str(action)).path),
                "expected": unquote(urlparse(str(expected)).path),
                "hashAlgorithm": str(manifest.value(case, test_vocab.hashAlgorithm) or "SHA256"),
            }
        )
    suite_root = manifests[0].parents[1]
    case_file = tmp / "rdf-canon-cases.json"
    case_file.write_text(
        json.dumps(
            [
                {
                    **case,
                    "input": str(suite_root / Path(case["input"]).relative_to(suite_root)),
                    "expected": str(suite_root / Path(case["expected"]).relative_to(suite_root)),
                }
                for case in cases
            ]
        ),
        encoding="utf-8",
    )
    package_root = tmp / "node-modules"
    _run(
        [
            "npm",
            "install",
            "--prefix",
            str(package_root),
            "--no-save",
            "--ignore-scripts",
            "rdf-canonize@5.0.0",
        ]
    )
    output = _run(
        [
            "node",
            str(REPO_ROOT / "tools" / "rdf_canonize_w3c.mjs"),
            str(package_root),
            str(case_file),
        ]
    )
    print(output.strip())


def main() -> int:
    """Download checksum-pinned tools into a temporary directory and run gates."""
    java_home, java = _java_executable()
    with tempfile.TemporaryDirectory(prefix="ontology-conformance-") as directory:
        tmp = Path(directory)
        robot_jar = tmp / "robot.jar"
        jena_archive = tmp / "jena.tar.gz"
        _download(ROBOT_URL, robot_jar, ROBOT_SHA256)
        _download(JENA_URL, jena_archive, JENA_SHA512)
        jena_home = tmp / "apache-jena-6.2.0"
        with tarfile.open(jena_archive, "r:gz") as bundle:
            bundle.extractall(tmp, filter="data")
        if not (jena_home / "bin" / "shacl").is_file():
            raise RuntimeError("Apache Jena 6.2.0 archive did not contain the SHACL CLI")
        _robot_checks(tmp, robot_jar, java)
        _shacl_checks(tmp, jena_home, java_home)
        _query_checks(tmp, jena_home, java_home)
        _rdf_canon_checks(tmp)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Ontology conformance failed: {exc}", file=sys.stderr)
        sys.exit(1)
