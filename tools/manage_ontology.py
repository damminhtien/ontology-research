"""Ontology management CLI: stats, diff, versioning, scaffolding, report.

Subcommands:
    stats                     per-module class/property counts
    diff OLD NEW              semantic diff with SemVer and compatibility classes
                              (exits 1 on MAJOR-level changes unless
                              --allow-breaking is passed)
    new-module NAME --layer   scaffold a middle/domain module importing core
    report                    markdown summary of the whole registry
    check-versions            enforce declared dcterms:version consistency
                              against the latest release in registry/
    release MODULE            record a release: snapshot + log + changelog
                              (MAJOR requires --migration)
    blast-radius TERM         locate schema, API, query and projection consumers
    stability                 Stability(m) = 1 - N_breaking/N_releases per module
    migrate MODULE --data ... generate a migration and preflight impact report
                              (RDF, JSON/JSONL, Parquet and manifest-backed lake)
    align add/list/check      semantic alignment registry (registry/alignments.json);
                              check validates every source term is still defined

The diff command enforces the roadmap rule "never redefine silently": any
domain/range change or term removal is flagged as a breaking (MAJOR) change;
label/comment edits are PATCH-level; additions are MINOR. check-versions makes
that rule machine-enforced on every `make check` run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ontology_utils import (
    describe_classes,
    describe_properties,
    dump_json,
    find_module_files,
    local_name,
)
from rdflib import RDF, Graph, URIRef
from rdflib.namespace import OWL

REPO_ROOT = Path(__file__).resolve().parent.parent

SEVERITY_ORDER = ["NONE", "PATCH", "MINOR", "MAJOR"]
SEVERITY_NONE = "NONE"
SEVERITY_PATCH = "PATCH"
SEVERITY_MINOR = "MINOR"
SEVERITY_MAJOR = "MAJOR"
COMPATIBILITY_SAFE = "SAFE"
COMPATIBILITY_COMPATIBLE = "COMPATIBLE"
COMPATIBILITY_BREAKING = "BREAKING"

REGISTRY_DIRNAME = "registry"
MIGRATIONS_DIRNAME = "migrations"
ALIGNMENTS_FILENAME = "alignments.json"
ALIGNMENT_RELATIONS = (
    "exactMatch",
    "closeMatch",
    "broadMatch",
    "narrowMatch",
    "relatedMatch",
)
RELEASES_FILE = "releases.json"
SNAPSHOT_VERSION = 1
STABILITY_THRESHOLD_CORE = 0.99
STABILITY_THRESHOLD_DEFAULT = 0.95

_DCT_VERSION = URIRef("http://purl.org/dc/terms/version")
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

MODULE_TEMPLATE = """@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix vann: <http://purl.org/vocab/vann/> .
@prefix core: <https://damminhtien.github.io/ontology-research/ontology/core#> .

<{module_iri}> a owl:Ontology ;
    dcterms:title "{title}"@en ;
    dcterms:description "TODO: scope of this module."@en ;
    dcterms:version "0.1.0" ;
    owl:imports <https://damminhtien.github.io/ontology-research/ontology/core> ;
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


def classify_compatibility(changes: list[Change]) -> str:
    """Classify schema changes by consumer compatibility, independent of SemVer."""
    if any(change.severity == SEVERITY_MAJOR for change in changes):
        return COMPATIBILITY_BREAKING
    if any(change.kind == "changed" for change in changes):
        return COMPATIBILITY_COMPATIBLE
    return COMPATIBILITY_SAFE


def change_compatibility(change: Change) -> str:
    """Classify one change using the repository's conservative SemVer rules."""
    if change.severity == SEVERITY_MAJOR:
        return COMPATIBILITY_BREAKING
    if change.kind == "changed":
        return COMPATIBILITY_COMPATIBLE
    return COMPATIBILITY_SAFE


def format_term(iri: str) -> str:
    """Human-readable rendering of a term IRI."""
    return local_name(iri)


# ---------------------------------------------------------------------------
# Versioning engine: SemVer helpers, release registry, consistency checks
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    """Return the current UTC instant as an xsd:dateTime lexical form."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse a SemVer string into a comparable tuple.

    Raises:
        ValueError: If the text is not MAJOR.MINOR.PATCH.
    """
    match = _SEMVER_RE.match(text.strip())
    if not match:
        raise ValueError(f"'{text}' is not valid SemVer (MAJOR.MINOR.PATCH)")
    return tuple(int(group) for group in match.groups())  # type: ignore[return-value]


def bump_level(old: str, new: str) -> str:
    """Classify the transition old -> new as NONE/PATCH/MINOR/MAJOR.

    Raises:
        ValueError: On downgrade or invalid input.
    """
    old_v, new_v = parse_version(old), parse_version(new)
    if new_v < old_v:
        raise ValueError(f"version downgraded {old} -> {new}")
    if new_v[0] != old_v[0]:
        return SEVERITY_MAJOR
    if new_v[1] != old_v[1]:
        return SEVERITY_MINOR
    if new_v[2] != old_v[2]:
        return SEVERITY_PATCH
    return SEVERITY_NONE


def next_version(current: str, severity: str) -> str:
    """Return the smallest version of ``current`` satisfying ``severity``."""
    major, minor, patch = parse_version(current)
    if severity == SEVERITY_MAJOR:
        return f"{major + 1}.0.0"
    if severity == SEVERITY_MINOR:
        return f"{major}.{minor + 1}.0"
    if severity == SEVERITY_PATCH:
        return f"{major}.{minor}.{patch + 1}"
    return current


def module_identity(graph: Graph) -> tuple[str, str]:
    """Return (module_iri, declared_version) from an ontology graph.

    Raises:
        ValueError: If the header is missing/ambiguous or version is invalid.
    """
    ontologies = sorted(str(s) for s in graph.subjects(RDF.type, OWL.Ontology))
    if len(ontologies) != 1:
        raise ValueError(f"expected exactly one owl:Ontology header, found {len(ontologies)}")
    iri = ontologies[0]
    versions = sorted(str(v) for v in graph.objects(URIRef(iri), _DCT_VERSION))
    if len(versions) != 1:
        raise ValueError(f"module <{iri}> must declare exactly one dcterms:version")
    parse_version(versions[0])
    return iri, versions[0]


def registry_dir(repo_root: Path | None = None) -> Path:
    """Resolve the release registry directory for a repo root."""
    base = repo_root if repo_root is not None else REPO_ROOT
    return base / REGISTRY_DIRNAME


def load_releases(registry: Path) -> list[dict]:
    """Load all release entries from ``registry/releases.json``."""
    path = registry / RELEASES_FILE
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("registry_version") != 1:
        raise ValueError(f"unsupported registry_version in {path}")
    return list(data.get("releases", []))


def save_releases(registry: Path, entries: list[dict]) -> None:
    """Persist release entries to the registry as stable JSON."""
    registry.mkdir(parents=True, exist_ok=True)
    payload = {"registry_version": 1, "releases": entries}
    (registry / RELEASES_FILE).write_text(dump_json(payload) + "\n", encoding="utf-8")


def latest_release(releases: list[dict], module_iri: str) -> dict | None:
    """Return the highest-version release entry for a module, or None."""
    module_releases = [r for r in releases if r.get("module_iri") == module_iri]
    if not module_releases:
        return None
    return max(module_releases, key=lambda r: parse_version(r["version"]))


def terms_to_jsonable(terms: dict[str, dict]) -> dict[str, dict]:
    """Convert term descriptors to JSON-safe form (sets become sorted lists)."""
    out: dict[str, dict] = {}
    for iri, desc in terms.items():
        entry = dict(desc)
        if isinstance(entry.get("parents"), set):
            entry["parents"] = sorted(entry["parents"])
        out[iri] = entry
    return out


def terms_from_jsonable(raw: dict[str, dict]) -> dict[str, dict]:
    """Rebuild term descriptors from JSON form (parents lists become sets)."""
    out: dict[str, dict] = {}
    for iri, desc in raw.items():
        entry = dict(desc)
        if "parents" in entry:
            entry["parents"] = set(entry["parents"])
        out[iri] = entry
    return out


def snapshot_path(registry: Path, module_local: str, version: str) -> Path:
    """Path of the snapshot file for one module version."""
    return registry / "snapshots" / module_local / f"{version}.json"


def save_snapshot(
    registry: Path,
    module_local: str,
    version: str,
    module_iri: str,
    file_rel: str,
    terms: dict[str, dict],
) -> Path:
    """Persist one term snapshot and return its path."""
    payload = {
        "snapshot_version": SNAPSHOT_VERSION,
        "module_iri": module_iri,
        "file": file_rel,
        "version": version,
        "date": utc_now_iso(),
        "terms": terms_to_jsonable(terms),
    }
    path = snapshot_path(registry, module_local, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_json(payload) + "\n", encoding="utf-8")
    return path


def load_snapshot_terms(registry: Path, module_local: str, version: str) -> dict[str, dict]:
    """Load a stored term snapshot back into diff-compatible descriptors."""
    path = snapshot_path(registry, module_local, version)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("snapshot_version") != SNAPSHOT_VERSION:
        raise ValueError(f"unsupported snapshot_version in {path}")
    return terms_from_jsonable(data["terms"])


def current_commit() -> str | None:
    """Best-effort short commit hash of HEAD; None when git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        )
        return result.stdout.strip()
    except Exception:
        return None


def evaluate_module_version(
    latest: dict,
    latest_terms: dict[str, dict],
    declared_version: str,
    current_terms: dict[str, dict],
) -> tuple[str, str]:
    """Check declared-version consistency against the semantic diff baseline.

    Returns (status, message); status is one of ok/warn/error, implementing
    the truth table from the Phase 5 design.
    """
    changes = diff_snapshots(latest_terms, current_terms)
    required = highest_severity(changes)
    bump = bump_level(latest["version"], declared_version)

    if not changes:
        if bump == SEVERITY_NONE:
            return "ok", f"no changes since {latest['version']}"
        return (
            "error",
            f"version bumped {latest['version']} -> {declared_version} but no "
            "semantic changes; revert the bump or make a real change",
        )
    if bump == SEVERITY_NONE:
        return (
            "error",
            f"{required} changes detected since {latest['version']} "
            f"(e.g. {changes[0].detail}); bump dcterms:version to at least "
            f"{next_version(latest['version'], required)}",
        )
    if SEVERITY_ORDER.index(required) > SEVERITY_ORDER.index(bump):
        return (
            "error",
            f"{required} changes detected but only a {bump} bump "
            f"({latest['version']} -> {declared_version}); silent redefinition "
            f"is forbidden - use at least "
            f"{next_version(latest['version'], required)}",
        )
    if SEVERITY_ORDER.index(bump) > SEVERITY_ORDER.index(required):
        return (
            "warn",
            f"conservative {bump} bump for {required} changes "
            f"({latest['version']} -> {declared_version})",
        )
    return "ok", f"{required} release {latest['version']} -> {declared_version}"


def collect_version_status() -> list[dict]:
    """Compute per-module version consistency against release baselines.

    Single source of truth used by both ``cmd_check_versions`` (CLI output)
    and the console API. Each result dict carries: file, module (local name),
    module_iri, declared_version, latest_version, status (ok/warn/error/info)
    and message.
    """
    registry = registry_dir()
    releases = load_releases(registry)
    results: list[dict] = []

    for path in find_module_files():
        entry: dict = {
            "file": path.relative_to(REPO_ROOT).as_posix(),
            "module": None,
            "module_iri": None,
            "declared_version": None,
            "latest_version": None,
            "status": "ok",
            "message": "",
        }
        try:
            iri, declared = module_identity(load_graph(path))
        except ValueError as exc:
            entry.update(status="error", message=str(exc))
            results.append(entry)
            continue

        entry["module"] = local_name(iri)
        entry["module_iri"] = iri
        entry["declared_version"] = declared
        latest = latest_release(releases, iri)

        if latest is None:
            entry.update(
                status="info",
                message=(
                    f"never released; run 'release {entry['module']}' to record "
                    f"{declared} as baseline"
                ),
            )
            results.append(entry)
            continue

        entry["latest_version"] = latest["version"]
        latest_terms = load_snapshot_terms(registry, entry["module"], latest["version"])
        status, message = evaluate_module_version(
            latest, latest_terms, declared, snapshot(load_graph(path))
        )
        entry.update(status=status, message=message)
        results.append(entry)
    return results


def cmd_check_versions(_args: argparse.Namespace) -> int:
    """Validate every module's declared version against its release baseline."""
    statuses = collect_version_status()
    if not statuses:
        print("No ontology modules found.")
        return 1
    for entry in statuses:
        print(f"[{entry['status']:<5}] {entry['file']}: {entry['message']}")
    failures = sum(1 for e in statuses if e["status"] == "error")
    if failures:
        print(f"\n{failures} module(s) failed the version check.")
        return 1
    print("\nVersion check passed.")
    return 0


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
        compatibility = change_compatibility(change)
        print(
            f"{marker} [{change.severity:<5}/{compatibility:<10}] "
            f"{format_term(change.target)}: {change.detail}"
        )

    top = highest_severity(changes)
    print(f"\nCompatibility: {classify_compatibility(changes)}")
    print(f"Suggested version bump: {top}")
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
    module_iri = (
        f"https://damminhtien.github.io/ontology-research/ontology/{args.layer}/{args.name}"
    )
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


# ---------------------------------------------------------------------------
# Release workflow, changelog, blast radius, stability
# ---------------------------------------------------------------------------


def changed_term_names(changes: list[Change]) -> list[str]:
    """Local names of all terms touched by a change list."""
    return sorted({local_name(change.target) for change in changes})


def blast_radius_for_terms(
    term_names: list[str], repo_root: Path | None = None
) -> dict[str, list[str]]:
    """Find lexical consumers across data, schema, API and projection layers.

    Results are impact leads, not proof that a file's runtime contract changes.
    Local-name matching deliberately over-reports to avoid missing consumers.
    """
    base = repo_root if repo_root is not None else REPO_ROOT
    iri_patterns = [re.compile(rf"[:#/]{re.escape(name)}\b") for name in term_names]
    # Application sources reference terms as bare identifiers, not IRIs.
    word_patterns = [re.compile(rf"\b{re.escape(name)}\b") for name in term_names]

    def references_iri(text: str) -> bool:
        return any(pattern.search(text) for pattern in iri_patterns)

    def references_word(text: str) -> bool:
        return any(pattern.search(text) for pattern in word_patterns)

    result: dict[str, list[str]] = {
        "modules": [],
        "shapes": [],
        "event_contracts": [],
        "queries": [],
        "api": [],
        "indexing": [],
        "applications": [],
        "migration_scripts": [],
    }

    def add_matches(category: str, paths: list[Path], *, iri_only: bool = False) -> None:
        for path in sorted(set(paths)):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            matches = (
                references_iri(text)
                if iri_only
                else (references_iri(text) or references_word(text))
            )
            if matches:
                result[category].append(path.relative_to(base).as_posix())

    ontology_dir = base / "ontology"
    if ontology_dir.exists():
        add_matches("modules", list(ontology_dir.rglob("*.ttl")), iri_only=True)

    shapes_dir = base / "shapes"
    if shapes_dir.exists():
        add_matches("shapes", list(shapes_dir.rglob("*.ttl")), iri_only=True)

    queries_dir = base / "benchmarks" / "queries"
    if queries_dir.exists():
        add_matches("queries", list(queries_dir.rglob("*.rq")), iri_only=True)

    foundry_dir = base / "foundry"
    if foundry_dir.exists():
        all_python = list(foundry_dir.rglob("*.py"))
        console_dir = foundry_dir / "console"
        api_paths = []
        if console_dir.exists():
            api_suffixes = {
                ".py",
                ".js",
                ".html",
                ".json",
                ".yaml",
                ".yml",
                ".graphql",
                ".gql",
            }
            api_paths = [
                path for path in console_dir.rglob("*") if path.suffix.lower() in api_suffixes
            ]
        index_names = {"projector.py", "readmodel.py", "lake.py"}
        indexing_paths = [path for path in all_python if path.name in index_names]
        event_paths = [foundry_dir / name for name in ("events.py", "namespaces.py")]
        event_paths.extend(
            path for path in (base / "VERSION", base / "CHANGELOG-DATA.md") if path.exists()
        )
        add_matches("api", api_paths)
        add_matches("indexing", indexing_paths)
        add_matches("event_contracts", event_paths)
        specialized_paths = set(api_paths + indexing_paths)
        specialized_paths.update(path for path in event_paths if path.is_relative_to(foundry_dir))
        add_matches("applications", [path for path in all_python if path not in specialized_paths])

    migrations_dir = base / MIGRATIONS_DIRNAME
    if migrations_dir.exists():
        add_matches("migration_scripts", list(migrations_dir.rglob("*.rq")), iri_only=True)

    return result


def scan_data_impact(term_iris: list[str], data_paths: list[str | Path]) -> dict:
    """Count data items mentioning changed terms from supplied sources.

    Supports RDF graph files, JSON/JSONL records, and Foundry Parquet lake
    files. A lake directory with ``manifest.json`` is manifest-authoritative.
    Every authoritative data source must be supplied explicitly; the lexical
    scan intentionally reports candidates and does not claim SHACL or
    application-level validity.
    """
    if not data_paths:
        raise ValueError("provide every authoritative historical data source with --data")

    supported_rdf = {".ttl", ".nt", ".n3", ".nq", ".nquads", ".trig", ".jsonld"}
    supported_json = {".json", ".jsonl", ".ndjson"}
    supported = supported_rdf | supported_json | {".parquet"}
    files: list[Path] = []
    sources: list[str] = []
    for raw_path in data_paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"historical data source does not exist: {path}")
        sources.append(str(path))
        if path.is_dir() and (path / "manifest.json").is_file():
            try:
                manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid lake manifest: {path / 'manifest.json'}") from exc
            if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
                raise ValueError(f"invalid lake manifest files list: {path / 'manifest.json'}")
            entries = manifest["files"]
            candidates = []
            for entry in entries:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("path"), str)
                    or not entry["path"]
                ):
                    raise ValueError(f"invalid lake manifest path entry: {entry!r}")
                relative = Path(entry["path"])
                candidate = (path / relative).resolve()
                if not candidate.is_relative_to(path):
                    raise ValueError(f"invalid lake manifest path: {relative}")
                if candidate.suffix.lower() != ".parquet":
                    raise ValueError(f"manifest entry is not a Parquet file: {relative}")
                if not candidate.is_file():
                    raise ValueError(f"manifest Parquet file is missing: {candidate}")
                candidates.append(candidate)
        elif path.is_dir():
            candidates = sorted(item for item in path.rglob("*") if item.is_file())
        else:
            candidates = [path]
        for candidate in candidates:
            if candidate.suffix.lower() in supported and candidate not in files:
                files.append(candidate)
    if not files:
        raise ValueError("no supported RDF, JSON, JSONL, or Parquet files found in --data sources")

    local_names = {iri: local_name(iri) for iri in term_iris}
    local_patterns = {
        iri: re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])") for iri, name in local_names.items()
    }
    matches_by_term = dict.fromkeys(term_iris, 0)
    items_scanned = 0
    matched_items = 0

    def record_matches(text: str) -> None:
        nonlocal matched_items
        matched = False
        for iri, pattern in local_patterns.items():
            if iri in text or pattern.search(text):
                matches_by_term[iri] += 1
                matched = True
        if matched:
            matched_items += 1

    for path in files:
        suffix = path.suffix.lower()
        if suffix in supported_json:
            with path.open(encoding="utf-8") as source:
                if suffix in {".jsonl", ".ndjson"}:
                    for line_number, line in enumerate(source, start=1):
                        if not line.strip():
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise ValueError(
                                f"invalid JSON in {path}:{line_number}: {exc}"
                            ) from exc
                        items_scanned += 1
                        record_matches(json.dumps(record, ensure_ascii=False, sort_keys=True))
                else:
                    try:
                        payload = json.load(source)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
                    records = payload if isinstance(payload, list) else [payload]
                    for record in records:
                        items_scanned += 1
                        record_matches(json.dumps(record, ensure_ascii=False, sort_keys=True))
        elif suffix == ".parquet":
            try:
                import pyarrow.parquet as pq
            except ImportError as exc:
                raise ValueError("pyarrow is required to scan historical Parquet data") from exc
            try:
                parquet = pq.ParquetFile(path)
                for batch in parquet.iter_batches(batch_size=1024):
                    for record in batch.to_pylist():
                        items_scanned += 1
                        record_matches(json.dumps(record, ensure_ascii=False, sort_keys=True))
            except Exception as exc:
                raise ValueError(f"cannot scan historical Parquet file {path}: {exc}") from exc
        else:
            graph = Graph()
            try:
                graph.parse(path.as_posix())
            except Exception as exc:
                raise ValueError(f"cannot parse historical RDF file {path}: {exc}") from exc
            for triple in graph:
                items_scanned += 1
                record_matches(" ".join(str(term) for term in triple))

    return {
        "status": "scanned",
        "sources": sources,
        "files_scanned": len(files),
        "items_scanned": items_scanned,
        "matched_items": matched_items,
        "matches_by_term": matches_by_term,
        "counted_unit": "RDF triple, JSON item, non-empty JSONL record, or Parquet row",
        "matching_method": "term IRI or delimiter-bounded local-name search",
    }


def build_impact_report(
    *,
    module: str,
    old_version: str,
    new_version: str,
    changes: list[Change],
    data_paths: list[str | Path],
    repo_root: Path | None = None,
) -> dict:
    """Create the review report that must accompany a breaking migration."""
    term_names = changed_term_names(changes)
    return {
        "module": module,
        "from_version": old_version,
        "to_version": new_version,
        "compatibility": classify_compatibility(changes),
        "changes": [
            {
                "target": change.target,
                "kind": change.kind,
                "semver_severity": change.severity,
                "compatibility": change_compatibility(change),
                "detail": change.detail,
            }
            for change in changes
        ],
        "consumers": blast_radius_for_terms(term_names, repo_root=repo_root),
        "historical_data": scan_data_impact([change.target for change in changes], data_paths),
        "limitations": [
            "Consumer references are lexical leads and require review.",
            "Data scan reports items mentioning changed terms; it is not a validity check.",
            "Every authoritative dataset must be included in the supplied --data paths.",
        ],
    }


def stability_report(releases: list[dict]) -> list[tuple[str, float, int, int]]:
    """Compute Stability(m) = 1 - N_breaking/N_releases per module."""
    by_module: dict[str, list[dict]] = {}
    for release in releases:
        by_module.setdefault(release["module_iri"], []).append(release)

    report: list[tuple[str, float, int, int]] = []
    for module_iri in sorted(by_module):
        entries = by_module[module_iri]
        total = len(entries)
        breaking = sum(1 for e in entries if e.get("severity") == SEVERITY_MAJOR)
        stability = round(1 - breaking / total, 4)
        report.append((module_iri, stability, breaking, total))
    return report


def render_changelog(releases: list[dict]) -> str:
    """Render the full changelog deterministically from the registry."""
    lines = [
        "# Ontology Changelog",
        "",
        "Generated from `registry/releases.json` by "
        "`tools/manage_ontology.py release`. Do not edit by hand.",
        "",
    ]
    by_module: dict[str, list[dict]] = {}
    for release in releases:
        by_module.setdefault(release["module_iri"], []).append(release)

    for module_iri in sorted(by_module):
        lines.append(f"## {local_name(module_iri)}")
        lines.append("")
        entries = sorted(
            by_module[module_iri], key=lambda r: parse_version(r["version"]), reverse=True
        )
        for entry in entries:
            lines.append(f"### {entry['version']} ({entry['date']}) - {entry['severity']}")
            lines.append("")
            if entry.get("commit"):
                lines.append(f"Commit: `{entry['commit']}`")
                lines.append("")
            changes = entry.get("changes", [])
            if changes:
                for change in changes:
                    marker = {"added": "+", "removed": "-", "changed": "~"}.get(change["kind"], "*")
                    name = local_name(change["target"])
                    lines.append(f"- {marker} [{change['severity']}] {name}: {change['detail']}")
            else:
                lines.append("- Initial baseline release.")
            if entry.get("migration"):
                lines.append("")
                lines.append("#### Migration")
                lines.append("")
                lines.append(str(entry["migration"]))
            if entry.get("migration_script"):
                lines.append("")
                lines.append(f"Migration script: `{entry['migration_script']}`")
            lines.append("")
    return "\n".join(lines) + "\n"


def cmd_release(args: argparse.Namespace) -> int:
    """Record a release: validate, snapshot terms, log entry, regenerate changelog."""
    registry = registry_dir()
    releases = load_releases(registry)

    matched = []
    for path in find_module_files():
        graph = load_graph(path)
        iri, declared = module_identity(graph)
        if args.module in {local_name(iri), str(path.relative_to(REPO_ROOT))}:
            matched.append((path, graph, iri, declared))

    if not matched:
        print(f"No module matches '{args.module}'. Use a module name like 'core' or a path.")
        return 1

    failures: list[str] = []
    prepared: list[tuple] = []
    for path, graph, iri, declared in matched:
        local = local_name(iri)
        rel = path.relative_to(REPO_ROOT).as_posix()
        current_terms = snapshot(graph)
        latest = latest_release(releases, iri)

        if latest is None:
            severity, changes = SEVERITY_NONE, []
        else:
            latest_terms = load_snapshot_terms(registry, local, latest["version"])
            changes = diff_snapshots(latest_terms, current_terms)
            if not changes:
                failures.append(
                    f"{local}: no semantic changes since {latest['version']}; nothing to release"
                )
                continue
            severity = highest_severity(changes)
            status, message = evaluate_module_version(latest, latest_terms, declared, current_terms)
            if status == "error":
                failures.append(f"{local}: {message}")
                continue
            migration = getattr(args, "migration", None)
            migration_script = getattr(args, "migration_script", None)
            if severity == SEVERITY_MAJOR and not migration:
                failures.append(
                    f"{local}: MAJOR release requires --migration (never redefine silently)"
                )
                continue
            if migration_script:
                script = REPO_ROOT / migration_script
                if severity != SEVERITY_MAJOR:
                    failures.append(f"{local}: --migration-script only applies to MAJOR releases")
                    continue
                if not script.exists():
                    failures.append(f"{local}: migration script not found: {script}")
                    continue
                impact_path = script.with_suffix(".impact.json")
                if not impact_path.is_file():
                    failures.append(f"{local}: migration impact report not found: {impact_path}")
                    continue
                try:
                    impact = json.loads(impact_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    failures.append(f"{local}: cannot read migration impact report: {exc}")
                    continue
                if not isinstance(impact, dict):
                    failures.append(f"{local}: migration impact report must contain a JSON object")
                    continue
                historical_data = impact.get("historical_data")
                try:
                    script_sha256 = hashlib.sha256(script.read_bytes()).hexdigest()
                except OSError as exc:
                    failures.append(f"{local}: cannot hash migration script: {exc}")
                    continue
                if (
                    impact.get("module") != local
                    or impact.get("from_version") != latest["version"]
                    or impact.get("to_version") != declared
                    or impact.get("compatibility") != COMPATIBILITY_BREAKING
                    or impact.get("migration_script") != _display_path(script.resolve())
                    or impact.get("migration_script_sha256") != script_sha256
                    or not isinstance(historical_data, dict)
                    or historical_data.get("status") != "scanned"
                    or not isinstance(historical_data.get("sources"), list)
                    or not historical_data["sources"]
                ):
                    failures.append(
                        f"{local}: migration impact report does not match this breaking release"
                    )
                    continue

        prepared.append((local, iri, rel, declared, severity, changes))

    if failures:
        for failure in failures:
            print(f"[error] {failure}")
        return 1

    if args.dry_run:
        for local, _iri, _rel, declared, severity, changes in prepared:
            print(f"{local} -> {declared} [{severity}/{classify_compatibility(changes)}]")
            radius = blast_radius_for_terms(changed_term_names(changes))
            for category, files in radius.items():
                if files:
                    print(f"  {category}: {', '.join(files)}")
        print("\nDry run only; re-run without --dry-run to record.")
        return 0

    commit_hash = current_commit()
    for local, iri, rel, declared, severity, changes in prepared:
        save_snapshot(registry, local, declared, iri, rel, snapshot(load_graph(REPO_ROOT / rel)))
        releases.append(
            {
                "module_iri": iri,
                "file": rel,
                "version": declared,
                "date": utc_now_iso(),
                "severity": severity,
                "compatibility": classify_compatibility(changes),
                "changes": [
                    {
                        "kind": change.kind,
                        "target": change.target,
                        "detail": change.detail,
                        "severity": change.severity,
                    }
                    for change in changes
                ],
                "migration": migration if severity == SEVERITY_MAJOR else None,
                "migration_script": (migration_script if severity == SEVERITY_MAJOR else None),
                "commit": commit_hash,
            }
        )
    save_releases(registry, releases)

    docs_dir = REPO_ROOT / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "CHANGELOG.md").write_text(render_changelog(releases), encoding="utf-8")

    for local, _iri, _rel, declared, severity, _changes in prepared:
        tag = f"{local}/{declared}"
        print(f"Released {local} {declared} [{severity}]")
        print(f"Suggested: git add registry/ docs/CHANGELOG.md && git commit && git tag {tag}")
    return 0


def cmd_blast_radius(args: argparse.Namespace) -> int:
    """Report consumers of a term across schema, API and projection layers."""
    term = args.term
    name = local_name(term) if "://" in term else term
    radius = blast_radius_for_terms([name])
    score = sum(len(files) for files in radius.values())
    print(f"Blast radius for '{name}': BR = {score}")
    for category, files in radius.items():
        listing = ", ".join(files) if files else "none"
        print(f"  {category} ({len(files)}): {listing}")
    return 0


def cmd_stability(_args: argparse.Namespace) -> int:
    """Report Semantic Stability per module against roadmap thresholds."""
    releases = load_releases(registry_dir())
    if not releases:
        print("No releases recorded yet; run 'release' first.")
        return 0

    violations = 0
    for module_iri, stability, breaking, total in stability_report(releases):
        threshold = (
            STABILITY_THRESHOLD_CORE
            if module_iri.endswith("/core")
            else STABILITY_THRESHOLD_DEFAULT
        )
        verdict = "ok" if stability >= threshold else f"WARN below {threshold}"
        print(
            f"{local_name(module_iri):<20} stability={stability:<7} "
            f"(breaking {breaking}/{total}) [{verdict}]"
        )
        if stability < threshold:
            violations += 1
    if violations:
        print(f"\n{violations} module(s) below their stability threshold.")
        return 1
    return 0


def collect_defined_term_iris(module_paths: list[Path]) -> set[str]:
    """Every class/property/individual IRI defined across the given modules."""
    type_targets = (
        OWL.Class,
        OWL.ObjectProperty,
        OWL.DatatypeProperty,
        OWL.AnnotationProperty,
        OWL.NamedIndividual,
    )
    iris: set[str] = set()
    for path in module_paths:
        graph = load_graph(path)
        for target in type_targets:
            iris.update(str(subject) for subject in graph.subjects(RDF.type, target))
    return iris


def alignments_file(repo_root: Path | None = None) -> Path:
    base = repo_root if repo_root is not None else REPO_ROOT
    return base / REGISTRY_DIRNAME / ALIGNMENTS_FILENAME


def load_alignments(path: Path) -> list[dict]:
    if not path.exists():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    return list(document.get("alignments", []))


def save_alignments(path: Path, alignments: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "alignments": alignments}, indent=2) + "\n",
        encoding="utf-8",
    )


def cmd_align_add(args: argparse.Namespace) -> int:
    """Record one semantic alignment; source term must exist in the modules."""
    path = alignments_file()
    alignments = load_alignments(path)
    key = (args.source, args.target, args.relation)
    if any((a["source"], a["target"], a["relation"]) == key for a in alignments):
        print(f"[error] alignment already recorded: {args.source} {args.relation} {args.target}")
        return 1
    if args.source not in collect_defined_term_iris(find_module_files()):
        print(f"[error] source term is not defined in any module: {args.source}")
        return 1
    alignments.append(
        {
            "source": args.source,
            "target": args.target,
            "relation": args.relation,
            "note": args.note,
            "added": utc_now_iso(),
        }
    )
    save_alignments(path, alignments)
    print(f"Aligned {args.source} {args.relation} {args.target}")
    return 0


def cmd_align_list(_args: argparse.Namespace) -> int:
    alignments = load_alignments(alignments_file())
    if not alignments:
        print("No alignments recorded.")
        return 0
    for alignment in alignments:
        note = f"  # {alignment['note']}" if alignment.get("note") else ""
        print(
            f"{local_name(alignment['source'])} {alignment['relation']} {alignment['target']}{note}"
        )
    return 0


def cmd_align_check(_args: argparse.Namespace) -> int:
    """Fail if any recorded alignment points at a term the modules no longer define."""
    alignments = load_alignments(alignments_file())
    term_iris = collect_defined_term_iris(find_module_files())
    errors: list[str] = []
    for alignment in alignments:
        if alignment["relation"] not in ALIGNMENT_RELATIONS:
            errors.append(f"unknown relation '{alignment['relation']}' on {alignment['source']}")
        if alignment["source"] not in term_iris:
            errors.append(f"source term no longer defined: {alignment['source']}")
    if errors:
        for error in errors:
            print(f"[error] {error}")
        return 1
    print(f"Alignment check passed ({len(alignments)} alignments).")
    return 0


def generate_migration_script(
    module: str,
    old_version: str,
    new_version: str,
    changes: list[Change],
    out_dir: Path | None = None,
) -> Path:
    """Emit a reviewable SPARQL Update script for the MAJOR changes in `changes`.

    Removed properties become DELETE statements (assertions of a term that no
    longer exists are garbage and must go). Removed classes only produce a
    commented investigation query — dropping class assertions silently would
    destroy typing that may need remapping first. Domain/range changes become
    revalidation notes. Raises ValueError when there is nothing breaking.
    """
    breaking = [change for change in changes if change.severity == SEVERITY_MAJOR]
    if not breaking:
        raise ValueError("no MAJOR changes detected; nothing to migrate")

    base = out_dir if out_dir is not None else REPO_ROOT / MIGRATIONS_DIRNAME / module
    base.mkdir(parents=True, exist_ok=True)
    script_path = base / f"{old_version}_to_{new_version}.rq"

    lines = [
        f"# Migration: {module} {old_version} -> {new_version}",
        "# Generated by `manage_ontology.py migrate` — REVIEW BEFORE APPLYING.",
        f"# Review companion impact report: {old_version}_to_{new_version}.impact.json.",
        "# Apply against the canonical data store; take a backup or rely on event replay.",
        "",
    ]
    for change in breaking:
        if change.kind == "removed" and "property" in change.detail:
            lines += [
                f"# {change.detail}: {change.target}",
                f"DELETE {{ ?s <{change.target}> ?o }} WHERE {{ ?s <{change.target}> ?o }} ;",
                "",
            ]
        elif change.kind == "removed":
            lines += [
                f"# {change.detail}: {change.target}",
                "# Investigate and remap instances before deleting class assertions:",
                f"# SELECT ?instance WHERE {{ ?instance a <{change.target}> }}",
                "",
            ]
    for change in breaking:
        if change.kind != "changed":
            continue
        fields = " ".join(change.field_changes)
        if "domain" in fields or "range" in fields:
            lines += [
                f"# NOTE {local_name(change.target)}: {'; '.join(change.field_changes)}.",
                "# Existing data may violate the new constraint.",
                "# Re-run SHACL validation after applying this script.",
                "",
            ]
    script_path.write_text("\n".join(lines), encoding="utf-8")
    return script_path


def _display_path(path: Path) -> str:
    """Path relative to repo root when possible (files may live outside it, e.g. tests)."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def cmd_migrate(args: argparse.Namespace) -> int:
    """Generate a migration script and its compatibility/data impact report."""
    registry = registry_dir()
    releases = load_releases(registry)
    for path in find_module_files():
        graph = load_graph(path)
        iri, declared = module_identity(graph)
        if args.module not in {local_name(iri), path.stem, _display_path(path)}:
            continue
        module = local_name(iri)
        latest = latest_release(releases, iri)
        if latest is None:
            print(f"[error] {module}: never released; no baseline to migrate from")
            return 1
        old_terms = load_snapshot_terms(registry, module, latest["version"])
        changes = diff_snapshots(old_terms, snapshot(graph))
        breaking = [change for change in changes if change.severity == SEVERITY_MAJOR]
        if not breaking:
            print(f"{module}: no MAJOR changes since {latest['version']}; nothing to migrate")
            return 1
        data_paths = getattr(args, "data", None) or []
        if not data_paths:
            print(
                "[error] migration preflight requires every authoritative historical data "
                "source via --data"
            )
            return 1
        try:
            impact = build_impact_report(
                module=module,
                old_version=latest["version"],
                new_version=declared,
                changes=changes,
                data_paths=data_paths,
            )
        except (OSError, ValueError) as exc:
            print(f"[error] cannot complete migration impact scan: {exc}")
            return 1

        script_path = generate_migration_script(module, latest["version"], declared, changes)
        impact["migration_script"] = _display_path(script_path.resolve())
        impact["migration_script_sha256"] = hashlib.sha256(script_path.read_bytes()).hexdigest()
        impact_path = script_path.with_suffix(".impact.json")
        impact_path.write_text(
            json.dumps(impact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"Generated {script_path}")
        print(f"Generated impact report {impact_path}")
        print(f"  Compatibility: {impact['compatibility']}")
        print(
            "  Historical data: "
            f"{impact['historical_data']['matched_items']} matching items / "
            f"{impact['historical_data']['items_scanned']} scanned"
        )
        for category in ("api", "indexing", "event_contracts", "shapes", "queries"):
            print(f"  {category}: {len(impact['consumers'][category])} consumer file(s)")
        for change in breaking:
            print(f"  [{change.severity}] {local_name(change.target)}: {change.detail}")
        print(
            "Review the report and script, then: release "
            f"{module} --migration '...' --migration-script {script_path}"
        )
        return 0
    print(f"No module matches '{args.module}'.")
    return 1


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

    sub.add_parser("check-versions", help="enforce version vs semantic diff").set_defaults(
        func=cmd_check_versions
    )

    release_parser = sub.add_parser("release", help="record a module release")
    release_parser.add_argument("module", help="module name (e.g. 'core') or repo path")
    release_parser.add_argument(
        "--migration",
        default=None,
        help="migration note; REQUIRED for MAJOR releases",
    )
    release_parser.add_argument(
        "--migration-script",
        default=None,
        help="generated MAJOR script; its matching .impact.json report is required",
    )
    release_parser.add_argument(
        "--dry-run", action="store_true", help="preview changes and blast radius only"
    )
    release_parser.set_defaults(func=cmd_release)

    radius_parser = sub.add_parser("blast-radius", help="consumers of a term")
    radius_parser.add_argument("term", help="term local name or full IRI")
    radius_parser.set_defaults(func=cmd_blast_radius)

    sub.add_parser("stability", help="stability per module vs thresholds").set_defaults(
        func=cmd_stability
    )

    migrate_parser = sub.add_parser(
        "migrate", help="generate a SPARQL Update migration for breaking changes"
    )
    migrate_parser.add_argument("module", help="module name (e.g. 'core') or repo path")
    migrate_parser.add_argument(
        "--data",
        action="append",
        required=True,
        metavar="PATH",
        help=(
            "historical RDF/JSON/JSONL/Parquet file or directory; repeat for every "
            "authoritative source (lake directories use manifest.json)"
        ),
    )
    migrate_parser.set_defaults(func=cmd_migrate)

    align_parser = sub.add_parser("align", help="semantic alignment registry")
    align_sub = align_parser.add_subparsers(dest="align_cmd", required=True)
    align_add = align_sub.add_parser("add", help="record one alignment")
    align_add.add_argument("source", help="source term IRI (must be defined in a module)")
    align_add.add_argument("target", help="target term IRI (external allowed)")
    align_add.add_argument("--relation", default="closeMatch", choices=ALIGNMENT_RELATIONS)
    align_add.add_argument("--note", default=None, help="why this alignment holds")
    align_add.set_defaults(func=cmd_align_add)
    align_sub.add_parser("list", help="show recorded alignments").set_defaults(func=cmd_align_list)
    align_sub.add_parser("check", help="validate alignments against current modules").set_defaults(
        func=cmd_align_check
    )

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
