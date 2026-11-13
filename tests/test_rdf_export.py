"""Contracts for the offline TriG RDF export profiles."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pyshacl import validate
from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, XSD

from foundry.assertions import make_assertion, register_document
from foundry.events import EventLog
from foundry.namespaces import CORE_ONTOLOGY_NS
from foundry.rdf_export import GEO, INTEROP_GRAPH_IRI, PROV, SOSA, project_event_log
from foundry.tracking import observation_event, sensor_event, track_event

CORE = CORE_ONTOLOGY_NS
ASSERTION = "https://damminhtien.github.io/ontology-research/ontology/middle/assertion#"
TRACKING = "https://damminhtien.github.io/ontology-research/ontology/domain/tracking#"
ENTITY = "urn:world:entity:ship-1"
SENSOR = "urn:world:entity:sensor-1"
LOCATION = "urn:world:location:harbour-1"
AT_TIME = "2026-09-01T03:04:05Z"


def _write_export_log(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    document = register_document(
        log=log,
        uri="https://data.example/report/1",
        title="Báo cáo vị trí",
        source_system="research-feed",
    )
    document_id = document.payload["document_id"]
    resolved = make_assertion(
        log=log,
        subject_id=ENTITY,
        predicate_iri=f"{CORE}locatedAt",
        object_kind="location",
        object_value=LOCATION,
        valid_from="2026-09-01T00:00:00Z",
        valid_until="2026-09-02T00:00:00Z",
        source_ids=[document_id, "urn:source:unregistered"],
        evidence=[
            {
                "source_id": document_id,
                "locator": "page=2",
                "span": {"start": 12, "end": 23, "unit": "character"},
            }
        ],
    )
    pending = make_assertion(
        log=log,
        subject_id="",
        subject_ref={
            "reference_id": "urn:reference:ship-name-1",
            "surface_form": "Tàu nghiên cứu",
            "entity_type": "Platform",
        },
        predicate_iri=f"{CORE}name",
        object_kind="literal",
        object_value="Tàu khảo sát",
        literal_language="vi",
        valid_from="2026-09-01T00:00:00Z",
        source_ids=[document_id],
        evidence=[{"source_id": document_id, "locator": "page=3"}],
    )
    sensor = sensor_event(
        sensor_entity_id=SENSOR,
        sensor_id="AIS-001",
        name="AIS receiver",
        platform_entity_id=ENTITY,
        platform_name="Tàu nghiên cứu",
        source_id=document_id,
    )
    observation = observation_event(
        observation_id="urn:fact:observation-1",
        sensor_entity_id=SENSOR,
        subject_entity_id=ENTITY,
        subject_name="Tàu nghiên cứu",
        at_time=AT_TIME,
        position_uri=LOCATION,
        measurements=[{"name": "speed", "value": 12.4, "unit": "kn"}],
        source_ids=[document_id],
        evidence=[
            {
                "source_id": document_id,
                "locator": "frame=7",
                "span": {"start": 7, "end": 8, "unit": "row"},
            }
        ],
    )
    track = track_event(
        track_id="ais-track-1",
        entity_id=ENTITY,
        subject_name="Tàu nghiên cứu",
        observation_ids=[observation.payload["observation_id"]],
        source_ids=[document_id],
    )
    log.extend([sensor, observation, track])
    return log.path, document_id, resolved, pending, observation


def _geometry_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["location_iri", "longitude", "latitude", "source_iri"])
        writer.writerows(rows)
    return path


def test_trig_round_trip_preserves_assertion_and_sosa_contract(tmp_path):
    log_path, document_id, resolved, pending, observation = _write_export_log(tmp_path)
    summary = project_event_log(log_path, profile="interop-v1")
    rendered = summary.dataset.serialize(format="trig")
    reopened = Dataset().parse(data=rendered, format="trig")
    canonical = reopened.default_graph
    interop = reopened.graph(INTEROP_GRAPH_IRI)

    resolved_id = URIRef(resolved.payload["assertion_id"])
    pending_id = URIRef(pending.payload["assertion_id"])
    assert (resolved_id, URIRef(f"{CORE}describes"), URIRef(ENTITY)) in canonical
    assert (resolved_id, URIRef(f"{ASSERTION}predicate"), URIRef(f"{CORE}locatedAt")) in canonical
    assert (resolved_id, URIRef(f"{ASSERTION}hasObject"), URIRef(LOCATION)) in canonical
    assert (
        resolved_id,
        URIRef(f"{CORE}validFrom"),
        Literal("2026-09-01T00:00:00Z", datatype=XSD.dateTime),
    ) in canonical
    assert (
        resolved_id,
        URIRef(f"{CORE}validUntil"),
        Literal("2026-09-02T00:00:00Z", datatype=XSD.dateTime),
    ) in canonical
    assert (resolved_id, URIRef(f"{CORE}hasSource"), URIRef(document_id)) in canonical
    assert (resolved_id, URIRef(f"{CORE}hasSource"), URIRef("urn:source:unregistered")) in canonical
    assert (
        pending_id,
        URIRef(f"{ASSERTION}subjectReference"),
        URIRef("urn:reference:ship-name-1"),
    ) in canonical
    assert (
        URIRef("urn:reference:ship-name-1"),
        URIRef(
            "https://damminhtien.github.io/ontology-research/ontology/middle/identity#referenceText"
        ),
        Literal("Tàu nghiên cứu", datatype=XSD.string),
    ) in canonical
    assert (
        pending_id,
        URIRef(f"{ASSERTION}literalValue"),
        Literal("Tàu khảo sát", lang="vi"),
    ) in canonical
    evidence_nodes = set(canonical.objects(resolved_id, URIRef(f"{ASSERTION}evidence")))
    evidence = next(
        node
        for node in evidence_nodes
        if (node, URIRef(f"{ASSERTION}locator"), Literal("page=2", datatype=XSD.string))
        in canonical
    )
    assert (
        evidence,
        URIRef(f"{ASSERTION}locator"),
        Literal("page=2", datatype=XSD.string),
    ) in canonical
    assert (evidence, URIRef(f"{ASSERTION}spanStart"), Literal(12)) in canonical
    assert (evidence, URIRef(f"{ASSERTION}spanEnd"), Literal(23)) in canonical
    assert (
        evidence,
        URIRef(f"{ASSERTION}spanUnit"),
        Literal("character", datatype=XSD.string),
    ) in canonical

    observation_id = URIRef(observation.payload["observation_id"])
    result_id = URIRef(observation.payload["observation_result_id"])
    assert (result_id, URIRef(f"{TRACKING}resultOf"), observation_id) in canonical
    assert list(
        canonical.triples(
            (None, URIRef(f"{TRACKING}measurementValue"), Literal("12.4", datatype=XSD.decimal))
        )
    )
    observation_evidence = next(canonical.objects(result_id, URIRef(f"{TRACKING}evidence")))
    assert (
        observation_evidence,
        URIRef(f"{TRACKING}evidenceSource"),
        URIRef(document_id),
    ) in canonical
    assert (
        observation_evidence,
        URIRef(f"{TRACKING}locator"),
        Literal("frame=7", datatype=XSD.string),
    ) in canonical
    assert (
        observation_evidence,
        URIRef(f"{TRACKING}spanStart"),
        Literal(7),
    ) in canonical
    assert (observation_id, URIRef(f"{SOSA}madeBySensor"), URIRef(SENSOR)) in interop
    assert (observation_id, URIRef(f"{SOSA}hasFeatureOfInterest"), URIRef(ENTITY)) in interop
    assert (observation_id, URIRef(f"{SOSA}hasResult"), result_id) in interop
    assert not list(interop.objects(observation_id, URIRef(f"{SOSA}resultTime")))
    phenomenon_time = next(interop.objects(observation_id, URIRef(f"{SOSA}phenomenonTime")))
    assert (
        phenomenon_time,
        URIRef("http://www.w3.org/2006/time#inXSDDateTimeStamp"),
        Literal(AT_TIME, datatype=XSD.dateTimeStamp),
    ) in interop
    assert (
        resolved_id,
        URIRef(f"{PROV}wasGeneratedBy"),
        URIRef(f"urn:event:{resolved.event_id}"),
    ) in interop
    assert (
        URIRef(f"urn:event:{resolved.event_id}"),
        URIRef(f"{PROV}used"),
        URIRef(document_id),
    ) in interop
    assert (
        URIRef(f"urn:event:{resolved.event_id}"),
        URIRef(f"{PROV}used"),
        URIRef("urn:source:unregistered"),
    ) not in interop
    track = URIRef(f"{TRACKING}track-ais-track-1")
    assert (track, URIRef(f"{PROV}wasDerivedFrom"), observation_id) in interop
    assert summary.event_count == 6
    assert summary.skipped_event_types == ()

    shapes = Graph().parse(Path(__file__).parents[1] / "shapes" / "interop_shapes.ttl")
    conforms, _, report = validate(
        data_graph=interop,
        shacl_graph=shapes,
        inference="none",
        advanced=True,
    )
    assert conforms, report

    invalid = Graph()
    for triple in interop:
        invalid.add(triple)
    invalid.add((observation_id, URIRef(f"{SOSA}resultTime"), phenomenon_time))
    conforms, _, _ = validate(
        data_graph=invalid,
        shacl_graph=shapes,
        inference="none",
        advanced=True,
    )
    assert not conforms


def test_geometry_uses_crs84_longitude_first_and_omits_missing_rows(tmp_path):
    log_path, document_id, *_ = _write_export_log(tmp_path)
    second_location = "urn:world:location:without-geometry"
    log = EventLog(log_path)
    make_assertion(
        log=log,
        subject_id=ENTITY,
        predicate_iri=f"{CORE}locatedAt",
        object_kind="location",
        object_value=second_location,
        valid_from="2026-09-03T00:00:00Z",
        source_ids=[document_id],
    )
    csv_path = _geometry_csv(
        tmp_path / "geometry.csv",
        [(LOCATION, "144.5", "-37.25", document_id)],
    )
    summary = project_event_log(log_path, profile="interop-v1", geometry_path=csv_path)
    interop = summary.dataset.graph(INTEROP_GRAPH_IRI)
    geometry = next(interop.objects(URIRef(LOCATION), URIRef(f"{GEO}hasGeometry")))
    assert (
        geometry,
        URIRef(f"{GEO}asWKT"),
        Literal(
            "<http://www.opengis.net/def/crs/OGC/1.3/CRS84> POINT(144.5 -37.25)",
            datatype=URIRef(f"{GEO}wktLiteral"),
        ),
    ) in interop
    assert (geometry, DCTERMS.source, URIRef(document_id)) in interop
    assert (geometry, URIRef(f"{PROV}wasDerivedFrom"), URIRef(document_id)) in interop
    assert not list(interop.objects(URIRef(second_location), URIRef(f"{GEO}hasGeometry")))


def test_geometry_accepts_locations_present_as_assertion_objects(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    document = register_document(
        log=log,
        uri="https://data.example/report/location",
        title="Location record",
        source_system="research-feed",
    )
    make_assertion(
        log=log,
        subject_id=ENTITY,
        predicate_iri=f"{CORE}locatedAt",
        object_kind="location",
        object_value=LOCATION,
        valid_from="2026-09-01T00:00:00Z",
        source_ids=[document.payload["document_id"]],
    )
    geometry_path = _geometry_csv(
        tmp_path / "geometry.csv",
        [(LOCATION, "144", "-37", document.payload["document_id"])],
    )
    summary = project_event_log(log.path, profile="interop-v1", geometry_path=geometry_path)
    assert list(
        summary.dataset.graph(INTEROP_GRAPH_IRI).objects(
            URIRef(LOCATION), URIRef(f"{GEO}hasGeometry")
        )
    )


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([(LOCATION, "144", "-37", "")], "source_iri must be non-empty"),
        ([(LOCATION, "181", "-37", "urn:source:test")], "longitude must be within"),
        (
            [
                (LOCATION, "144", "-37", "urn:source:test"),
                (LOCATION, "145", "-38", "urn:source:test"),
            ],
            "duplicate location_iri",
        ),
    ],
)
def test_geometry_rejects_bad_source_coordinates_and_duplicate_locations(tmp_path, rows, message):
    log_path, *_ = _write_export_log(tmp_path)
    geometry_path = _geometry_csv(tmp_path / "geometry.csv", rows)
    with pytest.raises(ValueError, match=message):
        project_event_log(log_path, profile="interop-v1", geometry_path=geometry_path)


def test_geometry_rejects_locations_outside_the_projection(tmp_path):
    log_path, *_ = _write_export_log(tmp_path)
    geometry_path = _geometry_csv(
        tmp_path / "geometry.csv",
        [("urn:world:location:unknown", "1", "2", "urn:source:test")],
    )
    with pytest.raises(ValueError, match="location is not in the RDF projection"):
        project_event_log(log_path, profile="interop-v1", geometry_path=geometry_path)


def test_canonical_profile_has_only_the_repository_default_graph(tmp_path):
    log_path, *_ = _write_export_log(tmp_path)
    summary = project_event_log(log_path, profile="canonical-v1")
    default_identifier = summary.dataset.default_graph.identifier
    assert all(graph.identifier == default_identifier for graph in summary.dataset.contexts())
    with pytest.raises(ValueError, match="only supported by the interop-v1 profile"):
        project_event_log(
            log_path,
            profile="canonical-v1",
            geometry_path=_geometry_csv(tmp_path / "geometry.csv", []),
        )


def test_export_cli_writes_trig_atomically(tmp_path):
    log_path, *_ = _write_export_log(tmp_path)
    output = tmp_path / "out" / "projection.trig"
    command = [
        sys.executable,
        str(Path(__file__).parents[1] / "tools" / "export_rdf.py"),
        "--event-log",
        str(log_path),
        "--profile",
        "interop-v1",
        "--output",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Exported" in result.stdout
    assert Dataset().parse(output.as_posix(), format="trig")


def test_crosswalk_file_is_versioned_and_each_mapping_has_a_condition():
    path = Path(__file__).parents[1] / "registry" / "rdf-crosswalks.json"
    crosswalk = json.loads(path.read_text(encoding="utf-8"))
    assert crosswalk["version"] == 1
    assert set(crosswalk["profiles"]) == {"canonical-v1", "interop-v1"}
    for profile in crosswalk["profiles"].values():
        assert all(mapping.get("condition") for mapping in profile["mappings"])
