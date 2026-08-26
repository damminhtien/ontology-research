#!/usr/bin/env python3
"""Validate RDF data against SHACL shapes using pyshacl.

Usage:
    python tools/validate.py --shapes shapes/core_shapes.ttl \
                             --data benchmarks/datasets/sample_data.ttl

Exits non-zero if any SHACL violation is found. Designed to run in CI:
any shape violation must fail the pipeline before data reaches the KG.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ontology_utils import materialize_type_closure
from pyshacl import validate
from rdflib import Graph

FORMATS = {
    ".ttl": "turtle",
    ".n3": "n3",
    ".nt": "nt",
    ".rdf": "xml",
    ".owl": "xml",
    ".jsonld": "json-ld",
}


def load(paths: list[Path]) -> Graph:
    graph = Graph()
    for path in paths:
        fmt = FORMATS.get(path.suffix.lower())
        if fmt is None:
            raise ValueError(f"Unsupported RDF file extension: {path}")
        try:
            graph.parse(path.as_posix(), format=fmt)
        except Exception as exc:  # surface a clear boundary error
            raise RuntimeError(f"Failed to parse {path}: {exc}") from exc
    return graph


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shapes",
        action="append",
        required=True,
        help="SHACL shapes file (repeatable)",
    )
    parser.add_argument(
        "--data",
        action="append",
        required=True,
        help="Data file to validate (repeatable)",
    )
    parser.add_argument(
        "--ontology",
        action="append",
        default=None,
        help="Ontology module providing class axioms for type expansion "
        "(repeatable; defaults to ontology/core/core.ttl)",
    )
    args = parser.parse_args()

    ontology_paths = (
        [Path(p) for p in args.ontology]
        if args.ontology
        else [Path(__file__).resolve().parent.parent / "ontology" / "core" / "core.ttl"]
    )
    shapes_paths = [Path(p) for p in args.shapes]
    data_paths = [Path(p) for p in args.data]
    for p in ontology_paths + shapes_paths + data_paths:
        if not p.is_file():
            print(f"ERROR: file not found: {p}", file=sys.stderr)
            return 2

    shapes = load(shapes_paths)
    data = load(ontology_paths + data_paths)
    expanded = materialize_type_closure(data)

    conforms, _results_graph, results_text = validate(
        data_graph=data,
        shacl_graph=shapes,
        inference="none",
        advanced=True,
    )

    print(results_text)
    if not conforms:
        print("RESULT: FAIL")
        return 1

    print(f"RESULT: PASS ({expanded} subclass-closure triples materialized)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
