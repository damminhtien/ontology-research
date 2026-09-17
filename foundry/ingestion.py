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

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ontology_utils import materialize_type_closure
from pyshacl import validate
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.namespace import OWL, XSD

from foundry.assertions import (
    assertion_to_rdf,
    build_assertion,
    document_to_rdf,
    register_document,
)
from foundry.events import (
    EVENT_TYPE_ASSERTION_MADE,
    EVENT_TYPE_DOCUMENT_REGISTERED,
    EventLog,
    SemanticEvent,
    make_event,
)
from foundry.extraction import Extractor, PatternExtractor
from foundry.identity import IdentityService
from foundry.namespaces import (
    IDENTITY_MIDDLE_NS,
    new_fact_iri,
    pending_reference_iri,
)

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
    ``pending`` marks accepted observations whose subject had no canonical
    identity yet — they are recorded verbatim and link up when it exists
    (architecture §4.7).
    """

    accepted: bool
    canonical_id: str
    event_id: str | None = None
    reason: str = ""
    event: SemanticEvent | None = None
    is_new: bool = False
    pending: bool = False


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


@dataclass(frozen=True)
class DocumentIngestResult:
    """Receipt for one unstructured-document ingest (Phase 2).

    ``candidates`` = extractor proposals; ``asserted`` = candidates that became
    SHACL-valid assertions; ``queued`` = candidates whose subject is unresolved
    (durable review queue, never auto-minted); ``skipped`` = undated
    candidates the assertion contract cannot represent.
    """

    document_id: str
    document_event: SemanticEvent
    candidates: int
    asserted: int
    queued: int
    skipped: int
    assertion_results: tuple[IngestResult, ...]


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
        """Preload ontology axioms and SHACL shapes once for all records.

        Besides ``shapes_path``, the sibling ``assertion_shapes.ttl`` in the
        same directory is loaded when present so the assertion gate enforces
        the same SHACL contract the shapes file pins.
        """
        self._identity = identity
        self._log = log
        self._ontology_path = ontology_path
        self._shapes = Graph()
        self._shapes.parse(shapes_path.as_posix(), format="turtle")
        for sibling in ("assertion_shapes.ttl", "domain_shapes.ttl"):
            sibling_path = shapes_path.parent / sibling
            if sibling_path.exists():
                self._shapes.parse(sibling_path.as_posix(), format="turtle")
        # review-queue dedup within one process run: the same unresolved
        # reference is queued once per run, not once per record
        self._queued_references: set[tuple[str | None, str | None, str]] = set()
        # assertion-gate context, loaded lazily from the log once per run:
        # provenance documents and recorded assertions (for supersedes checks
        # and the SHACL graph). Documents registered by *this* pipeline are
        # also added incrementally via ``register_document``.
        self._documents: dict[str, SemanticEvent] = {}
        self._assertion_events: dict[str, SemanticEvent] = {}
        self._assertion_context_loaded = False
        # sensor-gate context (Phase 4): SensorRegistered events keyed by
        # canonical sensor id, so an observation gate graph carries the
        # sensor's mountedOn platform (sensor:SensorShape needs it)
        self._sensor_events: dict[str, SemanticEvent] = {}
        self._sensor_context_loaded = False
        self._observation_events: dict[str, SemanticEvent] = {}

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

        # Pure lookup — never mints (architecture §4.7): an unresolved
        # reference becomes a pending observation instead of polluting the
        # registry with an entity that has no EntityCreated event.
        found = self._identity.lookup(name=entity_name, entity_type=entity_type)
        if found.method not in {"alias", "external_id"}:
            if found.method == "ambiguous":
                reason = (
                    f"entity reference matches candidates {found.candidates}; "
                    "resolve via exact alias or external id first"
                )
                queue_event = self._queue_review(
                    reference_name=entity_name,
                    external_source=None,
                    external_id=None,
                    entity_type=entity_type,
                    candidates=found.candidates,
                    reason=reason,
                )
                return _reject("", reason, event=queue_event)
            # Miss with no candidates (§4.7): nothing to review — record the
            # observation verbatim against an UnresolvedReference placeholder;
            # the projector links it to the entity once it is minted.
            data_graph = self._build_pending_graph(
                entity_name=entity_name,
                location_uri=location_uri,
                valid_from=valid_from,
                source_ids=source_ids,
                confidence=confidence,
            )
            conforms, _, results_text = validate(
                data_graph=data_graph,
                shacl_graph=self._shapes,
                inference="none",
                advanced=True,
            )
            if not conforms:
                return _reject("", f"SHACL violation: {results_text.strip()}")
            event = make_event(
                "LocationObserved",
                {
                    "entity_id": "",  # no canonical id yet — pending by design
                    "entity_ref": entity_name,
                    "location_uri": location_uri,
                    "valid_from": valid_from,
                    "source_ids": list(source_ids),
                    "confidence": confidence,
                },
            )
            self._log.append(event)
            return replace(_accept(pending_reference_iri(entity_name), event), pending=True)
        canonical_id = found.canonical_id

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

    # -- generic assertions (docs/architecture.md §4.2) -----------------------

    def register_document(
        self,
        *,
        uri: str,
        title: str,
        source_system: str,
        content_ref: str | None = None,
    ) -> SemanticEvent:
        """Record a provenance source document and remember it for the gate."""
        event = register_document(
            log=self._log,
            uri=uri,
            title=title,
            source_system=source_system,
            content_ref=content_ref,
        )
        self._remember_context_event(event)
        return event

    def ingest_assertion(
        self,
        *,
        subject_id: str,
        predicate: str,
        object_kind: str,
        object_value: str,
        valid_from: str,
        source_ids: list[str],
        confidence: float | None = None,
        supersedes: str | None = None,
    ) -> IngestResult:
        """Record one reified assertion about a known canonical entity.

        The assertion is mapped to RDF (ontology/middle/assertion.ttl) together
        with its cited ``DocumentRegistered`` events and any superseded
        assertion, then validated against the SHACL contract before anything
        is appended — nothing reaches the log unless it conforms.

        Malformed input raises ``ValueError`` (caller bug); an unknown subject
        is a data problem — it is durably queued for review like every other
        unresolved reference. A SHACL violation (e.g. a cited document that
        was never registered) is rejected with a structured receipt.
        """
        if not source_ids:
            raise ValueError("at least one source_id is required")
        if not self._identity.knows(subject_id):
            queue_event = self._queue_review(
                reference_name=subject_id,
                external_source=None,
                external_id=None,
                entity_type="",
                candidates=(),
                reason=f"assertion subject {subject_id} is not a known canonical entity",
            )
            return _reject(subject_id, "unknown assertion subject", event=queue_event)
        self._load_assertion_context()
        event = build_assertion(
            subject_id=subject_id,
            predicate=predicate,
            object_kind=object_kind,
            object_value=object_value,
            valid_from=valid_from,
            source_ids=source_ids,
            confidence=confidence,
            supersedes=supersedes,
            known_assertions=frozenset(self._assertion_events),
        )
        graph = self._build_assertion_graph(event)
        conforms, _, results_text = validate(
            data_graph=graph,
            shacl_graph=self._shapes,
            inference="none",
            advanced=True,
        )
        if not conforms:
            return _reject(subject_id, f"SHACL violation: {results_text.strip()}")
        self._log.append(event)
        self._remember_context_event(event)
        return _accept(subject_id, event)

    def _build_pending_graph(
        self,
        *,
        entity_name: str,
        location_uri: str,
        valid_from: str,
        source_ids: list[str],
        confidence: float | None = None,
    ) -> Graph:
        """Map one observation whose subject has no canonical identity yet.

        The subject of the LocationAssertion is an
        ``identity:UnresolvedReference`` placeholder — a ``core:Entity``
        subclass carrying the cited surface form — so the assertion passes
        the same shapes without minting a fake canonical id (§4.7).
        """
        graph = Graph()
        graph.parse(self._ontology_path.as_posix(), format="turtle")
        # the placeholder's subClassOf axiom lives in the identity module —
        # without it, sh:class core:Entity cannot see through the subclass
        identity_module = self._ontology_path.parent.parent / "middle" / "identity.ttl"
        graph.parse(identity_module.as_posix(), format="turtle")

        subject = URIRef(new_fact_iri())
        entity = URIRef(pending_reference_iri(entity_name))
        location = URIRef(location_uri)

        graph.add((subject, RDF.type, URIRef(CORE + "LocationAssertion")))
        graph.add((subject, URIRef(CORE + "describes"), entity))
        graph.add((subject, URIRef(CORE + "locatedAt"), location))
        graph.add((subject, URIRef(CORE + "validFrom"), Literal(valid_from, datatype=XSD.dateTime)))
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

        graph.add((entity, RDF.type, URIRef(IDENTITY_MIDDLE_NS + "UnresolvedReference")))
        graph.add((entity, URIRef(CORE + "name"), Literal(entity_name)))
        graph.add((location, RDF.type, URIRef(CORE + "Location")))
        location_label = location_uri.rstrip("/").rsplit("/", 1)[-1].split("#")[-1]
        graph.add((location, URIRef(CORE + "name"), Literal(location_label)))
        materialize_type_closure(graph)
        return graph

    # -- unstructured documents (Phase 2 — LLM chỉ đề xuất) -------------------

    def ingest_document(
        self,
        *,
        uri: str,
        title: str,
        source_system: str,
        text: str,
        content_ref: str | None = None,
        extractor: Extractor | None = None,
    ) -> DocumentIngestResult:
        """Register a document, extract candidate facts, gate every one of them.

        Contract (roadmap Phase 2): the extractor *proposes*; the pipeline
        decides. Per candidate:

        - undated candidates are skipped (the SHACL assertion contract needs a
          temporal anchor);
        - the subject must resolve by exact alias/external id — otherwise the
          candidate is durably queued for review and **no entity is minted**;
        - entity objects cited by name resolve the same way, falling back to a
          deterministic pending-placeholder IRI (§4.7) the ledger can re-point
          later; location objects cited as text do the same;
        - accepted candidates become low-confidence assertions whose provenance
          is the citing document.
        """
        document_event = self.register_document(
            uri=uri, title=title, source_system=source_system, content_ref=content_ref
        )
        document_id = document_event.payload["document_id"]
        extractor = extractor if extractor is not None else PatternExtractor()
        candidates = extractor.extract(text)

        results: list[IngestResult] = []
        queued = skipped = 0
        for candidate in candidates:
            if candidate.valid_from is None:
                skipped += 1
                continue
            found = self._identity.lookup(name=candidate.subject_name)
            if found.method not in {"alias", "external_id"}:
                self._queue_review(
                    reference_name=candidate.subject_name,
                    external_source=None,
                    external_id=None,
                    entity_type="",
                    candidates=found.candidates,
                    reason=f"extracted from document {document_id}: {candidate.snippet}",
                )
                queued += 1
                continue

            object_kind, object_value = candidate.object_kind, candidate.object_value
            is_iri = object_value.startswith(("urn:", "http://", "https://"))
            if object_kind == "entity" and not is_iri and not self._identity.knows(object_value):
                obj_found = self._identity.lookup(name=object_value)
                if obj_found.method in {"alias", "external_id"}:
                    object_value = obj_found.canonical_id
                else:
                    object_value = pending_reference_iri(object_value)
            elif object_kind == "location" and not is_iri:
                object_value = pending_reference_iri(object_value)

            results.append(
                self.ingest_assertion(
                    subject_id=found.canonical_id,
                    predicate=candidate.predicate,
                    object_kind=object_kind,
                    object_value=object_value,
                    valid_from=candidate.valid_from,
                    source_ids=[document_id],
                    confidence=candidate.confidence,
                )
            )
        asserted = sum(1 for r in results if r.accepted)
        return DocumentIngestResult(
            document_id=document_id,
            document_event=document_event,
            candidates=len(candidates),
            asserted=asserted,
            queued=queued,
            skipped=skipped,
            assertion_results=tuple(results),
        )

    # -- sensor/tracking domain (Phase 4 — the tracking vertical) -------------

    def ingest_sensor(
        self,
        *,
        sensor_id: str,
        name: str,
        platform_name: str,
        source_id: str,
    ) -> IngestResult:
        """Register a sensor artifact mounted on a platform.

        Identity: the sensor serial is a trusted external id (ADR-0006) and
        the carrier platform resolves/mints by its name. The SHACL gate
        enforces ``sensor:SensorShape`` (mounted on exactly one Platform).
        """
        if not sensor_id.strip():
            raise ValueError("sensor_id must be non-empty")
        if not name.strip():
            raise ValueError("sensor name must be non-empty")

        sensor = self._identity.resolve(
            name=name,
            external_source="sensor-registry",
            external_id=sensor_id,
            entity_type="Artifact",
        )
        platform = self._identity.resolve(name=platform_name, entity_type="Platform")

        from foundry.tracking import sensor_event, sensor_to_rdf

        event = sensor_event(
            sensor_entity_id=sensor.canonical_id,
            sensor_id=sensor_id,
            name=name,
            platform_entity_id=platform.canonical_id,
            platform_name=platform_name,
            source_id=source_id,
        )
        graph = self._build_domain_graph(event, mappers=[sensor_to_rdf])
        conforms, _, results_text = validate(
            data_graph=graph,
            shacl_graph=self._shapes,
            inference="none",
            advanced=True,
        )
        if not conforms:
            return _reject(sensor.canonical_id, f"SHACL violation: {results_text.strip()}")
        self._log.append(event)
        self._sensor_events[sensor.canonical_id] = event
        return _accept(sensor.canonical_id, event)

    def ingest_observation(
        self,
        *,
        sensor_entity_id: str,
        subject_name: str,
        subject_entity_id: str,
        at_time: str,
        location_uri: str | None,
        source_ids: list[str],
        confidence: float | None = None,
    ) -> IngestResult:
        """Record one sensor detection as a ``core:Observation``.

        The SHACL gate enforces ``core:ObservationShape`` (time anchor, at
        least one source, observes an entity) plus ``sensor:detectedBy``.
        """
        if not source_ids:
            raise ValueError("at least one source_id is required")
        from datetime import datetime as _dt

        try:
            _dt.fromisoformat(at_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid at_time timestamp {at_time!r}") from exc

        from foundry.tracking import observation_event, observation_to_rdf

        self._load_sensor_context()
        event = observation_event(
            observation_id=new_fact_iri(),
            sensor_entity_id=sensor_entity_id,
            subject_entity_id=subject_entity_id,
            subject_name=subject_name,
            at_time=at_time,
            location_uri=location_uri,
            source_ids=source_ids,
            confidence=confidence,
        )
        graph = self._build_domain_graph(event, mappers=[observation_to_rdf])
        # carry the sensor's registration context so its mountedOn platform
        # is in the gate graph (sensor:SensorShape targets every sensor node)
        sensor_event = self._sensor_events.get(sensor_entity_id)
        if sensor_event is not None:
            from foundry.tracking import sensor_to_rdf

            sensor_to_rdf(graph, sensor_event)
        conforms, _, results_text = validate(
            data_graph=graph,
            shacl_graph=self._shapes,
            inference="none",
            advanced=True,
        )
        if not conforms:
            return _reject(subject_entity_id, f"SHACL violation: {results_text.strip()}")
        self._log.append(event)
        self._observation_events[event.payload["observation_id"]] = event
        return _accept(subject_entity_id, event)

    def ingest_track(
        self,
        *,
        track_id: str,
        subject_name: str,
        subject_entity_id: str,
        observation_ids: list[str],
        source_ids: list[str],
    ) -> IngestResult:
        """Record the derived track hypothesis (``tracking:Track``, ADR-0005).

        The SHACL gate enforces ``tracking:TrackShape``: exactly one resolved
        entity, at least one supporting observation, exactly one track id.
        """
        if not track_id.strip():
            raise ValueError("track_id must be non-empty")
        if not observation_ids:
            return _reject(subject_entity_id, "a track needs at least one observation")

        from foundry.tracking import track_event, track_to_rdf

        event = track_event(
            track_id=track_id,
            entity_id=subject_entity_id,
            subject_name=subject_name,
            observation_ids=observation_ids,
            source_ids=source_ids,
        )
        graph = self._build_domain_graph(event, mappers=[track_to_rdf])
        # observations must appear with their full context: typing them as
        # core:Observation makes ObservationShape fire (atTime, sources) —
        # and each cited observation's sensor must carry its mountedOn platform
        self._load_sensor_context()
        self._load_observation_context()
        from foundry.tracking import observation_to_rdf, sensor_to_rdf

        sensor_ids: set[str] = set()
        for observation_id in event.payload["observation_ids"]:
            observation_event = self._observation_events.get(observation_id)
            if observation_event is not None:
                observation_to_rdf(graph, observation_event)
                sensor_ids.add(observation_event.payload["sensor_entity_id"])
        for sensor_entity_id in sensor_ids:
            sensor_event = self._sensor_events.get(sensor_entity_id)
            if sensor_event is not None:
                sensor_to_rdf(graph, sensor_event)
        conforms, _, results_text = validate(
            data_graph=graph,
            shacl_graph=self._shapes,
            inference="none",
            advanced=True,
        )
        if not conforms:
            return _reject(subject_entity_id, f"SHACL violation: {results_text.strip()}")
        self._log.append(event)
        return _accept(subject_entity_id, event)

    def _build_domain_graph(self, event: SemanticEvent, *, mappers: list) -> Graph:
        """Map one domain event to RDF over the domain ontology modules."""
        graph = Graph()
        graph.parse(self._ontology_path.as_posix(), format="turtle")
        middle = self._ontology_path.parent.parent / "middle"
        domain = self._ontology_path.parent.parent / "domain"
        for module_path in (
            middle / "identity.ttl",
            domain / "sensor.ttl",
            domain / "tracking.ttl",
        ):
            if module_path.exists():
                graph.parse(module_path.as_posix(), format="turtle")
        for mapper in mappers:
            mapper(graph, event)
        materialize_type_closure(graph)
        return graph

    # -- internals -----------------------------------------------------------

    def _load_sensor_context(self) -> None:
        """Populate the sensor registration cache from the log (once per run)."""
        if self._sensor_context_loaded:
            return
        for event in self._log.read_all():
            if event.event_type == "SensorRegistered":
                self._sensor_events[event.payload["sensor_entity_id"]] = event
        self._sensor_context_loaded = True

    def _load_observation_context(self) -> None:
        """Populate the observation cache from the log (once per run)."""
        if self._observation_events:
            return
        for event in self._log.read_all():
            if event.event_type == "ObservationRecorded":
                self._observation_events[event.payload["observation_id"]] = event

    def _load_assertion_context(self) -> None:
        """Load document/assertion context from the log once per run.

        Assertions may also be written by other tools between pipeline runs;
        the lazily built cache reflects the log as of the first
        ``ingest_assertion`` call of this run.
        """
        if self._assertion_context_loaded:
            return
        self._assertion_context_loaded = True
        for event in self._log.read_all():
            self._remember_context_event(event)

    def _remember_context_event(self, event: SemanticEvent) -> None:
        """Cache one DocumentRegistered/AssertionMade event for the gate."""
        if event.event_type == EVENT_TYPE_DOCUMENT_REGISTERED:
            self._documents[event.payload["document_id"]] = event
        elif event.event_type == EVENT_TYPE_ASSERTION_MADE:
            self._assertion_events[event.payload["assertion_id"]] = event

    def _build_assertion_graph(self, event: SemanticEvent) -> Graph:
        """Map one assertion — with provenance and correction context — to RDF.

        The cited documents and any superseded assertion are mapped from the
        context cache so the SHACL class constraints (``sh:class
        assertion:Document`` / ``assertion:Assertion``) can hold, and every
        mapped assertion's subject and object are typed and named (mirrors the
        observation gate: ``sh:class`` constraints and NamedThingShape need
        typed, named nodes in the gate graph).
        """
        graph = Graph()
        graph.parse(self._ontology_path.as_posix(), format="turtle")

        stack: list[SemanticEvent] = [event]
        visited: set[str] = set()
        while stack:
            current = stack.pop()
            assertion_id = current.payload["assertion_id"]
            if assertion_id in visited:
                continue
            visited.add(assertion_id)
            assertion_to_rdf(graph, current)
            self._type_and_name(graph, current)
            for source_id in current.payload.get("source_ids") or ():
                document = self._documents.get(source_id)
                if document is not None:
                    document_to_rdf(graph, document)
            target_id = current.payload.get("supersedes")
            if target_id is not None and target_id in self._assertion_events:
                stack.append(self._assertion_events[target_id])

        materialize_type_closure(graph)
        return graph

    def _type_and_name(self, graph: Graph, mapped: SemanticEvent) -> None:
        """Type and name the subject and object of one mapped assertion."""
        entity_type, aliases, _ = self._identity.identity(mapped.payload["subject_id"])
        subject = URIRef(mapped.payload["subject_id"])
        class_uri = URIRef(CORE + entity_type)
        if (class_uri, RDF.type, OWL.Class) not in graph:
            class_uri = URIRef(CORE + "Entity")  # unknown type: fall back to the root
        graph.add((subject, RDF.type, class_uri))
        display_name = next(iter(sorted(aliases)), mapped.payload["subject_id"])
        graph.add((subject, URIRef(CORE + "name"), Literal(display_name)))

        obj = mapped.payload["object"]
        if obj["kind"] not in ("entity", "location"):
            return  # literal objects carry the value as core:name on the assertion
        object_node = URIRef(obj["value"])
        label = obj["value"].rstrip("/").rsplit("/", 1)[-1].split("#")[-1]
        if obj["kind"] == "location":
            graph.add((object_node, RDF.type, URIRef(CORE + "Location")))
        elif self._identity.knows(obj["value"]):
            obj_type, obj_aliases, _ = self._identity.identity(obj["value"])
            obj_class = URIRef(CORE + obj_type)
            if (obj_class, RDF.type, OWL.Class) not in graph:
                obj_class = URIRef(CORE + "Entity")
            graph.add((object_node, RDF.type, obj_class))
            label = next(iter(sorted(obj_aliases)), label)
        else:
            graph.add((object_node, RDF.type, URIRef(CORE + "Entity")))
        graph.add((object_node, URIRef(CORE + "name"), Literal(label)))

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
