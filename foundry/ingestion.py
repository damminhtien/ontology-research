"""Validated canonical ingestion pipeline (Phase 2 skeleton).

Pipeline stages, in order:

    record -> schema validation -> identity resolution -> ontology mapping
           -> SHACL validation gate -> append-only event log

Nothing reaches the event log unless it passes the SHACL gate. Rejections
return a structured receipt (never silently dropped) so upstream systems can
route them to a repair/review queue.

Covered today: slowly-changing structured records (EntityCreated) and
high-rate location observations (LocationObserved). Unstructured documents
via LLM extraction plug in ahead of the same gate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ontology_utils import materialize_type_closure
from pyshacl import validate
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.namespace import XSD

from foundry.events import EventLog, SemanticEvent, make_event
from foundry.identity import IdentityService

CORE = "https://ontology.example/core#"

# Entity types allowed at ingestion; every entry is a subclass of core:Entity.
ALLOWED_ENTITY_TYPES = frozenset(
    {
        "Person",
        "Organization",
        "PhysicalObject",
        "Artifact",
        "Platform",
        "Facility",
        "System",
        "InformationObject",
        "Source",
    }
)


@dataclass(frozen=True)
class IngestResult:
    """Structured receipt for one ingested record.

    Accepted records carry the emitted event id and canonical entity id;
    rejected records carry a human-readable reason for the review queue.
    """

    accepted: bool
    canonical_id: str
    event_id: str | None = None
    reason: str = ""
    event: SemanticEvent | None = None
    is_new: bool = False


def _reject(canonical_id: str, reason: str) -> IngestResult:
    """Build a rejection receipt."""
    return IngestResult(accepted=False, canonical_id=canonical_id, reason=reason)


def _accept(canonical_id: str, event: SemanticEvent, *, is_new: bool = True) -> IngestResult:
    """Build an acceptance receipt."""
    return IngestResult(
        accepted=True,
        canonical_id=canonical_id,
        event_id=event.event_id if event is not None else None,
        event=event,
        is_new=is_new,
    )


class IngestionPipeline:
    """Canonical ingestion pipeline with a SHACL validation gate.

    Loads the ontology and shapes once; each record is mapped to a small RDF
    graph, validated, and only then appended to the immutable event log.
    """

    def __init__(
        self,
        *,
        identity: IdentityService,
        log: EventLog,
        ontology_path: Path,
        shapes_path: Path,
    ) -> None:
        """Preload ontology axioms and SHACL shapes once for all records."""
        self._identity = identity
        self._log = log
        self._ontology_path = ontology_path
        self._shapes = Graph()
        self._shapes.parse(shapes_path.as_posix(), format="turtle")

    # -- structured entities ------------------------------------------------

    def ingest_entity(
        self,
        *,
        name: str,
        entity_type: str,
        source_id: str,
        external_source: str | None = None,
        external_id: str | None = None,
        aliases: list[str] | None = None,
    ) -> IngestResult:
        """Ingest one slowly-changing structured entity record.

        ``aliases`` are bound to the resolved canonical identity so future
        references by alternate names resolve exactly instead of fuzzily, and
        are persisted on the emitted ``EntityCreated`` event as ``name_aliases``
        so downstream projections (read model, lake) can serve bilingual data.

        Raises:
            ValueError: On malformed input (empty name/source).
        """
        if not name.strip():
            raise ValueError("entity name must be non-empty")
        if not source_id:
            raise ValueError("source_id is required")
        if entity_type not in ALLOWED_ENTITY_TYPES:
            return _reject("", f"entity_type {entity_type!r} is not an ingestible core type")

        resolution = self._identity.resolve(
            name=name,
            external_source=external_source,
            external_id=external_id,
            entity_type=entity_type,
        )
        if resolution.method == "review":
            return _reject(
                "",
                f"name matches candidate entities {resolution.candidates} "
                f"(score={resolution.confidence}); needs human review before merge",
            )

        if aliases:
            self._identity.register(
                entity_id=resolution.canonical_id,
                entity_type=entity_type,
                aliases=[a for a in aliases if a != name],
            )

        if resolution.is_new:
            event = make_event(
                "EntityCreated",
                {
                    "entity_id": resolution.canonical_id,
                    "entity_type": entity_type,
                    "name": name,
                    "name_aliases": [a for a in aliases if a != name] if aliases else [],
                    "external_ids": (
                        [{"source": external_source, "external_id": external_id}]
                        if external_source and external_id
                        else []
                    ),
                    "source_id": source_id,
                    "confidence": resolution.confidence,
                },
            )
            self._log.append(event)
            return _accept(resolution.canonical_id, event)

        return _accept(resolution.canonical_id, event=None, is_new=False)

    # -- high-rate observations ---------------------------------------------

    def ingest_location_observation(
        self,
        *,
        entity_name: str,
        entity_type: str,
        location_uri: str,
        valid_from: str,
        source_ids: list[str],
        confidence: float | None = None,
    ) -> IngestResult:
        """Ingest one location observation as a reified LocationAssertion.

        The observation is validated against the SHACL contract before it may
        enter the log; unknown entities and shape violations are rejected.

        Raises:
            ValueError: On malformed timestamps, empty sources or bad confidence.
        """
        try:
            datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid valid_from timestamp {valid_from!r}") from exc
        if not source_ids:
            raise ValueError("at least one source_id is required")
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            raise ValueError(f"confidence {confidence} outside [0, 1]")

        resolution = self._identity.resolve(name=entity_name, entity_type=entity_type)
        if resolution.method not in {"alias", "external_id"}:
            if resolution.method == "review":
                return _reject(
                    "",
                    f"entity reference matches candidates {resolution.candidates}; "
                    "resolve via exact alias or external id first",
                )
            return _reject(
                resolution.canonical_id,
                "unresolved entity reference; ingest the entity before observing it",
            )
        canonical_id = resolution.canonical_id

        data_graph = self._build_observation_graph(
            canonical_id=canonical_id,
            entity_type=entity_type,
            location_uri=location_uri,
            valid_from=valid_from,
            source_ids=source_ids,
        )
        conforms, _, results_text = validate(
            data_graph=data_graph,
            shacl_graph=self._shapes,
            inference="none",
            advanced=True,
        )
        if not conforms:
            return _reject(canonical_id, f"SHACL violation: {results_text.strip()}")

        event = make_event(
            "LocationObserved",
            {
                "entity_id": canonical_id,
                "location_uri": location_uri,
                "valid_from": valid_from,
                "source_ids": list(source_ids),
                "confidence": confidence,
            },
        )
        self._log.append(event)
        return _accept(canonical_id, event)

    # -- internals -----------------------------------------------------------

    def _build_observation_graph(
        self,
        *,
        canonical_id: str,
        entity_type: str,
        location_uri: str,
        valid_from: str,
        source_ids: list[str],
        confidence: float | None = None,
    ) -> Graph:
        """Map one observation onto the core ontology and expand type closure."""
        graph = Graph()
        graph.parse(self._ontology_path.as_posix(), format="turtle")

        subject = URIRef("urn:fact:" + uuid.uuid4().hex)
        entity = URIRef(canonical_id)
        location = URIRef(location_uri)

        graph.add((subject, RDF.type, URIRef(CORE + "LocationAssertion")))
        graph.add((subject, URIRef(CORE + "describes"), entity))
        graph.add((subject, URIRef(CORE + "locatedAt"), location))
        graph.add(
            (
                subject,
                URIRef(CORE + "validFrom"),
                Literal(valid_from, datatype=XSD.dateTime),
            )
        )
        for source_id in source_ids:
            source_node = URIRef(source_id)
            graph.add((subject, URIRef(CORE + "hasSource"), source_node))
            graph.add((source_node, RDF.type, URIRef(CORE + "Source")))
            graph.add((source_node, URIRef(CORE + "name"), Literal(source_id)))
        if confidence is not None:
            graph.add(
                (
                    subject,
                    URIRef(CORE + "hasConfidence"),
                    Literal(str(confidence), datatype=XSD.decimal),
                )
            )

        graph.add((entity, RDF.type, URIRef(CORE + entity_type)))
        _, aliases, _ = self._identity.identity(canonical_id)
        display_name = next(iter(sorted(aliases)), canonical_id)
        graph.add((entity, URIRef(CORE + "name"), Literal(display_name)))
        graph.add((location, RDF.type, URIRef(CORE + "Location")))
        location_label = location_uri.rstrip("/").rsplit("/", 1)[-1].split("#")[-1]
        graph.add((location, URIRef(CORE + "name"), Literal(location_label)))
        materialize_type_closure(graph)
        return graph
