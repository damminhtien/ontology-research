"""Check the design of local ontology modules without loading instance data."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from manage_ontology import load_graph
from ontology_utils import dump_json
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from foundry.namespaces import (  # noqa: E402
    ASSERTION_MIDDLE_NS,
    CORE_ONTOLOGY_NS,
    ONTOLOGY_BASE,
    TRACKING_DOMAIN_NS,
)

NAMESPACE_URI = URIRef("http://purl.org/vocab/vann/preferredNamespaceUri")
CLASSES = frozenset({OWL.Class, RDFS.Class})
PROPERTIES = frozenset({OWL.ObjectProperty, OWL.DatatypeProperty})
CLASS_AXIOMS = (RDFS.subClassOf, OWL.equivalentClass, OWL.disjointWith)
REQUIRED_PARENTS = (
    (CORE_ONTOLOGY_NS + "Observation", CORE_ONTOLOGY_NS + "Event"),
    (TRACKING_DOMAIN_NS + "Track", CORE_ONTOLOGY_NS + "InformationObject"),
    (ASSERTION_MIDDLE_NS + "Assertion", CORE_ONTOLOGY_NS + "InformationObject"),
    (ASSERTION_MIDDLE_NS + "Document", CORE_ONTOLOGY_NS + "Source"),
)
REQUIRED_DISJOINT = (
    URIRef(CORE_ONTOLOGY_NS + "Event"),
    URIRef(CORE_ONTOLOGY_NS + "InformationObject"),
)

type Axiom = tuple[URIRef, URIRef, URIRef]
type Edges = dict[URIRef, dict[URIRef, Axiom]]


@dataclass(frozen=True)
class _Module:
    path: str
    iri: URIRef
    namespace: str
    graph: Graph


def _read_modules(root: Path) -> dict[URIRef, _Module]:
    modules: dict[URIRef, _Module] = {}
    namespaces: set[str] = set()
    for path in sorted((root / "ontology").rglob("*.ttl")):
        graph = load_graph(path)
        headers = set(graph.subjects(RDF.type, OWL.Ontology))
        if len(headers) != 1 or not isinstance(iri := next(iter(headers)), URIRef):
            raise ValueError(f"{path}: expected one named owl:Ontology header")
        values = set(graph.objects(iri, NAMESPACE_URI))
        if len(values) != 1 or not str(namespace := next(iter(values))).strip():
            raise ValueError(f"{path}: expected one nonempty vann:preferredNamespaceUri")
        if not isinstance(namespace, URIRef | Literal):
            raise ValueError(f"{path}: namespace must be an IRI or literal")
        if iri in modules or str(namespace) in namespaces:
            raise ValueError(f"{path}: duplicate ontology IRI or namespace")
        modules[iri] = _Module(path.relative_to(root).as_posix(), iri, str(namespace), graph)
        namespaces.add(str(namespace))
    if not modules:
        raise ValueError("No Turtle modules under ontology/")
    return modules


def _finding(code: str, files: set[str], terms: set[URIRef], evidence: list[str]) -> dict:
    return {
        "code": code,
        "files": sorted(files),
        "iris": sorted(map(str, terms)),
        "evidence": sorted(evidence),
    }


def _term(node: object) -> str:
    return "_:blank" if isinstance(node, BNode) else str(node)


def _axiom_text(triple: tuple) -> str:
    return " ".join(map(_term, triple))


def _reachable[T](start: T, links: dict[T, set[T]]) -> set[T]:
    visited = {start}
    pending = [start]
    while pending:
        for node in links.get(pending.pop(), set()) - visited:
            visited.add(node)
            pending.append(node)
    return visited


def _local_imports(modules: dict[URIRef, _Module], errors: list[dict]) -> dict[URIRef, set[URIRef]]:
    links: dict[URIRef, set[URIRef]] = {}
    for iri, module in modules.items():
        links[iri] = set()
        for target in module.graph.objects(iri, OWL.imports):
            if isinstance(target, URIRef) and target in modules:
                links[iri].add(target)
            else:
                errors.append(_finding("unresolved-import", {module.path}, {iri}, [_term(target)]))
    return {iri: _reachable(iri, links) for iri in modules}


def _kinds(module: _Module, term: URIRef) -> set:
    return set(module.graph.objects(term, RDF.type))


def _reference_slots(graph: Graph, kinds: dict[URIRef, set]):
    for predicate in (*CLASS_AXIOMS, RDFS.domain, RDFS.range, OWL.inverseOf):
        for left, right in graph.subject_objects(predicate):
            if predicate in CLASS_AXIOMS:
                expected = (CLASSES, CLASSES)
            elif predicate == OWL.inverseOf:
                expected = ({OWL.ObjectProperty}, {OWL.ObjectProperty})
            else:
                left_kinds = kinds.get(left, set())
                if OWL.AnnotationProperty in left_kinds and not left_kinds & PROPERTIES:
                    continue
                data_range = predicate == RDFS.range and OWL.DatatypeProperty in left_kinds
                expected = (PROPERTIES, {RDFS.Datatype} if data_range else CLASSES)
            yield (left, predicate, right), expected


def _reference_errors(
    module: _Module, visible: set[URIRef], modules: dict[URIRef, _Module], kinds: dict[URIRef, set]
) -> list[dict]:
    errors = []
    for axiom, expected in _reference_slots(module.graph, kinds):
        for term, allowed in zip((axiom[0], axiom[2]), expected, strict=True):
            if not isinstance(term, URIRef):
                continue
            owners = [m for m in modules.values() if str(term).startswith(m.namespace)]
            owner = max(owners, key=lambda m: len(m.namespace), default=None)
            if owner is None and not str(term).startswith(ONTOLOGY_BASE + "/"):
                continue
            declared = _kinds(owner, term) if owner else set()
            if not declared:
                code = "undeclared-term"
            elif not declared & allowed:
                code = "wrong-term-kind"
            elif owner.iri not in visible:
                code = "missing-import"
            else:
                continue
            evidence = [
                _axiom_text(axiom),
                "expected type: " + ", ".join(sorted(map(str, allowed))),
            ]
            if owner:
                evidence.append(f"owning module: {owner.path}")
            errors.append(_finding(code, {module.path}, {term}, evidence))
    return errors


def _proofs(start: URIRef, edges: Edges) -> dict[URIRef, tuple[Axiom, ...]]:
    proofs: dict[URIRef, tuple[Axiom, ...]] = {start: (), OWL.Thing: ()}
    pending = deque(sorted(proofs))
    while pending:
        node = pending.popleft()
        for parent, axiom in sorted(edges.get(node, {}).items()):
            if parent not in proofs:
                proofs[parent] = (*proofs[node], axiom)
                pending.append(parent)
    return proofs


def _taxonomy_errors(
    sources: dict[Axiom, set[str]], class_files: dict[URIRef, set[str]], module_paths: set[str]
) -> list[dict]:
    def finding(code: str, terms: set[URIRef], axioms: set[Axiom], message: str) -> dict:
        files = {file for axiom in axioms for file in sources[axiom]}
        files.update(file for term in terms for file in class_files.get(term, set()))
        return _finding(code, files or module_paths, terms, [message, *map(_axiom_text, axioms)])

    asserted: Edges = defaultdict(dict)
    edges: Edges = defaultdict(dict)
    disjoint: dict[tuple[URIRef, URIRef], set[Axiom]] = defaultdict(set)
    for left, predicate, right in sorted(sources):
        axiom = (left, predicate, right)
        if predicate == RDFS.subClassOf:
            asserted[left][right] = axiom
            edges[left][right] = axiom
        elif predicate == OWL.equivalentClass:
            edges[left].setdefault(right, axiom)
            edges[right].setdefault(left, axiom)
        elif predicate == OWL.disjointWith:
            pair = (min(left, right), max(left, right))
            disjoint[pair].add(axiom)

    errors = []
    links = {child: set(parents) for child, parents in asserted.items()}
    reachable = {cls: _reachable(cls, links) for cls in links}
    unchecked = set(links)
    while unchecked:
        start = min(unchecked)
        group = {node for node in reachable[start] & unchecked if start in reachable[node]}
        unchecked -= group
        if len(group) > 1 or start in asserted[start]:
            axioms = {asserted[child][parent] for child in group for parent in links[child] & group}
            errors.append(finding("subclass-cycle", group, axioms, "Asserted subclass cycle"))

    closures = {cls: _proofs(cls, edges) for cls in class_files}
    for cls, proof in closures.items():
        if OWL.Nothing in proof:
            errors.append(
                finding("empty-class", {cls}, set(proof[OWL.Nothing]), "Reaches owl:Nothing")
            )
        for (left, right), axioms in disjoint.items():
            if left in proof and right in proof:
                evidence = axioms | set(proof[left]) | set(proof[right])
                errors.append(
                    finding("empty-class", {cls}, evidence, "Inherits both disjoint classes")
                )
    for child, parent in ((URIRef(c), URIRef(p)) for c, p in REQUIRED_PARENTS):
        if parent not in class_files or parent not in closures.get(child, {}):
            errors.append(
                finding("required-subclass", {child, parent}, set(), f"Expected {child} ⊑ {parent}")
            )
    if set(REQUIRED_DISJOINT) - class_files.keys() or REQUIRED_DISJOINT not in disjoint:
        left, right = REQUIRED_DISJOINT
        errors.append(
            finding(
                "required-disjoint", {left, right}, set(), f"Expected {left} disjointWith {right}"
            )
        )
    return errors


def _annotation_report(module: _Module) -> tuple[dict, list[dict]]:
    graph = module.graph
    terms = {
        term
        for kind in CLASSES | PROPERTIES
        for term in graph.subjects(RDF.type, kind)
        if isinstance(term, URIRef) and str(term).startswith(module.namespace)
    }
    stats = {"file": module.path, "terms": len(terms)}
    warnings = []
    for field, predicate in (("label", RDFS.label), ("description", RDFS.comment)):
        missing = {
            term
            for term in terms
            if not any(
                isinstance(value, Literal) and str(value).strip()
                for value in graph.objects(term, predicate)
            )
        }
        stats[f"with_{field}"] = len(terms) - len(missing)
        if missing:
            warnings.append(
                _finding(f"missing-{field}", {module.path}, missing, [f"No nonempty {predicate}"])
            )
    return stats, warnings


def _inverse_warnings(graph: Graph, sources: dict[Axiom, set[str]]) -> list[dict]:
    pairs: dict[tuple[URIRef, URIRef], set[str]] = defaultdict(set)
    for axiom, files in sources.items():
        left, predicate, right = axiom
        if predicate == OWL.inverseOf:
            pairs[min(left, right), max(left, right)].update(files)
    warnings = []
    for (left, right), files in pairs.items():
        evidence = []
        for pred, opposite in ((RDFS.domain, RDFS.range), (RDFS.range, RDFS.domain)):
            declared = set(graph.objects(left, pred))
            mirrored = set(graph.objects(right, opposite))
            if declared != mirrored and all(
                isinstance(node, URIRef) for node in declared | mirrored
            ):
                evidence.append(
                    f"{left} {pred}: {sorted(map(_term, declared))}; "
                    f"{right} {opposite}: {sorted(map(_term, mirrored))}"
                )
        if evidence:
            warnings.append(_finding("inverse-signature", files, {left, right}, evidence))
    return warnings


def evaluate(root: Path = REPO_ROOT) -> dict:
    """Evaluate schema design without remote imports or benchmark data."""
    modules = _read_modules(root)
    errors: list[dict] = []
    warnings: list[dict] = []
    stats = []
    visible = _local_imports(modules, errors)
    graph = Graph()
    class_files: dict[URIRef, set[str]] = defaultdict(set)
    sources: dict[Axiom, set[str]] = defaultdict(set)
    for module in modules.values():
        graph += module.graph
        for kind in CLASSES:
            for cls in module.graph.subjects(RDF.type, kind):
                if isinstance(cls, URIRef) and str(cls).startswith(module.namespace):
                    class_files[cls].add(module.path)
        for predicate in (*CLASS_AXIOMS, OWL.inverseOf):
            for left, right in module.graph.subject_objects(predicate):
                if isinstance(left, URIRef) and isinstance(right, URIRef):
                    sources[left, predicate, right].add(module.path)
        module_stats, annotations = _annotation_report(module)
        stats.append(module_stats)
        warnings.extend(annotations)

    kinds: dict[URIRef, set] = defaultdict(set)
    for term, kind in graph.subject_objects(RDF.type):
        if isinstance(term, URIRef):
            kinds[term].add(kind)
    for iri, module in modules.items():
        errors.extend(_reference_errors(module, visible[iri], modules, kinds))
    errors.extend(_taxonomy_errors(sources, class_files, {m.path for m in modules.values()}))
    warnings.extend(_inverse_warnings(graph, sources))
    return {
        "report_version": 1,
        "passed": not errors,
        "modules": stats,
        "errors": sorted(errors, key=dump_json),
        "warnings": sorted(warnings, key=dump_json),
    }


def main(argv: list[str] | None = None, *, root: Path = REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the report to a file instead of stdout")
    args = parser.parse_args(argv)
    try:
        report = evaluate(root)
        payload = dump_json(report) + "\n"
        if args.output is None:
            print(payload, end="")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
            status = "PASS" if report["passed"] else "FAIL"
            print(f"Ontology design: {status} ({args.output})")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Ontology design could not run: {exc}", file=sys.stderr)
        return 2
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
