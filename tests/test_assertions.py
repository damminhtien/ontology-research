"""Tests for the Document/Assertion model (B5 foundation)."""

from __future__ import annotations

import json

import pytest
from conftest import CORE_ONTOLOGY, SHAPES_FILE

from foundry.assertions import (
    dump_assertions,
    is_superseded,
    load_assertions,
    make_assertion,
    register_document,
    supersede_assertion,
)
from foundry.events import EventLog, make_event
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline
from foundry.namespaces import (
    CORE_LOCATED_AT,
    CORE_MEMBER_OF,
    CORE_ONTOLOGY_NS,
    require_absolute_iri,
    resolve_legacy_predicate,
)
from foundry.projector import replay_log

E1 = "urn:world:entity:" + "a" * 32
LOC = "urn:world:location:" + "b" * 32


class TestPredicateIriContract:
    """The write path takes an absolute IRI and never resolves a local name."""

    def test_bare_local_name_is_rejected(self):
        with pytest.raises(ValueError, match="must be an absolute IRI"):
            require_absolute_iri("locatedAt", "predicate_iri")

    @pytest.mark.parametrize(
        "absolute",
        ["https://example.org/vocab#operatesIn", "http://example.org/x", "urn:x:relation"],
    )
    def test_absolute_iri_is_kept_verbatim(self, absolute):
        assert require_absolute_iri(absolute, "predicate_iri") == absolute

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_predicate_rejected(self, blank):
        with pytest.raises(ValueError, match="predicate_iri must be non-empty"):
            require_absolute_iri(blank, "predicate_iri")


class TestLegacyPredicateResolution:
    """v2 log upcasting: an explicit table, never a guessed IRI."""

    def test_known_legacy_names_map_through_the_table(self):
        assert resolve_legacy_predicate("locatedAt") == CORE_LOCATED_AT
        assert resolve_legacy_predicate("  memberOf  ") == CORE_MEMBER_OF

    def test_absolute_iri_passes_through(self):
        assert resolve_legacy_predicate("urn:x:relation") == "urn:x:relation"

    @pytest.mark.parametrize("unknown", ["someRandomRelation", ""])
    def test_unknown_legacy_name_fails_loudly(self, unknown):
        with pytest.raises(ValueError):
            resolve_legacy_predicate(unknown)


@pytest.fixture()
def log(tmp_path) -> EventLog:
    return EventLog(tmp_path / "events.jsonl")


@pytest.fixture()
def identity() -> IdentityService:
    svc = IdentityService()
    svc.register(entity_id=E1, entity_type="Organization", aliases=["Org A"])
    return svc


class TestDocuments:
    def test_register_document(self, log):
        event = register_document(
            log=log,
            uri="https://example.org/report-1",
            title="Weekly report",
            source_system="crawler",
        )
        assert event.event_type == "DocumentRegistered"
        assert event.payload["document_id"].startswith("urn:doc:")
        assert len(log.read_all()) == 1

    @pytest.mark.parametrize("field", ["uri", "title", "source_system"])
    def test_register_document_rejects_empty_fields(self, log, field):
        kwargs = {"uri": "https://x", "title": "t", "source_system": "s"}
        kwargs[field] = "  "
        with pytest.raises(ValueError, match="must be non-empty"):
            register_document(log=log, **kwargs)


class TestMakeAssertion:
    def test_happy_path(self, log, identity):
        event = make_assertion(
            log=log,
            subject_id=E1,
            predicate_iri=CORE_LOCATED_AT,
            object_kind="location",
            object_value=LOC,
            valid_from="2026-08-01T00:00:00Z",
            source_ids=["urn:doc:1"],
            confidence=0.9,
            identity=identity,
        )
        assert event.event_type == "AssertionMade"
        assert event.payload["assertion_id"].startswith("urn:assert:")
        assert event.payload["predicate_iri"] == CORE_LOCATED_AT
        assert event.payload["object"] == {"kind": "location", "value": LOC}
        assert event.payload["valid_to"] is None

    def test_absolute_predicate_iri_is_recorded_verbatim(self, log, identity):
        event = make_assertion(
            log=log,
            subject_id=E1,
            predicate_iri="https://example.org/vocab#operatesIn",
            object_kind="location",
            object_value=LOC,
            valid_from="2026-08-01T00:00:00Z",
            source_ids=["s"],
            identity=identity,
        )
        assert event.payload["predicate_iri"] == "https://example.org/vocab#operatesIn"

    def test_unknown_subject_rejected_when_registry_given(self, log, identity):
        with pytest.raises(ValueError, match="unknown subject"):
            make_assertion(
                log=log,
                subject_id="urn:world:entity:missing",
                predicate_iri=CORE_LOCATED_AT,
                object_kind="location",
                object_value=LOC,
                valid_from="2026-08-01T00:00:00Z",
                source_ids=["s"],
                identity=identity,
            )
        assert log.read_all() == []

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"predicate_iri": "  "}, "predicate_iri must be non-empty"),
            ({"predicate_iri": "locatedAt"}, "must be an absolute IRI"),
            ({"object_kind": "vibe"}, "object_kind must be one of"),
            ({"object_value": " "}, "object_value must be non-empty"),
            ({"valid_from": "not-a-date"}, "invalid instant"),
            ({"confidence": 1.5}, r"outside \[0, 1\]"),
            (
                {"object_kind": "literal", "literal_language": ""},
                "literal_language must be non-empty",
            ),
            (
                {
                    "object_kind": "literal",
                    "literal_language": "vi",
                    "literal_datatype_iri": "urn:x:t",
                },
                "never both",
            ),
            (
                {"object_kind": "literal", "literal_datatype_iri": "not-an-iri"},
                "literal_datatype_iri must be an absolute IRI",
            ),
            (
                {"object_kind": "location", "literal_datatype_iri": "urn:x:t"},
                "only valid for object_kind='literal'",
            ),
        ],
    )
    def test_validation_rejects_malformed_input(self, log, kwargs, match):
        base = {
            "subject_id": E1,
            "predicate_iri": CORE_LOCATED_AT,
            "object_kind": "location",
            "object_value": LOC,
            "valid_from": "2026-08-01T00:00:00Z",
            "source_ids": ["s"],
        }
        base.update(kwargs)
        with pytest.raises(ValueError, match=match):
            make_assertion(log=log, **base)
        assert log.read_all() == []  # nothing written on validation failure

    def test_supersede_round_trip(self, log, identity):
        first = make_assertion(
            log=log,
            subject_id=E1,
            predicate_iri=CORE_LOCATED_AT,
            object_kind="location",
            object_value=LOC,
            valid_from="2026-08-01T00:00:00Z",
            source_ids=["s"],
            identity=identity,
        )
        assertion_id = first.payload["assertion_id"]

        replacement = make_assertion(
            log=log,
            subject_id=E1,
            predicate_iri=CORE_LOCATED_AT,
            object_kind="location",
            object_value="urn:world:location:" + "c" * 32,
            valid_from="2026-08-02T00:00:00Z",
            source_ids=["s"],
            supersedes=assertion_id,
            identity=identity,
        )
        assert replacement.payload["supersedes"] == assertion_id

        supersede_assertion(log=log, assertion_id=assertion_id, reason="corrected")
        assert is_superseded(log, assertion_id)

        with pytest.raises(ValueError, match="already superseded"):
            supersede_assertion(log=log, assertion_id=assertion_id)
        with pytest.raises(ValueError, match="unknown assertion"):
            supersede_assertion(log=log, assertion_id="urn:assert:missing")

    def test_supersedes_unknown_target_rejected(self, log):
        with pytest.raises(ValueError, match="not a recorded assertion"):
            make_assertion(
                log=log,
                subject_id=E1,
                predicate_iri=CORE_LOCATED_AT,
                object_kind="location",
                object_value=LOC,
                valid_from="2026-08-01T00:00:00Z",
                source_ids=["s"],
                supersedes="urn:assert:missing",
            )


class TestLedgerReplay:
    def test_load_assertions_tracks_status(self, log, identity):
        first = make_assertion(
            log=log,
            subject_id=E1,
            predicate_iri=CORE_LOCATED_AT,
            object_kind="location",
            object_value=LOC,
            valid_from="2026-08-01T00:00:00Z",
            source_ids=["s"],
            identity=identity,
        )
        supersede_assertion(log=log, assertion_id=first.payload["assertion_id"])
        ledger = load_assertions(log)
        assert ledger[first.payload["assertion_id"]]["status"] == "superseded"
        assert "superseded" in dump_assertions(ledger)


def _entity_event(entity_id: str, name: str):
    return make_event(
        "EntityCreated",
        {
            "entity_id": entity_id,
            "entity_type": "Organization",
            "name": name,
            "source_id": "s",
            "confidence": 1.0,
        },
    )


class TestPipelineAssertions:
    @staticmethod
    def _pipeline(log_path) -> IngestionPipeline:
        return IngestionPipeline(
            identity=IdentityService(),
            log=EventLog(log_path),
            ontology_path=CORE_ONTOLOGY,
            shapes_path=SHAPES_FILE,
        )

    def test_ingest_assertion_accepts_known_subject(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        pipeline = self._pipeline(log_path)
        created = pipeline.ingest_entity(name="Org A", entity_type="Organization", source_id="s")
        doc = pipeline.register_document(
            uri="https://example.org/report-1",
            title="Weekly report",
            source_system="crawler",
        )
        result = pipeline.ingest_assertion(
            subject_id=created.canonical_id,
            predicate_iri=CORE_LOCATED_AT,
            object_kind="location",
            object_value=LOC,
            valid_from="2026-08-01T00:00:00Z",
            source_ids=[doc.payload["document_id"]],
            confidence=0.95,
        )
        assert result.accepted
        events = EventLog(log_path).read_all()
        assert [e.event_type for e in events] == [
            "EntityCreated",
            "DocumentRegistered",
            "AssertionMade",
        ]

    def test_assertion_citing_unregistered_document_rejected(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        pipeline = self._pipeline(log_path)
        created = pipeline.ingest_entity(name="Org A", entity_type="Organization", source_id="s")
        result = pipeline.ingest_assertion(
            subject_id=created.canonical_id,
            predicate_iri=CORE_LOCATED_AT,
            object_kind="location",
            object_value=LOC,
            valid_from="2026-08-01T00:00:00Z",
            source_ids=["urn:doc:missing"],
        )
        assert not result.accepted
        assert "SHACL violation" in result.reason
        assert [e.event_type for e in EventLog(log_path).read_all()] == ["EntityCreated"]

    def test_supersedes_chain_passes_the_gate(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        pipeline = self._pipeline(log_path)
        created = pipeline.ingest_entity(name="Org A", entity_type="Organization", source_id="s")
        doc = pipeline.register_document(
            uri="https://example.org/report-1",
            title="Weekly report",
            source_system="crawler",
        )
        first = pipeline.ingest_assertion(
            subject_id=created.canonical_id,
            predicate_iri=CORE_LOCATED_AT,
            object_kind="location",
            object_value=LOC,
            valid_from="2026-08-01T00:00:00Z",
            source_ids=[doc.payload["document_id"]],
        )
        second = pipeline.ingest_assertion(
            subject_id=created.canonical_id,
            predicate_iri=CORE_LOCATED_AT,
            object_kind="location",
            object_value="urn:world:location:" + "c" * 32,
            valid_from="2026-08-02T00:00:00Z",
            source_ids=[doc.payload["document_id"]],
            supersedes=first.event.payload["assertion_id"],
        )
        assert first.accepted and second.accepted
        events = EventLog(log_path).read_all()
        assert [e.event_type for e in events].count("AssertionMade") == 2

    def test_unknown_predicate_iri_never_reaches_the_log(self, tmp_path):
        """The ontology is the allowlist: only declared relations are assertable."""
        log_path = tmp_path / "events.jsonl"
        pipeline = self._pipeline(log_path)
        created = pipeline.ingest_entity(name="Org A", entity_type="Organization", source_id="s")
        doc = pipeline.register_document(
            uri="https://example.org/report-1",
            title="Weekly report",
            source_system="crawler",
        )
        result = pipeline.ingest_assertion(
            subject_id=created.canonical_id,
            predicate_iri="https://example.org/vocab#inventedRelation",
            object_kind="location",
            object_value=LOC,
            valid_from="2026-08-01T00:00:00Z",
            source_ids=[doc.payload["document_id"]],
        )
        assert not result.accepted
        assert "not declared by the registered ontology" in result.reason
        assert [e.event_type for e in EventLog(log_path).read_all()] == [
            "EntityCreated",
            "DocumentRegistered",
        ]

    def test_bare_predicate_name_is_a_caller_bug(self, tmp_path):
        """The boundary never resolves a local name into an IRI."""
        pipeline = self._pipeline(tmp_path / "events.jsonl")
        created = pipeline.ingest_entity(name="Org A", entity_type="Organization", source_id="s")
        with pytest.raises(ValueError, match="must be an absolute IRI"):
            pipeline.ingest_assertion(
                subject_id=created.canonical_id,
                predicate_iri="locatedAt",
                object_kind="location",
                object_value=LOC,
                valid_from="2026-08-01T00:00:00Z",
                source_ids=["urn:doc:1"],
            )

    def test_literal_object_type_is_recorded_end_to_end(self, tmp_path):
        """A language-tagged literal keeps its tag in the event payload."""
        log_path = tmp_path / "events.jsonl"
        pipeline = self._pipeline(log_path)
        created = pipeline.ingest_entity(name="Org A", entity_type="Organization", source_id="s")
        doc = pipeline.register_document(
            uri="https://example.org/report-1",
            title="Weekly report",
            source_system="crawler",
        )
        result = pipeline.ingest_assertion(
            subject_id=created.canonical_id,
            predicate_iri=CORE_ONTOLOGY_NS + "alias",
            object_kind="literal",
            object_value="Đà Nẵng",
            valid_from="2026-08-01T00:00:00Z",
            source_ids=[doc.payload["document_id"]],
            literal_language="vi",
        )
        assert result.accepted
        assert result.event.payload["object"] == {
            "kind": "literal",
            "value": "Đà Nẵng",
            "datatype_iri": None,
            "language": "vi",
        }

    def test_unknown_subject_is_queued_not_crashed(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        pipeline = self._pipeline(log_path)
        result = pipeline.ingest_assertion(
            subject_id="urn:world:entity:ghost",
            predicate_iri=CORE_LOCATED_AT,
            object_kind="location",
            object_value=LOC,
            valid_from="2026-08-01T00:00:00Z",
            source_ids=["urn:doc:1"],
        )
        assert not result.accepted
        assert result.event is not None
        assert result.event.event_type == "ResolutionReviewQueued"

    def test_malformed_input_raises_without_writing(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        pipeline = self._pipeline(log_path)
        created = pipeline.ingest_entity(name="Org A", entity_type="Organization", source_id="s")
        with pytest.raises(ValueError, match="outside"):
            pipeline.ingest_assertion(
                subject_id=created.canonical_id,
                predicate_iri=CORE_LOCATED_AT,
                object_kind="location",
                object_value=LOC,
                valid_from="2026-08-01T00:00:00Z",
                source_ids=["s"],
                confidence=2.0,
            )
        assert len(EventLog(log_path).read_all()) == 1  # only the EntityCreated


class TestAssertionProjection:
    def test_replay_folds_ledger_and_merge_repoints_subject(self, tmp_path):
        log = EventLog(tmp_path / "events.jsonl")
        e_org = "urn:world:entity:" + "c" * 32
        e_dup = "urn:world:entity:" + "d" * 32
        log.append(_entity_event(e_org, "Org"))
        log.append(_entity_event(e_dup, "Org dup"))
        assertion = make_assertion(
            log=log,
            subject_id=e_dup,
            predicate_iri=CORE_MEMBER_OF,
            object_kind="entity",
            object_value=e_org,
            valid_from="2026-08-01T00:00:00Z",
            source_ids=["s"],
        )
        aid = assertion.payload["assertion_id"]
        log.append(
            make_event(
                "EntityMerged",
                {
                    "survivor_id": e_org,
                    "duplicate_id": e_dup,
                    "moved_aliases": [],
                    "moved_external_ids": [],
                    "reason": "r",
                },
            )
        )
        model, stats = replay_log(log)
        assert stats.applied == 4
        entry = model.get_assertion(aid)
        assert entry is not None and entry["subject_id"] == e_org  # followed the merge
        assert entry["predicate_iri"] == CORE_MEMBER_OF
        # the relation filter takes the absolute IRI; a bare local name is
        # rejected rather than resolved, so a query can never match silently
        assert model.active_assertions(e_org, CORE_MEMBER_OF)
        with pytest.raises(ValueError, match="must be an absolute IRI"):
            model.active_assertions(e_org, "memberOf")
        assert model.active_assertions(e_org, CORE_LOCATED_AT) == []

        log.append(
            make_event(
                "AssertionSuperseded",
                {"assertion_id": aid, "reason": "wrong", "superseded_at": "2026-09-02T00:00:00Z"},
            )
        )
        model2, _stats2 = replay_log(log)
        assert model2.get_assertion(aid)["status"] == "superseded"
        assert model2.active_assertions(e_org, CORE_MEMBER_OF) == []


def test_json_serializable_payload(log):
    event = make_assertion(
        log=log,
        subject_id=E1,
        predicate_iri=CORE_LOCATED_AT,
        object_kind="literal",
        object_value="đà nẵng",
        valid_from="2026-08-01T00:00:00Z",
        source_ids=["s"],
    )
    line = json.dumps(event.payload, ensure_ascii=False)
    assert "đà nẵng" in line
