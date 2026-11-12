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
from collections.abc import Mapping
from dataclasses import dataclass

from foundry.events import (
    EVENT_TYPE_OBSERVATION_RECORDED,
    EVENT_TYPE_SENSOR_REGISTERED,
    EVENT_TYPE_TRACK_OBSERVED,
    SemanticEvent,
    make_event,
)
from foundry.namespaces import new_fact_iri, require_absolute_iri
from foundry.provenance import normalize_evidence

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
    position_uri: str | None
    at_time: str
    measurements: tuple[dict, ...] = ()

    @property
    def location_uri(self) -> str | None:
        """Legacy field name retained for callers reading older feed records."""
        return self.position_uri


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
                    position_uri=(
                        str(raw.get("position_uri", raw.get("location_uri")))
                        if raw.get("position_uri", raw.get("location_uri")) is not None
                        else None
                    ),
                    at_time=str(raw["at_time"]),
                    measurements=tuple(_normalize_measurements(raw.get("measurements") or ())),
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
    position_uri: str | None = None,
    location_uri: str | None = None,
    measurements: list[Mapping[str, object]] | None = None,
    source_ids: list[str],
    evidence: list[Mapping[str, object]] | None = None,
    confidence: float | None = None,
) -> SemanticEvent:
    """Build an observation event with a separate, typed result payload."""
    if position_uri is not None and location_uri is not None and position_uri != location_uri:
        raise ValueError("position_uri and legacy location_uri must match when both are supplied")
    position = position_uri if position_uri is not None else location_uri
    if position is not None:
        require_absolute_iri(position, "position_uri")
    normalized_measurements = _normalize_measurements(measurements or ())
    sources, evidence_refs = normalize_evidence(source_ids, evidence)
    result_id = f"urn:observation-result:{observation_id.removeprefix('urn:fact:')}"
    return make_event(
        EVENT_TYPE_OBSERVATION_RECORDED,
        {
            "observation_id": observation_id,
            "sensor_entity_id": sensor_entity_id,
            "subject_entity_id": subject_entity_id,
            "subject_name": subject_name,
            "at_time": at_time,
            "observation_result_id": result_id,
            "position_uri": position,
            "measurements": normalized_measurements,
            "source_ids": list(sources),
            "evidence": list(evidence_refs),
            "confidence": confidence,
        },
    )


def _normalize_measurements(values: object) -> list[dict[str, object]]:
    """Validate JSON measurement fields and keep their datatype and unit."""
    if not isinstance(values, (list, tuple)):
        raise ValueError("measurements must be a list")
    normalized: list[dict[str, object]] = []
    for item in values:
        if not isinstance(item, Mapping):
            raise ValueError("each measurement must be an object")
        name = str(item.get("name", "")).strip()
        value = item.get("value")
        if not name or value is None or isinstance(value, (dict, list, tuple)):
            raise ValueError("measurement requires a name and scalar value")
        datatype = item.get("datatype_iri")
        if datatype is None:
            if isinstance(value, bool):
                datatype = "http://www.w3.org/2001/XMLSchema#boolean"
            elif isinstance(value, int):
                datatype = "http://www.w3.org/2001/XMLSchema#integer"
            elif isinstance(value, float):
                datatype = "http://www.w3.org/2001/XMLSchema#decimal"
            else:
                datatype = "http://www.w3.org/2001/XMLSchema#string"
        datatype_iri = require_absolute_iri(str(datatype), "measurement.datatype_iri")
        unit = item.get("unit")
        if unit is not None and (not isinstance(unit, str) or not unit.strip()):
            raise ValueError("measurement unit must be a non-empty string when provided")
        normalized.append(
            {
                "name": name,
                "value": value,
                "datatype_iri": datatype_iri,
                "unit": unit.strip() if isinstance(unit, str) else None,
            }
        )
    return normalized


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
    from foundry.identity_rdf import identifier_to_rdf
    from foundry.provenance import add_sources_to_rdf

    identifier_to_rdf(
        graph,
        entity_id=payload["sensor_entity_id"],
        scheme="sensor-registry",
        value=payload["sensor_id"],
    )
    add_sources_to_rdf(graph, node, [payload["source_id"]])


def observation_to_rdf(graph, event: SemanticEvent) -> None:
    """Map the observing event and its distinct information result to RDF."""
    from rdflib import BNode

    RDF, URIRef, Literal, XSD = _rdf_terms()
    payload = event.payload
    node = URIRef(payload["observation_id"])
    graph.add((node, RDF.type, URIRef(CORE_NS + "Observation")))
    graph.add(
        (node, URIRef(CORE_NS + "atTime"), Literal(payload["at_time"], datatype=XSD.dateTime))
    )
    from foundry.provenance import add_sources_to_rdf

    add_sources_to_rdf(graph, node, payload.get("source_ids") or ())
    subject = URIRef(payload["subject_entity_id"])
    graph.add((node, URIRef(CORE_NS + "observes"), subject))
    graph.add((subject, RDF.type, URIRef(CORE_NS + "Platform")))
    graph.add(
        (subject, URIRef(CORE_NS + "name"), Literal(payload["subject_name"], datatype=XSD.string))
    )
    sensor = URIRef(payload["sensor_entity_id"])
    graph.add((node, URIRef(SENSOR_NS + "detectedBy"), sensor))
    graph.add((sensor, RDF.type, URIRef(SENSOR_NS + "Sensor")))
    result = URIRef(payload["observation_result_id"])
    graph.add((result, RDF.type, URIRef(TRACKING_NS + "ObservationResult")))
    graph.add((result, URIRef(TRACKING_NS + "resultOf"), node))
    evidence_ns = TRACKING_NS
    for item in payload.get("evidence") or ():
        evidence_node = BNode()
        source_id = item["source_id"]
        graph.add((result, URIRef(evidence_ns + "evidence"), evidence_node))
        graph.add((evidence_node, URIRef(evidence_ns + "evidenceSource"), URIRef(source_id)))
        if item.get("locator") is not None:
            graph.add(
                (
                    evidence_node,
                    URIRef(evidence_ns + "locator"),
                    Literal(item["locator"], datatype=XSD.string),
                )
            )
        span = item.get("span")
        if isinstance(span, dict):
            graph.add((evidence_node, URIRef(evidence_ns + "spanStart"), Literal(span["start"])))
            graph.add((evidence_node, URIRef(evidence_ns + "spanEnd"), Literal(span["end"])))
            graph.add(
                (
                    evidence_node,
                    URIRef(evidence_ns + "spanUnit"),
                    Literal(span["unit"], datatype=XSD.string),
                )
            )
    position_uri = payload.get("position_uri", payload.get("location_uri"))
    if position_uri:
        position = URIRef(position_uri)
        graph.add((result, URIRef(TRACKING_NS + "position"), position))
        graph.add((position, RDF.type, URIRef(CORE_NS + "Location")))
        graph.add((position, URIRef(CORE_NS + "name"), Literal(position_uri, datatype=XSD.string)))
    for measurement in payload.get("measurements") or ():
        measurement_node = BNode()
        graph.add((result, URIRef(TRACKING_NS + "measurement"), measurement_node))
        graph.add(
            (
                measurement_node,
                URIRef(TRACKING_NS + "measurementName"),
                Literal(measurement["name"], datatype=XSD.string),
            )
        )
        graph.add(
            (
                measurement_node,
                URIRef(TRACKING_NS + "measurementValue"),
                Literal(str(measurement["value"]), datatype=URIRef(measurement["datatype_iri"])),
            )
        )
        if measurement.get("unit") is not None:
            graph.add(
                (
                    measurement_node,
                    URIRef(TRACKING_NS + "measurementUnit"),
                    Literal(measurement["unit"], datatype=XSD.string),
                )
            )
    confidence = payload.get("confidence")
    if confidence is not None:
        graph.add(
            (
                result,
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
    from foundry.provenance import add_sources_to_rdf

    add_sources_to_rdf(graph, node, payload.get("source_ids") or ())
