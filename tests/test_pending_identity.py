"""Tests for unresolved-reference observations (D12 — architecture §4.7)."""

from __future__ import annotations

from conftest import CORE_ONTOLOGY, SHAPES_FILE
from pyshacl import validate
from rdflib import Graph, Literal, URIRef

from foundry.events import EventLog, make_event
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline
from foundry.namespaces import pending_reference_iri
from foundry.projector import replay_log
from foundry.readmodel import parse_instant

LOC = "https://data.example/entity/loc-da-nang"
SRC = "https://data.example/source/s1"


def _pipeline(log: EventLog) -> IngestionPipeline:
    return IngestionPipeline(
        identity=IdentityService(),
        log=log,
        ontology_path=CORE_ONTOLOGY,
        shapes_path=SHAPES_FILE,
    )


class TestPendingObservations:
    def test_unresolved_observation_is_accepted_pending(self, tmp_path):
        pipeline = _pipeline(EventLog(tmp_path / "events.jsonl"))
        result = pipeline.ingest_location_observation(
            entity_name="Ghost Vessel",
            entity_type="Platform",
            location_uri=LOC,
            valid_from="2026-08-20T03:00:00Z",
            source_ids=[SRC],
        )
        assert result.accepted and result.pending
        assert result.canonical_id == pending_reference_iri("Ghost Vessel")
        payload = result.event.payload
        assert payload["subject_id"] == ""  # no fake canonical id
        assert payload["subject_ref"]["surface_form"] == "Ghost Vessel"

    def test_ambiguous_reference_routes_to_queue(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        pipeline = _pipeline(log)
        # multimap collision: one surface name, two same-type entities
        pipeline.ingest_entity(name="Trung đoàn 101", entity_type="Organization", source_id="s1")
        colliding = "urn:world:entity:" + "e" * 32
        pipeline._identity.register(
            entity_id=colliding, entity_type="Organization", aliases=["Trung đoàn 101"]
        )
        rejected = pipeline.ingest_location_observation(
            entity_name="Trung đoàn 101",
            entity_type="Organization",
            location_uri=LOC,
            valid_from="2026-08-20T03:00:00Z",
            source_ids=[SRC],
        )
        assert not rejected.accepted
        assert rejected.event.event_type == "ResolutionReviewQueued"

    def test_pending_links_when_entity_is_minted_later(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        pipeline = _pipeline(log)
        pending = pipeline.ingest_location_observation(
            entity_name="Ghost Vessel",
            entity_type="Platform",
            location_uri=LOC,
            valid_from="2026-08-01T03:00:00Z",
            source_ids=[SRC],
        )
        assert pending.pending

        minted = pipeline.ingest_entity(
            name="Ghost Vessel",
            entity_type="Platform",
            external_source="naval-registry",
            external_id="GV-1",
            source_id="s1",
        )
        assert minted.is_new

        model, _stats = replay_log(log)
        # the pending observation linked to the minted entity
        assert model.current_location(minted.canonical_id).location_uri == LOC
        assert model.pending_count == 0

    def test_unique_pending_link_is_removed_if_name_becomes_ambiguous(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        pipeline = _pipeline(log)
        pending = pipeline.ingest_location_observation(
            entity_name="Ghost Vessel",
            entity_type="Platform",
            location_uri=LOC,
            valid_from="2026-08-01T03:00:00Z",
            source_ids=[SRC],
        )
        first = pipeline.ingest_entity(name="Ghost Vessel", entity_type="Platform", source_id="s1")
        model, _stats = replay_log(log)
        assert (
            model.location_as_of(
                first.canonical_id,
                parse_instant("2026-08-02T00:00:00Z"),
            ).location_uri
            == LOC
        )

        second_id = "urn:world:entity:" + "c" * 32
        log.append(
            make_event(
                "EntityCreated",
                {
                    "entity_id": second_id,
                    "entity_type": "Platform",
                    "name": "Ghost Vessel",
                    "name_aliases": [],
                    "external_ids": [],
                    "source_id": SRC,
                    "confidence": 1.0,
                },
            )
        )
        model, _stats = replay_log(log)
        reference = pending.event.payload["subject_ref"]
        assertion = model.get_assertion(pending.event.payload["assertion_id"])
        assert assertion["subject_ref"] == reference
        assert assertion["subject_id"] == ""
        assert assertion["subject_status"] == "ambiguous"
        assert model.pending_count == 1
        assert (
            model.location_as_of(
                first.canonical_id,
                parse_instant("2026-08-02T00:00:00Z"),
            )
            is None
        )

    def test_pending_links_via_alias_too(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        pipeline = _pipeline(log)
        pipeline.ingest_location_observation(
            entity_name="Hai quan Viet Nam",
            entity_type="Organization",
            location_uri=LOC,
            valid_from="2026-08-01T03:00:00Z",
            source_ids=[SRC],
        )
        pipeline.ingest_entity(
            name="Hải quân nhân dân Việt Nam",
            entity_type="Organization",
            aliases=["Hai quan Viet Nam"],
            source_id="s1",
        )
        model, _stats = replay_log(log)
        assert model.pending_count == 0
        entities = model.find_by_name("Hải quân nhân dân Việt Nam")
        assert entities
        assert model.current_location(entities[0].entity_id).location_uri == LOC

    def test_replay_is_deterministic_with_pending(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        pipeline = _pipeline(log)
        pipeline.ingest_location_observation(
            entity_name="Ghost Vessel",
            entity_type="Platform",
            location_uri=LOC,
            valid_from="2026-08-01T03:00:00Z",
            source_ids=[SRC],
        )
        pipeline.ingest_entity(name="Ghost Vessel", entity_type="Platform", source_id="s1")

        model_a, stats_a = replay_log(log)
        model_b, stats_b = replay_log(log)
        assert stats_a == stats_b
        assert model_a.stats() == model_b.stats()

    def test_snapshot_round_trips_pending(self, tmp_path):
        from foundry.readmodel import ReadModel

        log = EventLog(tmp_path / "events.jsonl")
        pipeline = _pipeline(log)
        pipeline.ingest_location_observation(
            entity_name="Ghost Vessel",
            entity_type="Platform",
            location_uri=LOC,
            valid_from="2026-08-01T03:00:00Z",
            source_ids=[SRC],
        )
        model, _stats = replay_log(log)
        snapshot = tmp_path / "model.pkl"
        model.save_snapshot(snapshot)

        restored = ReadModel.load_snapshot(snapshot)
        assert restored is not None
        assert restored.pending_count == model.pending_count == 1


class TestUnresolvedReferenceShape:
    """The identity module's SHACL contract pins the placeholder invariants."""

    CORE = "https://damminhtien.github.io/ontology-research/ontology/core#"
    IDENTITY = "https://damminhtien.github.io/ontology-research/ontology/middle/identity#"

    def _placeholder_graph(self, *, with_name: bool) -> Graph:
        graph = Graph()
        graph.parse("ontology/middle/identity.ttl", format="turtle")
        entity = URIRef(pending_reference_iri("Ghost Vessel"))
        rdf_type = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
        graph.add((entity, rdf_type, URIRef(self.IDENTITY + "UnresolvedReference")))
        if with_name:
            graph.add((entity, URIRef(self.IDENTITY + "referenceText"), Literal("Ghost Vessel")))
        return graph

    def _shapes(self) -> Graph:
        return Graph().parse("shapes/identity_shapes.ttl", format="turtle")

    def test_placeholder_with_name_conforms(self):
        conforms, _, _report = validate(
            data_graph=self._placeholder_graph(with_name=True),
            shacl_graph=self._shapes(),
            inference="none",
            advanced=True,
        )
        assert conforms

    def test_placeholder_without_name_violates(self):
        conforms, _, _report = validate(
            data_graph=self._placeholder_graph(with_name=False),
            shacl_graph=self._shapes(),
            inference="none",
            advanced=True,
        )
        assert not conforms

    def test_pending_assertion_graph_conforms_without_entity_placeholder(self, tmp_path):
        """The generic assertion gate accepts a reference without typing it as Entity."""
        pipeline = _pipeline(EventLog(tmp_path / "events.jsonl"))
        result = pipeline.ingest_location_observation(
            entity_name="Ghost Vessel",
            entity_type="Platform",
            location_uri=LOC,
            valid_from="2026-08-20T03:00:00Z",
            source_ids=[SRC],
            confidence=0.9,
        )
        assert result.accepted and result.pending
        graph = pipeline._build_assertion_graph(result.event)
        entity_ref = URIRef(pending_reference_iri("Ghost Vessel"))
        assert (
            entity_ref,
            URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
            URIRef(self.IDENTITY + "UnresolvedReference"),
        ) in graph
        assert (
            entity_ref,
            URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
            URIRef(self.CORE + "Entity"),
        ) not in graph
        conforms, _, report = validate(
            data_graph=graph,
            shacl_graph=pipeline._shapes,
            inference="none",
            advanced=True,
        )
        assert conforms, report
