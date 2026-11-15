"""Build offline canonical and standards-aligned RDF datasets from the event log."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Container, Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

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


@dataclass(frozen=True)
class ProjectionContext:
    """In-memory references for callers that materialize the full dataset."""

    event_count: int
    last_sequence: int
    registered_documents: frozenset[str]
    registered_observation_ids: frozenset[str]
    projected_locations: frozenset[str]
    skipped_event_types: tuple[str, ...]


class _SqliteMembership(Container[str]):
    """Bounded-memory membership view over one SQLite reference kind."""

    def __init__(self, connection: sqlite3.Connection, kind: str) -> None:
        self._connection = connection
        self._kind = kind

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        return (
            self._connection.execute(
                "SELECT 1 FROM projection_references WHERE kind = ? AND iri = ?",
                (self._kind, value),
            ).fetchone()
            is not None
        )


class ProjectionContextIndex:
    """Disk-backed cross-event lookups for the streaming exporter."""

    def __init__(
        self,
        path: Path,
        connection: sqlite3.Connection,
        source_fingerprint: tuple[tuple[str, int, int], ...],
        event_count: int,
        last_sequence: int,
    ) -> None:
        """Open an indexed reference database and initialize run-local state."""
        self.path = path
        self._connection = connection
        self.source_fingerprint = source_fingerprint
        self.event_count = event_count
        self.last_sequence = last_sequence
        self.registered_documents: Container[str] = _SqliteMembership(connection, "document")
        self.registered_observation_ids: Container[str] = _SqliteMembership(
            connection, "observation"
        )
        self.projected_locations: Container[str] = _SqliteMembership(connection, "location")
        self._connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS seen_geometry (iri TEXT PRIMARY KEY) WITHOUT ROWID"
        )

    @property
    def skipped_event_types(self) -> tuple[str, ...]:
        """Return the small, schema-bounded set of event types without mappers."""
        return tuple(
            row[0]
            for row in self._connection.execute(
                "SELECT event_type FROM skipped_event_types ORDER BY event_type"
            )
        )

    def remember_geometry_location(self, location_iri: str) -> bool:
        """Return whether a geometry location is new in this export."""
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO temp.seen_geometry (iri) VALUES (?)", (location_iri,)
        )
        return cursor.rowcount == 1

    def reset_geometry_locations(self) -> None:
        """Clear duplicate tracking before consuming one geometry CSV."""
        self._connection.execute("DELETE FROM temp.seen_geometry")

    def verify_source(self, event_log_path: Path) -> None:
        """Fail if the append-only source changed during either export scan."""
        if EventLog(event_log_path).fingerprint() != self.source_fingerprint:
            raise RuntimeError("event log changed during RDF export; output was not published")

    def close(self) -> None:
        """Release the SQLite connection and its temporary geometry table."""
        self._connection.close()

    def __enter__(self) -> ProjectionContextIndex:
        """Return this open index for a managed projection scope."""
        return self

    def __exit__(self, *_exc) -> None:
        """Close the index when its projection scope ends."""
        self.close()


_PROJECTED_EVENT_TYPES = frozenset(
    {
        "EntityCreated",
        "ExternalIdBound",
        "DocumentRegistered",
        "AssertionMade",
        "AssertionSuperseded",
        "SensorRegistered",
        "ObservationRecorded",
        "TrackObserved",
    }
)

_CONTEXT_INDEX_SCHEMA_VERSION = "1"
_SQLITE_CACHE_KIB = 8192


def _context_reference_values(event: SemanticEvent) -> Iterator[tuple[str, str]]:
    """Yield the cross-event identifiers needed to map one event."""
    if event.event_type == "DocumentRegistered":
        yield "document", event.payload["document_id"]
    elif event.event_type == "ObservationRecorded":
        yield "observation", event.payload["observation_id"]
        position_iri = event.payload.get("position_uri", event.payload.get("location_uri"))
        if position_iri:
            yield "location", position_iri
    elif event.event_type == "AssertionMade":
        object_value = event.payload.get("object") or {}
        if object_value.get("kind") == "location":
            yield "location", object_value["value"]
    elif event.event_type == "EntityCreated" and event.payload.get("entity_type") == "Location":
        yield "location", event.payload["entity_id"]


def _configure_index_connection(connection: sqlite3.Connection) -> None:
    """Keep SQLite's page cache bounded and disable mmap for predictable RSS."""
    connection.execute(f"PRAGMA cache_size = -{_SQLITE_CACHE_KIB}")
    connection.execute("PRAGMA mmap_size = 0")
    connection.execute("PRAGMA temp_store = FILE")


def _projection_fingerprint_json(
    fingerprint: tuple[tuple[str, int, int], ...],
) -> str:
    return json.dumps(fingerprint, separators=(",", ":"))


def _open_matching_projection_index(
    index_path: Path,
    source_fingerprint: tuple[tuple[str, int, int], ...],
) -> ProjectionContextIndex | None:
    """Open a compatible cached index, or return ``None`` when it is stale."""
    if not index_path.is_file():
        return None
    connection = sqlite3.connect(index_path)
    try:
        _configure_index_connection(connection)
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if metadata.get("schema_version") != _CONTEXT_INDEX_SCHEMA_VERSION or metadata.get(
            "source_fingerprint"
        ) != _projection_fingerprint_json(source_fingerprint):
            connection.close()
            return None
        # Reject incomplete or incompatible indexes before exposing their
        # membership views to the exporter. A failed rebuild can then leave the
        # previous index untouched and the next invocation can retry safely.
        connection.execute("SELECT 1 FROM projection_references LIMIT 0")
        connection.execute("SELECT 1 FROM skipped_event_types LIMIT 0")
        return ProjectionContextIndex(
            index_path,
            connection,
            source_fingerprint,
            int(metadata["event_count"]),
            int(metadata["last_sequence"]),
        )
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        connection.close()
        return None


def open_projection_context_index(
    event_log_path: Path,
    index_path: Path,
) -> ProjectionContextIndex:
    """Open or atomically rebuild a disk-backed index for one event-log state.

    The index is keyed by the event-log segment fingerprint. A stale or corrupt
    index is rebuilt beside the previous one and replaced only when the source
    stayed unchanged through the scan.
    """
    log = EventLog(event_log_path)
    source_fingerprint = log.fingerprint()
    source_paths = {Path(path).resolve() for path, _size, _mtime in source_fingerprint}
    if index_path.resolve() in source_paths:
        raise ValueError("context index path must not be an event-log segment")
    if index_path.exists() and not index_path.is_file():
        raise ValueError(f"context index path is not a file: {index_path}")
    index_path.parent.mkdir(parents=True, exist_ok=True)

    existing = _open_matching_projection_index(index_path, source_fingerprint)
    if existing is not None:
        existing.verify_source(event_log_path)
        return existing

    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=index_path.parent,
        prefix=f".{index_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        candidate_path = Path(temporary.name)

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(candidate_path)
        _configure_index_connection(connection)
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute(
            "CREATE TABLE projection_references ("
            "kind TEXT NOT NULL, iri TEXT NOT NULL, PRIMARY KEY (kind, iri)) WITHOUT ROWID"
        )
        connection.execute(
            "CREATE TABLE skipped_event_types (event_type TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

        event_count = 0
        last_sequence = 0
        with connection:
            for event in log.iter_all():
                event_count += 1
                last_sequence = event.sequence or event_count
                if event.event_type not in _PROJECTED_EVENT_TYPES:
                    connection.execute(
                        "INSERT OR IGNORE INTO skipped_event_types (event_type) VALUES (?)",
                        (event.event_type,),
                    )
                connection.executemany(
                    "INSERT OR IGNORE INTO projection_references (kind, iri) VALUES (?, ?)",
                    _context_reference_values(event),
                )
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                (
                    ("schema_version", _CONTEXT_INDEX_SCHEMA_VERSION),
                    ("source_fingerprint", _projection_fingerprint_json(source_fingerprint)),
                    ("event_count", str(event_count)),
                    ("last_sequence", str(last_sequence)),
                ),
            )

        connection.close()
        connection = None
        if log.fingerprint() != source_fingerprint:
            raise RuntimeError("event log changed while its projection index was built")
        descriptor = os.open(candidate_path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        candidate_path.replace(index_path)
        try:
            directory_descriptor = os.open(index_path.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            except OSError:
                pass
            finally:
                os.close(directory_descriptor)
    except Exception:
        if connection is not None:
            connection.close()
        candidate_path.unlink(missing_ok=True)
        Path(f"{candidate_path}-journal").unlink(missing_ok=True)
        raise

    return _open_matching_projection_index(index_path, source_fingerprint) or _raise_index_error(
        index_path
    )


def _raise_index_error(index_path: Path) -> ProjectionContextIndex:
    raise RuntimeError(f"built projection context index could not be reopened: {index_path}")


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
    registered_documents: Container[str],
    registered_observations: Container[str],
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


def _iter_geometry_triples(
    geometry_path: Path,
    context: ProjectionContext | ProjectionContextIndex,
) -> Iterator[tuple[URIRef, URIRef, URIRef | Literal]]:
    """Yield validated location geometry triples without collecting CSV rows."""
    seen: set[str] = set()
    if isinstance(context, ProjectionContextIndex):
        context.reset_geometry_locations()
    expected_fields = ["location_iri", "longitude", "latitude", "source_iri"]
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
            if isinstance(context, ProjectionContextIndex):
                is_new = context.remember_geometry_location(location_iri)
            else:
                is_new = location_iri not in seen
                seen.add(location_iri)
            if not is_new:
                raise ValueError(f"geometry CSV line {line_number}: duplicate location_iri")
            location = URIRef(location_iri)
            if location_iri not in context.projected_locations:
                raise ValueError(
                    f"geometry CSV line {line_number}: location is not in the RDF projection"
                )
            longitude = _decimal_coordinate(row.get("longitude") or "", "longitude", -180, 180)
            latitude = _decimal_coordinate(row.get("latitude") or "", "latitude", -90, 90)
            digest = hashlib.sha256(location_iri.encode("utf-8")).hexdigest()
            geometry = URIRef(f"urn:geometry:{digest}")
            yield location, URIRef(f"{GEO}hasGeometry"), geometry
            yield geometry, RDF.type, URIRef(f"{GEO}Geometry")
            yield (
                geometry,
                URIRef(f"{GEO}asWKT"),
                Literal(
                    f"<{CRS84}> POINT({longitude} {latitude})",
                    datatype=URIRef(f"{GEO}wktLiteral"),
                ),
            )
            yield geometry, DCTERMS.source, URIRef(source_iri)
            if source_iri in context.registered_documents:
                yield geometry, URIRef(f"{PROV}wasDerivedFrom"), URIRef(source_iri)


def validate_export_options(profile: str, geometry_path: Path | None) -> None:
    """Validate the requested profile before opening or scanning the event log."""
    if profile not in {"canonical-v1", "interop-v1"}:
        raise ValueError(f"unknown RDF export profile {profile!r}")
    if geometry_path is not None and profile != "interop-v1":
        raise ValueError("geometry CSV is only supported by the interop-v1 profile")


def collect_projection_context(event_log_path: Path) -> ProjectionContext:
    """Collect cross-event references for the full-materialization API."""
    log = EventLog(event_log_path)
    registered_documents: set[str] = set()
    registered_observation_ids: set[str] = set()
    projected_locations: set[str] = set()
    skipped_event_types: set[str] = set()
    event_count = 0
    last_sequence = 0
    for event in log.iter_all():
        event_count += 1
        last_sequence = event.sequence or event_count
        if event.event_type not in _PROJECTED_EVENT_TYPES:
            skipped_event_types.add(event.event_type)
        for kind, identifier in _context_reference_values(event):
            if kind == "document":
                registered_documents.add(identifier)
            elif kind == "observation":
                registered_observation_ids.add(identifier)
            elif kind == "location":
                projected_locations.add(identifier)

    return ProjectionContext(
        event_count=event_count,
        last_sequence=last_sequence,
        registered_documents=frozenset(registered_documents),
        registered_observation_ids=frozenset(registered_observation_ids),
        projected_locations=frozenset(projected_locations),
        skipped_event_types=tuple(sorted(skipped_event_types)),
    )


def iter_projected_quads(
    event_log_path: Path,
    *,
    profile: str = "canonical-v1",
    geometry_path: Path | None = None,
    context: ProjectionContext | ProjectionContextIndex | None = None,
) -> Iterator[tuple[object, object, object, URIRef | None]]:
    """Yield quads, using a temporary SQLite index unless context is provided."""
    validate_export_options(profile, geometry_path)
    if context is None:
        with (
            tempfile.TemporaryDirectory(prefix="ontology-rdf-context-") as directory,
            open_projection_context_index(
                event_log_path, Path(directory) / "context.sqlite"
            ) as index,
        ):
            yield from iter_projected_quads(
                event_log_path,
                profile=profile,
                geometry_path=geometry_path,
                context=index,
            )
            index.verify_source(event_log_path)
        return

    projection_context = context
    log = EventLog(event_log_path)

    def yield_graph(graph: Graph, graph_iri: URIRef | None) -> Iterator[tuple]:
        for subject, predicate, object_ in graph:
            yield subject, predicate, object_, graph_iri

    for event in log.iter_all():
        # RDFLib's Memory store retains empty subject/predicate index buckets
        # when triples are removed. Discard each event-local store instead of
        # clearing and reusing it so those buckets cannot accumulate with log
        # length.
        canonical = Graph()
        interop = Graph() if profile == "interop-v1" else None
        entity: URIRef | None = None
        if event.event_type in {
            "EntityCreated",
            "ExternalIdBound",
            "SourceRecordMatched",
        }:
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

        yield from yield_graph(canonical, None)
        if interop is not None:
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
                    projection_context.registered_documents,
                    projection_context.registered_observation_ids,
                )
            yield from yield_graph(interop, INTEROP_GRAPH_IRI)

    if profile == "interop-v1" and geometry_path is not None:
        for subject, predicate, object_ in _iter_geometry_triples(
            geometry_path, projection_context
        ):
            yield subject, predicate, object_, INTEROP_GRAPH_IRI


def write_trig_stream(
    quads: Iterator[tuple[object, object, object, URIRef | None]],
    output: TextIO,
) -> int:
    """Write TriG graph blocks incrementally and return emitted triple lines.

    Repeated blocks for one graph label are merged by RDF dataset parsers. This
    lets event-local canonical and interop quads be serialized without holding
    either graph or the complete dataset in memory.
    """
    no_graph = object()
    current_graph: object = no_graph
    emitted = 0
    for subject, predicate, object_, graph in quads:
        if graph != current_graph:
            if current_graph is not no_graph:
                output.write("}\n")
            graph_label = "" if graph is None else graph.n3()
            output.write(f"{graph_label} {{\n")
            current_graph = graph
        output.write(f"  {subject.n3()} {predicate.n3()} {object_.n3()} .\n")
        emitted += 1

    if current_graph is no_graph:
        output.write("{}\n")
    else:
        output.write("}\n")
    return emitted


def project_event_log(
    event_log_path: Path,
    *,
    profile: str = "canonical-v1",
    geometry_path: Path | None = None,
) -> ExportSummary:
    """Project an event log to TriG-ready canonical and optional interop graphs."""
    validate_export_options(profile, geometry_path)
    context = collect_projection_context(event_log_path)
    dataset = Dataset()
    for subject, predicate, object_, graph_iri in iter_projected_quads(
        event_log_path,
        profile=profile,
        geometry_path=geometry_path,
        context=context,
    ):
        graph = dataset.default_graph if graph_iri is None else dataset.graph(graph_iri)
        graph.add((subject, predicate, object_))
    return ExportSummary(dataset, context.event_count, context.skipped_event_types)
