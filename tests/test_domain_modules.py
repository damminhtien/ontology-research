"""Tests for the domain ontology modules (Phase 4 DoD).

Covers the tracking vertical: middle-layer geography/organization and
domain-layer sensor/tracking modules - positive and negative reasoning,
domain/range coverage, module headers and the DAG import invariant.
"""

from __future__ import annotations

import subprocess
import sys

import manage_ontology as mgmt
import pytest
from conftest import CORE_ONTOLOGY, REPO_ROOT, type_closure
from rdflib import RDF, Graph, URIRef
from rdflib.namespace import RDFS

MIDDLE = {
    "location": REPO_ROOT / "ontology" / "middle" / "location.ttl",
    "organization": REPO_ROOT / "ontology" / "middle" / "organization.ttl",
}
DOMAIN = {
    "sensor": REPO_ROOT / "ontology" / "domain" / "sensor.ttl",
    "tracking": REPO_ROOT / "ontology" / "domain" / "tracking.ttl",
}

LOCATION = "https://damminhtien.github.io/ontology-research/ontology/middle/location#"
ORG = "https://damminhtien.github.io/ontology-research/ontology/middle/organization#"
CORE = "https://damminhtien.github.io/ontology-research/ontology/core#"
EX = "https://data.example/entity/"


def load_module(*paths) -> Graph:
    """Load the kernel plus the given module files into one graph."""
    graph = Graph()
    graph.parse(CORE_ONTOLOGY.as_posix(), format="turtle")
    for path in paths:
        graph.parse(path.as_posix(), format="turtle")
    return graph


class TestMiddleModules:
    def test_module_headers_are_registered(self):
        for iri_suffix, path in (
            ("location", MIDDLE["location"]),
            ("organization", MIDDLE["organization"]),
        ):
            iri, version = mgmt.module_identity(mgmt.load_graph(path))
            assert (
                iri
                == f"https://damminhtien.github.io/ontology-research/ontology/middle/{iri_suffix}"
            )
            assert version == "0.1.0"

    def test_geography_classes_subclass_location(self):
        graph = load_module(MIDDLE["location"])
        country = URIRef(EX + "x-country")
        graph.add((country, RDF.type, URIRef(LOCATION + "Country")))
        closed = type_closure(graph, country)
        assert URIRef(CORE + "Location") in closed
        assert URIRef(CORE + "Entity") in closed

    def test_administrative_region_closure(self):
        graph = load_module(MIDDLE["location"])
        region = URIRef(EX + "x-region")
        graph.add((region, RDF.type, URIRef(LOCATION + "AdministrativeRegion")))
        closed = type_closure(graph, region)
        assert URIRef(CORE + "Location") in closed
        assert URIRef(CORE + "PhysicalObject") not in closed

    def test_subordinate_to_has_domain_and_range(self):
        graph = load_module(MIDDLE["organization"])
        prop = URIRef(ORG + "subordinateTo")
        domain = next(graph.objects(prop, RDFS.domain), None)
        rng = next(graph.objects(prop, RDFS.range), None)
        assert domain == URIRef(CORE + "Organization")
        assert rng == URIRef(CORE + "Organization")

    def test_dag_check_passes_with_middle_layer(self):
        result = subprocess.run(
            [sys.executable, (REPO_ROOT / "tools" / "check_dependency_dag.py").as_posix()],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT.as_posix(),
        )
        assert result.returncode == 0, result.stdout + result.stderr


DOMAIN_SENSOR = "https://damminhtien.github.io/ontology-research/ontology/domain/sensor#"
DOMAIN_TRACKING = "https://damminhtien.github.io/ontology-research/ontology/domain/tracking#"


class TestDomainModules:
    """Domain-layer invariants for the tracking vertical."""

    def test_module_headers_are_registered(self):
        for iri_suffix, path in (
            ("sensor", DOMAIN["sensor"]),
            ("tracking", DOMAIN["tracking"]),
        ):
            iri, version = mgmt.module_identity(mgmt.load_graph(path))
            assert (
                iri
                == f"https://damminhtien.github.io/ontology-research/ontology/domain/{iri_suffix}"
            )
            assert version == "0.1.0"

    def test_sensor_closure_reaches_entity(self):
        graph = load_module(DOMAIN["sensor"])
        sensor = URIRef(EX + "x-sensor")
        graph.add((sensor, RDF.type, URIRef(DOMAIN_SENSOR + "Sensor")))
        closed = type_closure(graph, sensor)
        for cls in ("Artifact", "PhysicalObject", "Entity"):
            assert URIRef(CORE + cls) in closed

    def test_track_closure_is_information_object_and_never_event(self):
        graph = load_module(DOMAIN["tracking"])
        track = URIRef(EX + "x-track")
        graph.add((track, RDF.type, URIRef(DOMAIN_TRACKING + "Track")))
        closed = type_closure(graph, track)
        assert URIRef(CORE + "InformationObject") in closed
        assert URIRef(CORE + "Entity") in closed
        # ADR-0005: a track is derived information, never a world event.
        assert URIRef(CORE + "Event") not in closed

    def test_track_of_does_not_reclassify_target_as_track(self):
        """Negative inference: asserting a link must never re-type its object."""
        graph = load_module(DOMAIN["tracking"])
        vessel = URIRef(EX + "x-vessel")
        track = URIRef(EX + "x-track")
        graph.add((track, RDF.type, URIRef(DOMAIN_TRACKING + "Track")))
        graph.add((track, URIRef(DOMAIN_TRACKING + "trackOf"), vessel))
        graph.add((vessel, RDF.type, URIRef(CORE + "Platform")))
        assert (vessel, RDF.type, URIRef(DOMAIN_TRACKING + "Track")) not in graph
        assert URIRef(DOMAIN_TRACKING + "Track") not in type_closure(graph, vessel)

    def test_detected_by_does_not_reclassify_observation_as_sensor(self):
        graph = load_module(DOMAIN["sensor"])
        obs = URIRef(EX + "x-obs")
        sensor = URIRef(EX + "x-sensor")
        graph.add((obs, RDF.type, URIRef(CORE + "Observation")))
        graph.add((obs, URIRef(DOMAIN_SENSOR + "detectedBy"), sensor))
        assert (obs, RDF.type, URIRef(DOMAIN_SENSOR + "Sensor")) not in graph

    @pytest.mark.parametrize(
        ("prop_name", "domain_cls", "range_cls"),
        [
            ("mountedOn", "sensor:Sensor", "core:Platform"),
            ("detectedBy", "core:Observation", "sensor:Sensor"),
            ("trackOf", "tracking:Track", "core:Entity"),
            ("derivedFrom", "tracking:Track", "core:Observation"),
        ],
    )
    def test_domain_object_properties_have_complete_axioms(self, prop_name, domain_cls, range_cls):
        ns = DOMAIN_SENSOR if prop_name in {"mountedOn", "detectedBy"} else DOMAIN_TRACKING
        prop = URIRef(ns + prop_name)
        graph = load_module(DOMAIN["sensor"], DOMAIN["tracking"])
        prop_types = {str(t) for t in graph.objects(prop, RDF.type)}
        assert "http://www.w3.org/2002/07/owl#ObjectProperty" in prop_types
        prefix_map = {
            "core": CORE,
            "sensor": DOMAIN_SENSOR,
            "tracking": DOMAIN_TRACKING,
        }

        def resolve(name: str) -> URIRef:
            prefix, local = name.split(":")
            return URIRef(prefix_map[prefix] + local)

        assert next(graph.objects(prop, RDFS.domain), None) == resolve(domain_cls)
        assert next(graph.objects(prop, RDFS.range), None) == resolve(range_cls)

    def test_track_id_is_datatype_property_with_string_range(self):
        prop = URIRef(DOMAIN_TRACKING + "hasTrackId")
        graph = load_module(DOMAIN["tracking"])
        types = {str(t) for t in graph.objects(prop, RDF.type)}
        assert "http://www.w3.org/2002/07/owl#DatatypeProperty" in types
        rng = next(graph.objects(prop, RDFS.range), None)
        assert str(rng) == "http://www.w3.org/2001/XMLSchema#string"


class TestDomainShaclConformance:
    """Domain data must satisfy domain shapes (and core shapes) — the DoD gate."""

    @staticmethod
    def _data_graph() -> Graph:
        from ontology_utils import find_module_files, materialize_type_closure

        graph = Graph()
        for path in find_module_files():
            graph.parse(path.as_posix(), format="turtle")
        for path in sorted((REPO_ROOT / "benchmarks" / "datasets").glob("*.ttl")):
            graph.parse(path.as_posix(), format="turtle")
        materialize_type_closure(graph)
        return graph

    @staticmethod
    def _validate(data: Graph, shapes_file: str = "domain_shapes.ttl") -> tuple[bool, str]:
        from pyshacl import validate as shacl_validate

        shapes = Graph()
        shapes.parse((REPO_ROOT / "shapes" / shapes_file).as_posix(), format="turtle")
        conforms, _results_graph, text = shacl_validate(
            data_graph=data, shacl_graph=shapes, inference="none", advanced=True
        )
        return conforms, text

    def test_full_data_conforms_to_domain_shapes(self):
        conforms, text = self._validate(self._data_graph())
        assert conforms, text

    def test_full_data_conforms_to_core_shapes(self):
        conforms, text = self._validate(self._data_graph(), shapes_file="core_shapes.ttl")
        assert conforms, text

    @staticmethod
    def _stripped_violation_graph(prop_iri: str, subject_iri: str) -> Graph:
        """Module+domain dataset graph with one assertion removed (violation fixture)."""
        from ontology_utils import find_module_files, materialize_type_closure

        graph = Graph()
        for path in find_module_files():
            graph.parse(path.as_posix(), format="turtle")
        graph.parse(
            (REPO_ROOT / "benchmarks" / "datasets" / "domain_tracking.ttl").as_posix(),
            format="turtle",
        )
        graph.remove((URIRef(subject_iri), URIRef(prop_iri), None))
        materialize_type_closure(graph)
        return graph

    def test_track_without_track_id_violates(self):
        graph = self._stripped_violation_graph(
            "https://damminhtien.github.io/ontology-research/ontology/domain/tracking#hasTrackId",
            "https://data.example/entity/track-t101",
        )
        conforms, _ = self._validate(graph)
        assert not conforms

    def test_sensor_without_mount_violates(self):
        graph = self._stripped_violation_graph(
            "https://damminhtien.github.io/ontology-research/ontology/domain/sensor#mountedOn",
            "https://data.example/entity/radar-01",
        )
        conforms, _ = self._validate(graph)
        assert not conforms
