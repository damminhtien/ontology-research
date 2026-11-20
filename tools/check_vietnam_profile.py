#!/usr/bin/env python3
"""Validate the Vietnam profile mappings, CQ catalog, and affiliation fixture."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pyshacl import validate
from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.namespace import OWL
from rdflib.plugins.sparql import prepareQuery

REPO_ROOT = Path(__file__).resolve().parents[1]
if REPO_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, REPO_ROOT.as_posix())
if (REPO_ROOT / "tools").as_posix() not in sys.path:
    sys.path.insert(0, (REPO_ROOT / "tools").as_posix())

from ontology_utils import find_module_files, materialize_type_closure  # noqa: E402

from foundry.osint_sources import VIETNAM_SCS  # noqa: E402

PROFILE_PATH = REPO_ROOT / "profiles" / "vietnam" / "profile.json"
PROFILE_ROOT = PROFILE_PATH.parent
FIXTURE_PATH = PROFILE_ROOT / "examples" / "affiliation.ttl"
SHAPE_PATH = PROFILE_ROOT / "affiliation_shapes.ttl"
CQ_ROOT = REPO_ROOT / "benchmarks" / "queries"
EXPECTED_ROOT = REPO_ROOT / "benchmarks" / "expected_results"
XSD_DATETIME = "http://www.w3.org/2001/XMLSchema#dateTime"


def _load_graph(paths: list[Path]) -> Graph:
    graph = Graph()
    for path in paths:
        graph.parse(path.as_posix(), format="turtle")
    return graph


def _binding_kind(value: dict) -> str | None:
    if not isinstance(value, dict):
        return None
    if set(value) == {"iri"}:
        return "iri"
    if set(value) <= {"literal", "datatype", "lang"} and "literal" in value:
        return "datetime" if value.get("datatype") == XSD_DATETIME else "literal"
    return None


def validate_profile(profile: dict, ontology: Graph, root: Path = REPO_ROOT) -> list[str]:
    """Return contract errors for profile metadata and registered query templates."""
    errors: list[str] = []
    if profile.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if profile.get("profile_id") != "urn:world:profile:vietnam-scs":
        errors.append("profile_id must be urn:world:profile:vietnam-scs")
    if profile.get("preferred_languages") != ["vi", "en"]:
        errors.append("preferred_languages must put Vietnamese and English first")

    expected_area = {
        "name": VIETNAM_SCS.name,
        "south": VIETNAM_SCS.south,
        "west": VIETNAM_SCS.west,
        "north": VIETNAM_SCS.north,
        "east": VIETNAM_SCS.east,
    }
    if profile.get("area_of_interest") != expected_area:
        errors.append("area_of_interest must match the VIETNAM_SCS connector bounds")

    for key, class_iri in profile.get("entity_type_mappings", {}).items():
        if (URIRef(class_iri), RDF.type, OWL.Class) not in ontology:
            errors.append(f"entity_type_mappings.{key} is not a declared OWL class: {class_iri}")
    for key, property_iri in profile.get("relationship_mappings", {}).items():
        if (URIRef(property_iri), RDF.type, OWL.ObjectProperty) not in ontology:
            errors.append(
                f"relationship_mappings.{key} is not a declared object property: {property_iri}"
            )

    types = profile.get("entity_type_mappings", {})
    core = "https://damminhtien.github.io/ontology-research/ontology/core#"
    location = "https://damminhtien.github.io/ontology-research/ontology/middle/location#"
    expected_types = {
        "vessel": core + "Platform",
        "aircraft": core + "Platform",
        "country": location + "Country",
        "administrative_region": location + "AdministrativeRegion",
    }
    for key, expected in expected_types.items():
        if types.get(key) != expected:
            errors.append(f"{key} must reuse the shared ontology class {expected}")

    relationships = profile.get("relationship_mappings", {})
    expected_relations = {
        "membership": core + "memberOf",
        "role_assignment": core + "hasRole",
        "event_participation": core + "participatesIn",
    }
    for key, expected in expected_relations.items():
        if relationships.get(key) != expected:
            errors.append(f"{key} must use the shared predicate {expected}")

    signature_checks = {
        core + "memberOf": (core + "Agent", core + "Agent"),
        core + "hasRole": (core + "Agent", core + "Role"),
        core + "participatesIn": (core + "Entity", core + "Event"),
    }
    for prop, expected_signature in signature_checks.items():
        actual_signature = tuple(
            str(ontology.value(URIRef(prop), term)) for term in (RDFS.domain, RDFS.range)
        )
        if actual_signature != expected_signature:
            errors.append(f"unexpected domain/range for {prop}: {actual_signature}")

    country = profile.get("country_reference", {})
    if country.get("namespace") != "wikidata" or country.get("identifier") != "Q881":
        errors.append("country_reference must preserve the Wikidata Q881 external identifier")
    if country.get("entity_class") != location + "Country":
        errors.append("country_reference must map to location:Country")

    for template in profile.get("semantic_query_templates", []):
        template_id = template.get("id", "")
        spec_path = root / "benchmarks" / "expected_results" / f"{template_id.lower()}.json"
        query_path = root / "benchmarks" / "queries" / template.get("query_file", "")
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append(f"missing competency contract for {template_id}")
            continue
        if spec.get("query_file") != template.get("query_file"):
            errors.append(f"{template_id} query_file differs from its CQ contract")
        expected_bindings = {
            name: _binding_kind(value) for name, value in (spec.get("bindings") or {}).items()
        }
        if template.get("bindings") != expected_bindings:
            errors.append(f"{template_id} binding types differ from its CQ contract")
        try:
            if query_path.parent.resolve() != CQ_ROOT.resolve() or not query_path.is_file():
                raise ValueError("query must be a file directly under benchmarks/queries")
            prepared = prepareQuery(query_path.read_text(encoding="utf-8"))
            if getattr(prepared.algebra, "name", None) != "SelectQuery":
                errors.append(f"{template_id} must be a SELECT query")
        except Exception as exc:
            errors.append(f"{template_id} query is invalid: {exc}")

    for path in (root / "profiles" / "vietnam").rglob("*.ttl"):
        try:
            local_graph = _load_graph([path])
        except Exception as exc:
            errors.append(f"could not parse profile RDF file {path}: {exc}")
            continue
        forbidden_types = {OWL.Ontology, OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty}
        if any(any(local_graph.triples((None, RDF.type, term))) for term in forbidden_types):
            errors.append(
                f"profile RDF must use shared ontology terms, not declare ontology terms: {path}"
            )

    return errors


def _validate_affiliation_fixture(ontology: Graph) -> list[str]:
    data = Graph()
    data += ontology
    data.parse(FIXTURE_PATH.as_posix(), format="turtle")
    materialize_type_closure(data)
    shapes = _load_graph([REPO_ROOT / "shapes" / "assertion_shapes.ttl", SHAPE_PATH])
    conforms, _results, report = validate(data_graph=data, shacl_graph=shapes, advanced=True)
    if not conforms:
        return [report]

    core = "https://damminhtien.github.io/ontology-research/ontology/core#"
    example = "https://data.example/vietnam-profile/"
    if (URIRef(example + "role"), RDF.type, URIRef(core + "Agent")) in data:
        return ["a core:Role fixture was incorrectly inferred or asserted as a core:Agent"]
    return []


def main() -> int:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    ontology = _load_graph(find_module_files())
    errors = validate_profile(profile, ontology)
    errors.extend(_validate_affiliation_fixture(ontology))
    if errors:
        print("Vietnam profile check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(
        "Vietnam profile check passed: shared ontology mappings, 3 CQ templates, "
        "and affiliation SHACL fixture conform."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
