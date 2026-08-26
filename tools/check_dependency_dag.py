#!/usr/bin/env python3
"""Enforce the ontology module dependency invariant.

Layers (allowed import direction: higher number may import lower number):

    0 = ontology/core      (semantic kernel — imports nothing above it)
    1 = ontology/middle    (may import core only)
    2 = ontology/domain(s), profiles/  (may import middle and core)

Fails on:
  * upward imports (e.g. core importing domain);
  * circular owl:imports within the same layer;
  * imports of unknown/unregistered module IRIs.

Usage:
    python tools/check_dependency_dag.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from rdflib import Graph
from rdflib.namespace import OWL

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directory prefix -> dependency layer (lower = more foundational).
LAYER_BY_PREFIX: list[tuple[str, int]] = [
    ("ontology/core", 0),
    ("ontology/middle", 1),
    ("ontology/domain", 2),
    ("profiles", 2),
]


def layer_of(file_path: Path) -> int | None:
    rel = file_path.relative_to(REPO_ROOT).as_posix()
    for prefix, layer in LAYER_BY_PREFIX:
        if rel.startswith(prefix):
            return layer
    return None


def find_module_files() -> list[Path]:
    files: list[Path] = []
    for root in ("ontology", "profiles"):
        base = REPO_ROOT / root
        if base.exists():
            files.extend(sorted(p for p in base.rglob("*.ttl") if p.is_file()))
    return files


def parse_module(path: Path) -> tuple[set[str], set[str]]:
    """Return (declared ontology IRIs, imported module IRIs) of a file."""
    g = Graph()
    g.parse(path.as_posix(), format="turtle")
    declared = {str(s) for s in g.subjects(None, OWL.Ontology)}
    imports = {str(o) for o in g.objects(None, OWL.imports)}
    return declared, imports


def main() -> int:
    files = find_module_files()
    errors: list[str] = []

    # module IRI -> (layer, path)
    modules: dict[str, tuple[int, Path]] = {}
    parsed: dict[Path, tuple[set[str], set[str]]] = {}

    for path in files:
        layer = layer_of(path)
        try:
            declared, imports = parse_module(path)
        except Exception as exc:
            errors.append(f"PARSE ERROR {path}: {exc}")
            continue
        parsed[path] = (declared, imports)

        if not declared:
            errors.append(f"NO ONTOLOGY HEADER: {path} does not declare an owl:Ontology IRI")
            continue

        for iri in declared:
            if layer is None:
                errors.append(
                    f"UNREGISTERED LOCATION: {path} (module <{iri}>) is outside known layers"
                )
                continue
            existing = modules.get(iri)
            if existing and existing[0] != layer:
                errors.append(
                    f"DUPLICATE MODULE IRI: <{iri}> declared at both "
                    f"{existing[1]} (layer {existing[0]}) and {path} (layer {layer})"
                )
            modules.setdefault(iri, (layer, path))

    # Import edges between registered modules.
    edges: list[tuple[str, str]] = []
    for declared, imports in parsed.values():
        src = next((iri for iri in declared if iri in modules), None)
        if src is None:
            continue
        for dst in sorted(imports):
            edges.append((src, dst))

    for src, dst in edges:
        dst_entry = modules.get(dst)
        if dst_entry is None:
            errors.append(f"UNKNOWN IMPORT: <{src}> imports unregistered module <{dst}>")
            continue
        src_layer = modules[src][0]
        dst_layer = dst_entry[0]
        if src_layer is not None and dst_layer > src_layer:
            errors.append(
                f"UPWARD DEPENDENCY: layer {src_layer} (<{src}>) imports "
                f"layer {dst_layer} (<{dst}>) — allowed direction is downward only"
            )

    # Cycle detection within the same layer.
    adj: dict[str, list[str]] = {}
    for src, dst in edges:
        s = modules.get(src)
        d = modules.get(dst)
        if s and d and s[0] == d[0]:
            adj.setdefault(src, []).append(dst)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}

    def dfs(node: str, stack: list[str]) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in adj.get(node, []):
            state = color.get(nxt, WHITE)
            if state == GRAY:
                cycle = [*stack[stack.index(nxt) :], nxt]
                errors.append(f"CIRCULAR IMPORT: {' -> '.join(cycle)}")
            elif state == WHITE:
                dfs(nxt, stack)
        stack.pop()
        color[node] = BLACK

    for node in list(adj):
        if color.get(node, WHITE) == WHITE:
            dfs(node, [])

    if errors:
        print("Dependency DAG check FAILED:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(
        f"Dependency DAG check PASSED "
        f"({len(files)} files, {len(edges)} import edges, no upward deps, no cycles)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
