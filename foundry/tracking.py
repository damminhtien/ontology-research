"""Sensor/tracking ingestion mapping (Phase 4 — the tracking vertical).

Turns AIS-style feed records into canonical events through the same gates as
everything else: identity resolution (MMSI and sensor serials are trusted
external ids, ADR-0006) + the domain SHACL contracts
(``tracking:TrackShape``, ``sensor:SensorShape``, ``core:ObservationShape``)
before anything reaches the log.

Mapping (ADR-0005 — a Track is a derived InformationObject, never an Event):

    SensorRegistered    → sensor artifact, mounted on a platform entity
    ObservationRecorded → one detection: sensor, subject entity, time anchor
    TrackObserved       → the derived hypothesis: trackOf + derivedFrom ids

The feed is pluggable: :func:`parse_feed` consumes JSONL lines shaped like
real AIS exports (mmsi/name/sensor/position/time), and every record maps to
one of the three events.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from foundry.events import (
    EVENT_TYPE_OBSERVATION_RECORDED,
    EVENT_TYPE_SENSOR_REGISTERED,
    EVENT_TYPE_TRACK_OBSERVED,
    SemanticEvent,
    make_event,
)
from foundry.namespaces import new_fact_iri

TRACKING_NS = "https://damminhtien.github.io/ontology-research/ontology/domain/tracking#"
SENSOR_NS = "https://damminhtien.github.io/ontology-research/ontology/domain/sensor#"
CORE_NS = "https://damminhtien.github.io/ontology-research/ontology/core#"


@dataclass(frozen=True)
class FeedRecord:
    """One normalized AIS-style feed row."""

    mmsi: str
    name: str
    sensor_id: str
    track_id: str
    location_uri: str
    at_time: str


def parse_feed(lines: list[str] | tuple[str, ...]) -> list[FeedRecord]:
    """Parse JSONL feed lines into normalized records.

    Raises:
        ValueError: On a non-JSON line or a record missing required fields.
    """
    records: list[FeedRecord] = []
    for line_no, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"feed line {line_no} is not JSON: {exc}") from exc
        try:
            records.append(
                FeedRecord(
                    mmsi=str(raw["mmsi"]),
                    name=str(raw["name"]).strip(),
                    sensor_id=str(raw["sensor_id"]),
                    track_id=str(raw["track_id"]),
                    location_uri=str(raw["location_uri"]),
                    at_time=str(raw["at_time"]),
                )
            )
        except KeyError as exc:
            raise ValueError(f"feed line {line_no} missing field: {exc}") from exc
        if not records[-1].name:
            raise ValueError(f"feed line {line_no}: empty platform name")
    return records


def sensor_event(
    *,
    sensor_entity_id: str,
    sensor_id: str,
    name: str,
    platform_entity_id: str,
    platform_name: str,
    source_id: str,
) -> SemanticEvent:
    """Build the ``SensorRegistered`` event (mounted on a platform entity)."""
    return make_event(
        EVENT_TYPE_SENSOR_REGISTERED,
        {
            "sensor_entity_id": sensor_entity_id,
            "sensor_id": sensor_id,
            "name": name,
            "platform_entity_id": platform_entity_id,
            "platform_name": platform_name,
            "source_id": source_id,
            "confidence": 1.0,
        },
    )


def observation_event(
    *,
    observation_id: str,
    sensor_entity_id: str,
    subject_entity_id: str,
    subject_name: str,
    at_time: str,
    location_uri: str | None,
    source_ids: list[str],
    confidence: float | None = None,
) -> SemanticEvent:
    """Build the ``ObservationRecorded`` event (one detection)."""
    return make_event(
        EVENT_TYPE_OBSERVATION_RECORDED,
        {
            "observation_id": observation_id,
            "sensor_entity_id": sensor_entity_id,
            "subject_entity_id": subject_entity_id,
            "subject_name": subject_name,
            "at_time": at_time,
            "location_uri": location_uri,
            "source_ids": list(source_ids),
            "confidence": confidence,
        },
    )


def track_event(
    *,
    track_id: str,
    entity_id: str,
    subject_name: str,
    observation_ids: list[str],
    source_ids: list[str],
) -> SemanticEvent:
    """Build the ``TrackObserved`` event (the derived hypothesis)."""
    return make_event(
        EVENT_TYPE_TRACK_OBSERVED,
        {
            "track_id": track_id,
            "entity_id": entity_id,
            "subject_name": subject_name,
            "observation_ids": list(observation_ids),
            "source_ids": list(source_ids),
        },
    )


def new_observation_id() -> str:
    """Mint a fresh observation fact IRI (``urn:fact``, per ADR-0002)."""
    return new_fact_iri()


def _rdf_terms():
    from rdflib import RDF, Literal, URIRef
    from rdflib.namespace import XSD

    return RDF, URIRef, Literal, XSD


def sensor_to_rdf(graph, event: SemanticEvent) -> None:
    """Map ``SensorRegistered`` to the ``sensor:SensorShape`` contract."""
    RDF, URIRef, Literal, XSD = _rdf_terms()
    payload = event.payload
    node = URIRef(payload["sensor_entity_id"])
    graph.add((node, RDF.type, URIRef(SENSOR_NS + "Sensor")))
    graph.add((node, URIRef(CORE_NS + "name"), Literal(payload["name"], datatype=XSD.string)))
    platform = URIRef(payload["platform_entity_id"])
    graph.add((node, URIRef(SENSOR_NS + "mountedOn"), platform))
    graph.add((platform, RDF.type, URIRef(CORE_NS + "Platform")))
    graph.add(
        (platform, URIRef(CORE_NS + "name"), Literal(payload["platform_name"], datatype=XSD.string))
    )


def observation_to_rdf(graph, event: SemanticEvent) -> None:
    """Map ``ObservationRecorded`` to ``core:ObservationShape`` + detectedBy."""
    RDF, URIRef, Literal, XSD = _rdf_terms()
    payload = event.payload
    node = URIRef(payload["observation_id"])
    graph.add((node, RDF.type, URIRef(CORE_NS + "Observation")))
    graph.add(
        (node, URIRef(CORE_NS + "atTime"), Literal(payload["at_time"], datatype=XSD.dateTime))
    )
    for source_id in payload.get("source_ids") or []:
        source_node = URIRef(source_id)
        graph.add((node, URIRef(CORE_NS + "hasSource"), source_node))
        graph.add((source_node, RDF.type, URIRef(CORE_NS + "Source")))
        graph.add((source_node, URIRef(CORE_NS + "name"), Literal(source_id, datatype=XSD.string)))
    subject = URIRef(payload["subject_entity_id"])
    graph.add((node, URIRef(CORE_NS + "observes"), subject))
    graph.add((subject, RDF.type, URIRef(CORE_NS + "Platform")))
    graph.add(
        (subject, URIRef(CORE_NS + "name"), Literal(payload["subject_name"], datatype=XSD.string))
    )
    sensor = URIRef(payload["sensor_entity_id"])
    graph.add((node, URIRef(SENSOR_NS + "detectedBy"), sensor))
    graph.add((sensor, RDF.type, URIRef(SENSOR_NS + "Sensor")))
    confidence = payload.get("confidence")
    if confidence is not None:
        graph.add(
            (
                node,
                URIRef(CORE_NS + "hasConfidence"),
                Literal(str(confidence), datatype=XSD.decimal),
            )
        )


def track_to_rdf(graph, event: SemanticEvent) -> None:
    """Map ``TrackObserved`` to the ``tracking:TrackShape`` contract."""
    RDF, URIRef, Literal, XSD = _rdf_terms()
    payload = event.payload
    node = URIRef(TRACKING_NS + "track-" + payload["track_id"])
    graph.add((node, RDF.type, URIRef(TRACKING_NS + "Track")))
    graph.add(
        (
            node,
            URIRef(TRACKING_NS + "hasTrackId"),
            Literal(payload["track_id"], datatype=XSD.string),
        )
    )
    entity = URIRef(payload["entity_id"])
    graph.add((node, URIRef(TRACKING_NS + "trackOf"), entity))
    graph.add((entity, RDF.type, URIRef(CORE_NS + "Platform")))
    graph.add(
        (entity, URIRef(CORE_NS + "name"), Literal(payload["subject_name"], datatype=XSD.string))
    )
    for observation_id in payload.get("observation_ids") or []:
        obs = URIRef(observation_id)
        graph.add((node, URIRef(TRACKING_NS + "derivedFrom"), obs))
        graph.add((obs, RDF.type, URIRef(CORE_NS + "Observation")))
