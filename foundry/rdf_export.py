"""Build offline canonical and standards-aligned RDF datasets from the event log."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from rdflib import RDF, Dataset, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, XSD

from foundry.assertions import (
    assertion_superseded_to_rdf,
    assertion_to_rdf,
    document_to_rdf,
)
from foundry.events import EventLog, SemanticEvent
from foundry.identity_rdf import entity_identity_to_rdf
from foundry.namespaces import CORE_ONTOLOGY_NS, require_absolute_iri
from foundry.tracking import (
    observation_to_rdf,
    sensor_to_rdf,
    track_to_rdf,
)

PROV = URIRef("http://www.w3.org/ns/prov#")
SOSA = URIRef("http://www.w3.org/ns/sosa/")
TIME = URIRef("http://www.w3.org/2006/time#")
GEO = URIRef("http://www.opengis.net/ont/geosparql#")
CRS84 = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
INTEROP_GRAPH_IRI = URIRef("urn:ontology-research:graph:interop-v1")
CORE = CORE_ONTOLOGY_NS
TRACKING = "https://damminhtien.github.io/ontology-research/ontology/domain/tracking#"


@dataclass(frozen=True)
class ExportSummary:
    """Metadata about one completed projection."""

    dataset: Dataset
    event_count: int
    skipped_event_types: tuple[str, ...]


def _activity(event: SemanticEvent) -> URIRef:
    """Return the stable PROV activity IRI for a persisted event."""
    return URIRef(f"urn:event:{event.event_id}")


def _record_generation(graph: Graph, entity: URIRef, event: SemanticEvent) -> None:
    """Connect one projected entity to the event that recorded it."""
    activity = _activity(event)
    graph.add((activity, RDF.type, URIRef(f"{PROV}Activity")))
    graph.add((entity, RDF.type, URIRef(f"{PROV}Entity")))
    graph.add((entity, URIRef(f"{PROV}wasGeneratedBy"), activity))
    graph.add((activity, URIRef(f"{PROV}generated"), entity))


def _add_provenance(
    graph: Graph,
    event: SemanticEvent,
    entity: URIRef,
    registered_documents: set[str],
    registered_observations: set[str],
) -> None:
    """Add recorded generation and source use without inferring agents."""
    _record_generation(graph, entity, event)
    activity = _activity(event)
    if event.event_type == "AssertionMade":
        for source_id in event.payload.get("source_ids") or ():
            if source_id in registered_documents:
                graph.add((activity, URIRef(f"{PROV}used"), URIRef(source_id)))
    elif event.event_type == "TrackObserved":
        for observation_id in event.payload.get("observation_ids") or ():
            if observation_id in registered_observations:
                graph.add(
                    (
                        entity,
                        URIRef(f"{PROV}wasDerivedFrom"),
                        URIRef(observation_id),
                    )
                )


def _add_sosa_observation(graph: Graph, event: SemanticEvent) -> None:
    """Expose an existing observation, sensor, feature and result through SOSA."""
    payload = event.payload
    observation = URIRef(payload["observation_id"])
    sensor = URIRef(payload["sensor_entity_id"])
    feature = URIRef(payload["subject_entity_id"])
    result = URIRef(payload["observation_result_id"])
    instant = URIRef(f"{observation}#phenomenon-time")
    graph.add((observation, RDF.type, URIRef(f"{SOSA}Observation")))
    graph.add((sensor, RDF.type, URIRef(f"{SOSA}Sensor")))
    graph.add((observation, URIRef(f"{SOSA}madeBySensor"), sensor))
    graph.add((observation, URIRef(f"{SOSA}hasFeatureOfInterest"), feature))
    graph.add((observation, URIRef(f"{SOSA}hasResult"), result))
    graph.add((observation, URIRef(f"{SOSA}phenomenonTime"), instant))
    graph.add((instant, RDF.type, URIRef(f"{TIME}Instant")))
    graph.add(
        (
            instant,
            URIRef(f"{TIME}inXSDDateTimeStamp"),
            Literal(payload["at_time"], datatype=XSD.dateTimeStamp),
        )
    )


def _decimal_coordinate(value: str, field: str, minimum: int, maximum: int) -> str:
    """Parse a finite coordinate, enforce its axis range, and format WKT text."""
    try:
        coordinate = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal coordinate") from exc
    if not coordinate.is_finite() or not Decimal(minimum) <= coordinate <= Decimal(maximum):
        raise ValueError(f"{field} must be within [{minimum}, {maximum}]")
    if coordinate == 0:
        return "0"
    return format(coordinate.normalize(), "f")


def _add_geometries(
    graph: Graph,
    geometry_path: Path,
    *,
    canonical_graph: Graph,
    projected_locations: set[str],
    registered_documents: set[str],
) -> None:
    """Add validated CSV point geometries for locations in the projection."""
    expected_fields = ["location_iri", "longitude", "latitude", "source_iri"]
    seen: set[str] = set()
    location_type = URIRef(f"{CORE}Location")
    with geometry_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != expected_fields:
            raise ValueError(f"geometry CSV columns must be exactly {expected_fields}")
        for line_number, row in enumerate(reader, start=2):
            location_text = (row.get("location_iri") or "").strip()
            source_text = (row.get("source_iri") or "").strip()
            try:
                location_iri = require_absolute_iri(location_text, "location_iri")
                source_iri = require_absolute_iri(source_text, "source_iri")
            except ValueError as exc:
                raise ValueError(f"geometry CSV line {line_number}: {exc}") from exc
            if location_iri in seen:
                raise ValueError(f"geometry CSV line {line_number}: duplicate location_iri")
            seen.add(location_iri)
            location = URIRef(location_iri)
            if (
                location,
                RDF.type,
                location_type,
            ) not in canonical_graph and location_iri not in projected_locations:
                raise ValueError(
                    f"geometry CSV line {line_number}: location is not in the RDF projection"
                )
            longitude = _decimal_coordinate(row.get("longitude") or "", "longitude", -180, 180)
            latitude = _decimal_coordinate(row.get("latitude") or "", "latitude", -90, 90)
            digest = hashlib.sha256(location_iri.encode("utf-8")).hexdigest()
            geometry = URIRef(f"urn:geometry:{digest}")
            graph.add((location, URIRef(f"{GEO}hasGeometry"), geometry))
            graph.add((geometry, RDF.type, URIRef(f"{GEO}Geometry")))
            graph.add(
                (
                    geometry,
                    URIRef(f"{GEO}asWKT"),
                    Literal(
                        f"<{CRS84}> POINT({longitude} {latitude})",
                        datatype=URIRef(f"{GEO}wktLiteral"),
                    ),
                )
            )
            graph.add((geometry, DCTERMS.source, URIRef(source_iri)))
            if source_iri in registered_documents:
                graph.add((geometry, URIRef(f"{PROV}wasDerivedFrom"), URIRef(source_iri)))


def project_event_log(
    event_log_path: Path,
    *,
    profile: str = "canonical-v1",
    geometry_path: Path | None = None,
) -> ExportSummary:
    """Project an event log to TriG-ready canonical and optional interop graphs."""
    if profile not in {"canonical-v1", "interop-v1"}:
        raise ValueError(f"unknown RDF export profile {profile!r}")
    if geometry_path is not None and profile != "interop-v1":
        raise ValueError("geometry CSV is only supported by the interop-v1 profile")
    log = EventLog(event_log_path)
    registered_documents: set[str] = set()
    registered_observation_ids: set[str] = set()
    projected_locations: set[str] = set()
    event_count = 0
    for event in log.iter_all():
        event_count += 1
        if event.event_type == "DocumentRegistered":
            registered_documents.add(event.payload["document_id"])
        elif event.event_type == "ObservationRecorded":
            registered_observation_ids.add(event.payload["observation_id"])
            position_iri = event.payload.get("position_uri", event.payload.get("location_uri"))
            if position_iri:
                projected_locations.add(position_iri)
        elif event.event_type == "AssertionMade":
            object_value = event.payload.get("object") or {}
            if object_value.get("kind") == "location":
                projected_locations.add(object_value["value"])
        elif event.event_type == "EntityCreated" and event.payload.get("entity_type") == "Location":
            projected_locations.add(event.payload["entity_id"])

    dataset = Dataset()
    canonical = dataset.default_graph
    interop = dataset.graph(INTEROP_GRAPH_IRI) if profile == "interop-v1" else None
    skipped_event_types: set[str] = set()
    for event in log.iter_all():
        entity: URIRef | None = None
        if event.event_type == "EntityCreated" or event.event_type == "ExternalIdBound":
            entity_identity_to_rdf(canonical, event)
        elif event.event_type == "DocumentRegistered":
            entity = document_to_rdf(canonical, event)
        elif event.event_type == "AssertionMade":
            entity = assertion_to_rdf(canonical, event)
        elif event.event_type == "AssertionSuperseded":
            assertion_superseded_to_rdf(canonical, event)
        elif event.event_type == "SensorRegistered":
            sensor_to_rdf(canonical, event)
        elif event.event_type == "ObservationRecorded":
            observation_to_rdf(canonical, event)
        elif event.event_type == "TrackObserved":
            track_to_rdf(canonical, event)
            entity = URIRef(f"{TRACKING}track-{event.payload['track_id']}")
        else:
            skipped_event_types.add(event.event_type)

        if interop is None:
            continue
        if event.event_type == "ObservationRecorded":
            _add_sosa_observation(interop, event)
        if entity is not None and event.event_type in {
            "DocumentRegistered",
            "AssertionMade",
            "TrackObserved",
        }:
            _add_provenance(
                interop,
                event,
                entity,
                registered_documents,
                registered_observation_ids,
            )

    if interop is not None and geometry_path is not None:
        _add_geometries(
            interop,
            geometry_path,
            canonical_graph=canonical,
            projected_locations=projected_locations,
            registered_documents=registered_documents,
        )
    return ExportSummary(dataset, event_count, tuple(sorted(skipped_event_types)))
