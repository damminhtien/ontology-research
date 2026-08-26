"""Ontology management CLI: stats, semantic diff, module scaffolding, report.

Subcommands:
    stats                     per-module class/property counts
    diff OLD NEW              semantic diff with SemVer severity classification
                              (exits 1 on MAJOR-level changes unless
                              --allow-breaking is passed)
    new-module NAME --layer   scaffold a middle/domain module importing core
    report                    markdown summary of the whole registry

The diff command enforces the roadmap rule "never redefine silently": any
domain/range change or term removal is flagged as a breaking (MAJOR) change;
label/comment edits are PATCH-level; additions are MINOR.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ontology_utils import (
    describe_classes,
    describe_properties,
    find_module_files,
    local_name,
)
from rdflib import Graph

REPO_ROOT = Path(__file__).resolve().parent.parent

SEVERITY_ORDER = ["PATCH", "MINOR", "MAJOR"]
SEVERITY_PATCH = "PATCH"
SEVERITY_MINOR = "MINOR"
SEVERITY_MAJOR = "MAJOR"

MODULE_TEMPLATE = """@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix vann: <http://purl.org/vocab/vann/> .
@prefix core: <https://ontology.example/core#> .

<{module_iri}> a owl:Ontology ;
    dcterms:title "{title}"@en ;
    dcterms:description "TODO: scope of this module."@en ;
    dcterms:version "0.1.0" ;
    owl:imports <https://ontology.example/core> ;
    vann:preferredNamespacePrefix "{prefix}" ;
    vann:preferredNamespaceUri "{module_iri}#" .

# TODO: define classes and properties here.
# Every property MUST declare rdfs:domain and rdfs:range.
# Domain concepts must NOT leak upward into core.
"""


@dataclass
class Change:
    """One detected difference between two ontology versions."""

    kind: str  # added | removed | changed
    target: str
    detail: str
    severity: str = SEVERITY_MINOR
    field_changes: list[str] = field(default_factory=list)


def load_graph(path: Path) -> Graph:
    """Parse a Turtle file, raising a clear error on failure."""
    graph = Graph()
    try:
        graph.parse(path.as_posix(), format="turtle")
    except Exception as exc:
        raise ValueError(f"cannot parse {path}: {exc}") from exc
    return graph


def snapshot(graph: Graph) -> dict[str, dict]:
    """Build a comparable term -> descriptor map for classes and properties."""
    terms: dict[str, dict] = {}
    for iri, desc in describe_classes(graph).items():
        terms[iri] = {
            "kind": "class",
            "label": desc["label"],
            "comment": desc["comment"],
            "parents": set(desc["parents"]),
        }
    for prop in describe_properties(graph):
        terms[prop["iri"]] = {
            "kind": f"{prop['kind']}-property",
            "label": prop["label"],
            "domain": prop["domain"],
            "range": prop["range"],
        }
    return terms


def classify_change(old: dict, new: dict) -> tuple[str, list[str]]:
    """Compare two descriptors of the same term; returns (severity, details)."""
    details: list[str] = []
    severity = SEVERITY_PATCH

    def note(level: str, text: str) -> None:
        nonlocal severity
        details.append(text)
        if SEVERITY_ORDER.index(level) > SEVERITY_ORDER.index(severity):
            severity = level

    if old.get("kind") != new.get("kind"):
        note(SEVERITY_MAJOR, f"kind changed {old.get('kind')} -> {new.get('kind')}")

    old_fields = {k: v for k, v in old.items() if k not in {"kind"}}
    new_fields = {k: v for k, v in new.items() if k not in {"kind"}}
    for name in sorted(set(old_fields) | set(new_fields)):
        before, after = old_fields.get(name), new_fields.get(name)
        if name in {"domain", "range"}:
            if before != after:
                note(
                    SEVERITY_MAJOR,
                    f"{name}: '{before or 'none'}' -> '{after or 'none'}'",
                )
        elif isinstance(before, set) or isinstance(after, set):
            removed = sorted((before or set()) - (after or set()))
            added = sorted((after or set()) - (before or set()))
            if removed:
                note(SEVERITY_MAJOR, f"parents removed: {removed}")
            if added:
                note(SEVERITY_MINOR, f"parents added: {added}")
        elif (before or "") != (after or ""):
            note(SEVERITY_PATCH, f"{name}: updated")
    return severity, details


def diff_snapshots(old_terms: dict[str, dict], new_terms: dict[str, dict]) -> list[Change]:
    """Compute the full change list between two term snapshots."""
    changes: list[Change] = []

    def add(
        kind: str, iri: str, detail: str, severity: str, field_changes: list[str] | None = None
    ) -> None:
        changes.append(
            Change(
                kind=kind,
                target=iri,
                detail=detail,
                severity=severity,
                field_changes=field_changes or [],
            )
        )

    for iri in sorted(set(old_terms) - set(new_terms)):
        add("removed", iri, f"{old_terms[iri]['kind']} removed", SEVERITY_MAJOR)
    for iri in sorted(set(new_terms) - set(old_terms)):
        add("added", iri, f"{new_terms[iri]['kind']} added", SEVERITY_MINOR)
    for iri in sorted(set(old_terms) & set(new_terms)):
        severity, details = classify_change(old_terms[iri], new_terms[iri])
        if details:
            add("changed", iri, "; ".join(details), severity, details)
    return changes


def highest_severity(changes: list[Change]) -> str:
    """Return the strongest severity present, defaulting to PATCH."""
    return max((c.severity for c in changes), default=SEVERITY_PATCH, key=SEVERITY_ORDER.index)


def format_term(iri: str) -> str:
    """Human-readable rendering of a term IRI."""
    return local_name(iri)


def cmd_stats(_args: argparse.Namespace) -> int:
    """Print per-module term counts."""
    modules = find_module_files()
    if not modules:
        print("No ontology modules found.")
        return 1
    total_classes = 0
    total_props = 0
    print(f"{'module':<28} {'classes':>7} {'obj-props':>9} {'data-props':>10}")
    for path in modules:
        graph = load_graph(path)
        classes = describe_classes(graph)
        props = describe_properties(graph)
        obj = sum(1 for p in props if p["kind"] == "object")
        dat = sum(1 for p in props if p["kind"] == "data")
        total_classes += len(classes)
        total_props += len(props)
        rel = path.relative_to(REPO_ROOT)
        print(f"{rel!s:<28} {len(classes):>7} {obj:>9} {dat:>10}")
    print(f"{'TOTAL':<28} {total_classes:>7} {'':>9} {total_props:>19} properties")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    """Diff two ontology files and classify the release severity."""
    old_terms = snapshot(load_graph(Path(args.old)))
    new_terms = snapshot(load_graph(Path(args.new)))
    changes = diff_snapshots(old_terms, new_terms)

    if not changes:
        print("No semantic changes.")
        return 0

    for change in changes:
        marker = {"added": "+", "removed": "-", "changed": "~"}[change.kind]
        print(f"{marker} [{change.severity:<5}] {format_term(change.target)}: {change.detail}")

    top = highest_severity(changes)
    print(f"\nSuggested version bump: {top}")
    if top == SEVERITY_MAJOR and not args.allow_breaking:
        print(
            "ERROR: breaking (MAJOR) changes detected; "
            "pass --allow-breaking to acknowledge a major release."
        )
        return 1
    return 0


def cmd_new_module(args: argparse.Namespace) -> int:
    """Scaffold a new ontology module in the requested layer."""
    if args.layer not in {"middle", "domain"}:
        print(f"Unsupported layer {args.layer!r}; choose 'middle' or 'domain'.")
        return 1
    target_dir = REPO_ROOT / "ontology" / args.layer
    target = target_dir / f"{args.name}.ttl"
    if target.exists():
        print(f"Module already exists: {target}")
        return 1
    target_dir.mkdir(parents=True, exist_ok=True)
    module_iri = f"https://ontology.example/{args.layer}/{args.name}"
    target.write_text(
        MODULE_TEMPLATE.format(
            module_iri=module_iri,
            title=f"{args.name.capitalize()} ({args.layer})",
            prefix=args.name,
        ),
        encoding="utf-8",
    )
    print(f"Scaffolded {target}")
    print("Next: define terms, then run 'make dag && make check'.")
    return 0


def cmd_report(_args: argparse.Namespace) -> int:
    """Write a markdown summary of the registry to build/ontology-report.md."""
    modules = find_module_files()
    lines = ["# Ontology Registry Report", ""]
    god_nodes: dict[str, int] = {}
    for path in modules:
        graph = load_graph(path)
        classes = describe_classes(graph)
        props = describe_properties(graph)
        rel = path.relative_to(REPO_ROOT)
        lines.append(f"## `{rel}`")
        lines.append("")
        lines.append(f"- Classes: **{len(classes)}**")
        lines.append(f"- Properties: **{len(props)}**")
        lines.append("")
        lines.append("| Class | Parents | Properties |")
        lines.append("|-------|---------|------------|")
        for iri, desc in sorted(classes.items()):
            parents = ", ".join(local_name(p) for p in desc["parents"]) or "-"
            prop_names = ", ".join(p["name"] for p in desc["properties"]) or "-"
            lines.append(f"| `{local_name(iri)}` | {parents} | {prop_names} |")
            god_nodes[iri] = len(desc["properties"]) + len(desc["parents"])
        lines.append("")

    hubs = sorted(god_nodes.items(), key=lambda kv: kv[1], reverse=True)[:5]
    lines.append("## Most-connected classes")
    lines.append("")
    for iri, score in hubs:
        lines.append(f"- `{local_name(iri)}` (score {score})")

    out_dir = REPO_ROOT / "build"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "ontology-report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {report_path}")
    return 0


def main() -> int:
    """CLI entrypoint dispatching subcommands."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="per-module class/property counts").set_defaults(func=cmd_stats)

    diff_parser = sub.add_parser("diff", help="semantic diff with SemVer classification")
    diff_parser.add_argument("old", help="old ontology .ttl file")
    diff_parser.add_argument("new", help="new ontology .ttl file")
    diff_parser.add_argument(
        "--allow-breaking",
        action="store_true",
        help="exit 0 even when MAJOR-level changes are detected",
    )
    diff_parser.set_defaults(func=cmd_diff)

    new_parser = sub.add_parser("new-module", help="scaffold a middle/domain module")
    new_parser.add_argument("name", help="module name, e.g. 'organization'")
    new_parser.add_argument("--layer", choices=["middle", "domain"], required=True)
    new_parser.set_defaults(func=cmd_new_module)

    sub.add_parser("report", help="markdown registry report").set_defaults(func=cmd_report)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
