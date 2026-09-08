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

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ontology_utils import materialize_type_closure
from pyshacl import validate
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.namespace import XSD

from foundry.events import EventLog, SemanticEvent, make_event
from foundry.identity import IdentityService
from foundry.namespaces import new_fact_iri

CORE = "https://damminhtien.github.io/ontology-research/ontology/core#"

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


def _reject(canonical_id: str, reason: str, event: SemanticEvent | None = None) -> IngestResult:
    """Build a rejection receipt, optionally carrying a durable queue event."""
    return IngestResult(accepted=False, canonical_id=canonical_id, reason=reason, event=event)


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
        # review-queue dedup within one process run: the same unresolved
        # reference is queued once per run, not once per record
        self._queued_references: set[tuple[str | None, str | None, str]] = set()

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
            queue_event = self._queue_review(
                reference_name=name,
                external_source=external_source,
                external_id=external_id,
                entity_type=entity_type,
                candidates=resolution.candidates,
                reason=f"name matches candidate entities {resolution.candidates} "
                f"(score={resolution.confidence}); needs human review before merge",
            )
            return _reject(
                "",
                f"name matches candidate entities {resolution.candidates} "
                f"(score={resolution.confidence}); needs human review before merge",
                event=queue_event,
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

        # Resolved by exact alias while the caller supplied an external id that
        # missed: bind the trusted id to the existing entity and record the
        # binding as a first-class fact (ExternalIdBound) so restarts recover
        # it from the log instead of re-deriving it every run.
        if resolution.method == "alias" and external_source and external_id:
            _, _, bound = self._identity.identity(resolution.canonical_id)
            if external_id not in bound.get(external_source, frozenset()):
                self._identity.add_external_id(
                    resolution.canonical_id, external_source, external_id
                )
                binding_event = make_event(
                    "ExternalIdBound",
                    {
                        "entity_id": resolution.canonical_id,
                        "source": external_source,
                        "external_id": external_id,
                    },
                )
                self._log.append(binding_event)
                return _accept(resolution.canonical_id, binding_event, is_new=False)
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
            reason = (
                f"entity reference matches candidates {resolution.candidates}; "
                "resolve via exact alias or external id first"
                if resolution.method == "review"
                else "unresolved entity reference; ingest the entity before observing it"
            )
            queue_event = self._queue_review(
                reference_name=entity_name,
                external_source=None,
                external_id=None,
                entity_type=entity_type,
                candidates=resolution.candidates,
                reason=reason,
            )
            return _reject(resolution.canonical_id, reason, event=queue_event)
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

    def _queue_review(
        self,
        *,
        reference_name: str | None,
        external_source: str | None,
        external_id: str | None,
        entity_type: str,
        candidates: tuple[str, ...],
        reason: str,
    ) -> SemanticEvent | None:
        """Append a durable ``ResolutionReviewQueued`` fact for a rejection.

        Rejections are no longer transient stdout noise: the review queue is
        replayable from the log, so a human review UI can drain it and a
        re-run of the same source does not silently lose it. Within one
        pipeline run the same reference is queued only once (cross-run
        repeats are legitimate — the reference is still unresolved).
        """
        key = (reference_name, external_source, external_id or "")
        if key in self._queued_references:
            return None
        self._queued_references.add(key)
        event = make_event(
            "ResolutionReviewQueued",
            {
                "reference_name": reference_name,
                "external_source": external_source,
                "external_id": external_id,
                "entity_type": entity_type,
                "candidates": list(candidates),
                "reason": reason,
            },
        )
        self._log.append(event)
        return event

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

        subject = URIRef(new_fact_iri())
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
