"""Tests for bounded source connectors, raw snapshots and canonical mapping."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest
from conftest import SHAPES_FILE

from foundry.events import EventLog, make_event
from foundry.extraction import CandidateFact
from foundry.identity import IdentityService
from foundry.ingestion import IngestionPipeline
from foundry.merge import rebuild_identity
from foundry.namespaces import CORE_MEMBER_OF, ORGANIZATION_SUBSIDIARY_OF
from foundry.projector import Projector
from foundry.provenance import content_sha256, stable_source_record_key
from foundry.public_sources import (
    CandidateFactExtractor,
    GleifEntityMapper,
    WikidataEntityMapper,
    WikipediaDocumentMapper,
    ingest_entity_record,
    valid_lei,
)
from foundry.query_service import QueryService
from foundry.readmodel import ReadModel
from foundry.source_adapters import (
    CheckpointStore,
    ConnectorPage,
    JsonHttpClient,
    RawArchive,
    RawRecord,
    SourceAdapterError,
    SourceContract,
    load_source_contracts,
    run_connector_page,
)

REGISTRY = Path(__file__).resolve().parents[1] / "config" / "source_registry.json"
REPO_ROOT = Path(__file__).resolve().parents[1]
WIKIDATA_CONTRACT = SourceContract(
    id="wikidata",
    identifier="https://www.wikidata.org",
    record_schema="test",
    license="test",
    storage_policy="test",
    redistribution_policy="test",
    freshness_policy="test",
    identifier_rules="test",
    mapping_rules="test",
    data_quality_checks=("test",),
)
GLEIF_CONTRACT = SourceContract(
    id="gleif",
    identifier="https://www.gleif.org",
    record_schema="test",
    license="test",
    storage_policy="test",
    redistribution_policy="test",
    freshness_policy="test",
    identifier_rules="test",
    mapping_rules="test",
    data_quality_checks=("test",),
)
WIKIPEDIA_CONTRACT = SourceContract(
    id="wikipedia-en",
    identifier="https://en.wikipedia.org",
    record_schema="test",
    license="test",
    storage_policy="test",
    redistribution_policy="test",
    freshness_policy="test",
    identifier_rules="test",
    mapping_rules="test",
    data_quality_checks=("test",),
)
LEI = "INR2EJN1ERAN0W5ZP974"


class FakeConnector:
    """One-page connector used to exercise checkpoint behavior offline."""

    contract = WIKIDATA_CONTRACT
    request_fingerprint = content_sha256("fake-connector-v1")

    def __init__(self, body: bytes) -> None:
        """Set the raw response bytes used by the test connector."""
        self.body = body
        self.cursors: list[str | None] = []

    def fetch_page(self, cursor: str | None) -> ConnectorPage:
        self.cursors.append(cursor)
        record = RawRecord(
            source_id="wikidata",
            record_id="record-1",
            source_uri="https://example.org/record-1",
            schema_version="test",
            body=self.body,
            retrieved_at="2026-11-16T00:00:00Z",
        )
        return ConnectorPage((record,), None, True)


class IdentityMapper:
    """Map raw bytes to one string for the connector runner test."""

    mapping_version = "1"

    def map(self, record: RawRecord) -> tuple[str, ...]:
        return (record.body.decode("utf-8"),)


def wikidata_entity(
    qid: str, label: str, type_qid: str, *, lei: str | None = None, aliases: list[str] | None = None
) -> dict[str, object]:
    """Build a small wbgetentities fixture."""
    claims: dict[str, object] = {
        "P31": [
            {"mainsnak": {"datavalue": {"value": {"id": type_qid}}}},
        ]
    }
    if lei:
        claims["P1278"] = [{"mainsnak": {"datavalue": {"value": lei}}}]
    return {
        "id": qid,
        "labels": {"en": {"value": label, "language": "en"}},
        "aliases": {"en": [{"value": alias, "language": "en"} for alias in aliases or []]},
        "claims": claims,
    }


def raw_record(source_id: str, record_id: str, uri: str, body: dict[str, object]) -> RawRecord:
    """Serialize one source fixture into a raw response record."""
    return RawRecord(
        source_id=source_id,
        record_id=record_id,
        source_uri=uri,
        schema_version="test",
        body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        retrieved_at="2026-11-16T00:00:00Z",
    )


def make_pipeline(tmp_path: Path, identity: IdentityService, log: EventLog) -> IngestionPipeline:
    """Construct the canonical ingestion pipeline for integration tests."""
    return IngestionPipeline(
        identity=identity,
        log=log,
        ontology_path=REPO_ROOT / "ontology" / "middle" / "organization.ttl",
        shapes_path=SHAPES_FILE,
    )


def test_source_registry_declares_storage_rights_freshness_and_quality() -> None:
    contracts = load_source_contracts(REGISTRY)
    assert set(contracts) == {"wikidata", "gleif", "wikipedia-en"}
    for contract in contracts.values():
        assert contract.record_schema
        assert contract.license
        assert contract.storage_policy
        assert contract.redistribution_policy
        assert contract.freshness_policy
        assert contract.identifier_rules
        assert contract.mapping_rules
        assert contract.data_quality_checks


def test_raw_archive_is_content_addressed_and_keeps_manifest(tmp_path: Path) -> None:
    archive = RawArchive(tmp_path / "raw")
    record = RawRecord(
        "wikidata", "page-1", "https://example.org", "test", b'{"x":1}', "2026-11-16T00:00:00Z"
    )

    first = archive.store(record, WIKIDATA_CONTRACT)
    second = archive.store(record, WIKIDATA_CONTRACT)

    assert first == second
    assert first.path.read_bytes() == record.body
    entries = (first.path.parent / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(entries) == 2
    assert json.loads(entries[0])["content_hash"] == first.content_hash
    restored = archive.read_latest("wikidata", "page-1", WIKIDATA_CONTRACT)
    assert restored.body == record.body
    assert restored.content_hash == record.content_hash


def test_checkpoint_advances_only_after_every_mapped_command_is_handled(tmp_path: Path) -> None:
    connector = FakeConnector(b"sample")
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    archive = RawArchive(tmp_path / "raw")

    def fail_after_map(_record, _mapped, _archived) -> None:
        raise RuntimeError("durable commit failed")

    with pytest.raises(RuntimeError, match="durable commit failed"):
        run_connector_page(
            connector,
            IdentityMapper(),
            archive=archive,
            checkpoints=checkpoints,
            handle=fail_after_map,
        )
    assert checkpoints.load("wikidata") is None

    processed: list[str] = []
    result = run_connector_page(
        connector,
        IdentityMapper(),
        archive=archive,
        checkpoints=checkpoints,
        handle=lambda _raw, mapped, _archived: processed.extend(mapped),
    )
    assert connector.cursors == [None, None]
    assert processed == ["sample"]
    assert result is not None and result.complete
    assert checkpoints.load("wikidata")["complete"] is True
    assert (
        run_connector_page(
            connector,
            IdentityMapper(),
            archive=archive,
            checkpoints=checkpoints,
            handle=lambda *_args: pytest.fail("completed checkpoint must not rerun"),
        )
        is None
    )


def test_checkpoint_rejects_reuse_with_different_connector_parameters(tmp_path: Path) -> None:
    connector = FakeConnector(b"sample")
    checkpoints = CheckpointStore(tmp_path / "checkpoints")
    archive = RawArchive(tmp_path / "raw")
    run_connector_page(
        connector,
        IdentityMapper(),
        archive=archive,
        checkpoints=checkpoints,
        handle=lambda *_args: None,
    )
    connector.request_fingerprint = content_sha256("different-query")

    with pytest.raises(SourceAdapterError, match="configuration does not match"):
        run_connector_page(
            connector,
            IdentityMapper(),
            archive=archive,
            checkpoints=checkpoints,
            handle=lambda *_args: None,
        )


def test_json_http_client_retries_rate_limit_and_uses_retry_after() -> None:
    calls = 0
    sleeps: list[float] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return b"{}"

    def opener(_request, timeout):
        nonlocal calls
        assert timeout == 30
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                "https://example.org",
                429,
                "rate limited",
                {"Retry-After": "2"},
                None,
            )
        return Response()

    client = JsonHttpClient(
        user_agent="test-bot/1.0",
        minimum_interval=0,
        retries=1,
        backoff=0.1,
        opener=opener,
        sleep=sleeps.append,
    )
    assert client.get("https://example.org/data", accept="application/json") == b"{}"
    assert calls == 2
    assert sleeps == [2.0]


def test_wikidata_mapper_extracts_identity_alias_and_valid_lei_crosswalk() -> None:
    body = raw_record(
        "wikidata",
        "entities:0-1",
        "https://www.wikidata.org/w/api.php",
        {"entities": {"Q2283": wikidata_entity("Q2283", "Microsoft", "Q1058914", lei=LEI)}},
    )

    mapped = WikidataEntityMapper().map(body)

    assert len(mapped) == 1
    assert mapped[0].name == "Microsoft"
    assert mapped[0].external_source == "wikidata"
    assert mapped[0].external_id == "Q2283"
    assert mapped[0].crosswalks[0].namespace == "gleif-lei"
    assert mapped[0].crosswalks[0].value == LEI
    assert valid_lei(LEI)
    assert not valid_lei("INR2EJN1ERAN0W5ZP970")


def test_entity_content_hash_versions_a_source_record_idempotently(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    identity = IdentityService()
    pipeline = make_pipeline(tmp_path, identity, log)
    version_one = content_sha256("source version 1")
    version_two = content_sha256("source version 2")
    command = {
        "name": "Versioned Organization",
        "entity_type": "Organization",
        "source_id": "wikidata",
        "record_id": "Q900",
        "external_source": "wikidata",
        "external_id": "Q900",
    }

    first = pipeline.ingest_entity(**command, content_hash=version_one)
    second = pipeline.ingest_entity(**command, content_hash=version_two)
    replay = pipeline.ingest_entity(**command, content_hash=version_two)

    assert first.accepted and second.accepted and replay.replayed
    assert first.canonical_id == second.canonical_id == replay.canonical_id
    assert first.event.payload["content_hash"] == version_one
    assert second.event.payload["content_hash"] == version_two
    assert log.last_sequence == 2


def test_entity_without_source_hash_replays_legacy_idempotency_fingerprint(tmp_path: Path) -> None:
    command = {
        "name": "Legacy Organization",
        "entity_type": "Organization",
        "source_id": "urn:source:wikidata",
        "record_id": "Q901",
        "name_aliases": [],
        "external_ids": [{"source": "wikidata", "external_id": "Q901"}],
    }
    command_hash = content_sha256(
        json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    legacy_fingerprint = content_sha256(
        json.dumps(
            {"event_type": "EntityCreated", "command": command},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    legacy_key = f"entity-record:{stable_source_record_key('wikidata', 'Q901')}:{command_hash}"

    generated_log = EventLog(tmp_path / "generated.jsonl")
    generated = make_pipeline(tmp_path, IdentityService(), generated_log).ingest_entity(
        name="Legacy Organization",
        entity_type="Organization",
        source_id="wikidata",
        record_id="Q901",
        external_source="wikidata",
        external_id="Q901",
    )
    assert generated.event is not None

    legacy_log = EventLog(tmp_path / "legacy.jsonl")
    legacy_log.commit(
        [make_event(generated.event.event_type, generated.event.payload)],
        idempotency_key=legacy_key,
        request_fingerprint=legacy_fingerprint,
    )
    replay = make_pipeline(tmp_path, IdentityService(), legacy_log).ingest_entity(
        name="Legacy Organization",
        entity_type="Organization",
        source_id="wikidata",
        record_id="Q901",
        external_source="wikidata",
        external_id="Q901",
    )

    assert replay.accepted and replay.replayed
    assert legacy_log.last_sequence == 1


def test_gleif_mapper_rejects_response_for_a_different_requested_lei() -> None:
    body = raw_record(
        "gleif",
        "bad-lei",
        "https://api.gleif.org/api/v1/lei-records/bad-lei",
        {
            "data": {
                "id": LEI,
                "attributes": {"entity": {"legalName": {"name": "Microsoft Corporation"}}},
            }
        },
    )
    with pytest.raises(SourceAdapterError, match="does not match"):
        GleifEntityMapper().map(body)


def test_document_subject_qid_does_not_fall_back_to_a_matching_name(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    identity = IdentityService()
    pipeline = make_pipeline(tmp_path, identity, log)
    subject = pipeline.ingest_entity(
        name="Wikidata Page Title",
        entity_type="Organization",
        source_id="fixture-subject",
        external_source="wikidata",
        external_id="Q100",
    )
    target = pipeline.ingest_entity(
        name="Parent Organization",
        entity_type="Organization",
        source_id="fixture-target",
        external_source="wikidata",
        external_id="Q101",
    )
    candidate = CandidateFact(
        subject_name="Wikidata Page Title",
        subject_external_source="wikidata",
        subject_external_id="Q999",
        predicate_iri=CORE_MEMBER_OF,
        object_kind="entity",
        object_value=target.canonical_id,
        valid_from="2026-11-01T00:00:00Z",
        model_score=0.0,
        snippet="Statement.",
        evidence_span={
            "start": 0,
            "end": 10,
            "unit": "character",
            "exact_text": "Statement.",
        },
    )

    result = pipeline.ingest_document(
        uri="https://example.org/document",
        title="Document",
        source_system="fixture-docs",
        text="Statement.",
        record_id="document-1",
        extractor=CandidateFactExtractor([candidate]),
    )

    assert subject.accepted and target.accepted
    assert result.asserted == 0
    assert result.queued == 1
    assert not any(event.event_type == "AssertionMade" for event in log.read_all())


def test_live_source_mappers_reconcile_identity_and_query_document_evidence(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    identity = IdentityService()
    pipeline = make_pipeline(tmp_path, identity, log)
    wikidata = raw_record(
        "wikidata",
        "entities:0-3",
        "https://www.wikidata.org/w/api.php",
        {
            "entities": {
                "Q2283": wikidata_entity("Q2283", "Microsoft", "Q1058914", lei=LEI),
                "Q24039192": wikidata_entity("Q24039192", "BBC Studios", "Q783794"),
                "Q9531": wikidata_entity(
                    "Q9531", "British Broadcasting Corporation", "Q1126006", aliases=["BBC"]
                ),
            }
        },
    )
    entities = WikidataEntityMapper().map(wikidata)
    assert len(entities) == 3
    microsoft_id = ""
    for entity in entities:
        result = ingest_entity_record(pipeline, identity, log, entity)
        assert result.accepted
        if entity.external_id == "Q2283":
            microsoft_id = result.canonical_id

    gleif = raw_record(
        "gleif",
        LEI,
        f"https://api.gleif.org/api/v1/lei-records/{LEI}",
        {
            "meta": {"goldenCopy": {"publishDate": "2026-11-16T00:00:00Z"}},
            "data": {
                "id": LEI,
                "attributes": {
                    "entity": {
                        "legalName": {"name": "MICROSOFT CORPORATION", "language": "en"},
                        "otherNames": [],
                        "status": "ACTIVE",
                    }
                },
            },
        },
    )
    gleif_entity = GleifEntityMapper().map(gleif)[0]
    gleif_result = ingest_entity_record(pipeline, identity, log, gleif_entity)
    assert gleif_result.accepted
    assert not gleif_result.is_new
    assert gleif_result.canonical_id == microsoft_id

    text = (
        "BBC Studios Limited is a British media company. It is a commercial subsidiary "
        "of the BBC that was formed in April 2018 through a merger."
    )
    wiki = raw_record(
        "wikipedia-en",
        "article:42",
        "https://en.wikipedia.org/wiki/BBC_Studios",
        {
            "query": {
                "pages": [
                    {
                        "pageid": 42,
                        "title": "BBC Studios",
                        "pageprops": {"wikibase_item": "Q24039192"},
                        "revisions": [{"revid": 123, "timestamp": "2026-11-04T03:51:15Z"}],
                        "extract": text,
                    }
                ]
            }
        },
    )
    document = WikipediaDocumentMapper().map(wiki)[0]
    assert document.candidate_facts[0].subject_external_source == "wikidata"
    assert document.candidate_facts[0].subject_external_id == "Q24039192"
    assert document.candidate_facts[0].predicate_iri == ORGANIZATION_SUBSIDIARY_OF
    document_result = pipeline.ingest_document(
        uri=document.source_uri,
        title=document.title,
        source_system=document.source_id,
        text=document.text,
        content_ref=document.source_uri,
        record_id=document.record_id,
        extractor=CandidateFactExtractor(document.candidate_facts),
    )
    assert document_result.asserted == 1
    assertion_event = document_result.assertion_results[0].event
    assert assertion_event is not None
    assert assertion_event.payload["predicate_iri"] == ORGANIZATION_SUBSIDIARY_OF
    assert assertion_event.payload["valid_from"] == "2026-11-04T03:51:15Z"
    assert assertion_event.payload["recorded_at"] != assertion_event.payload["valid_from"]
    evidence = assertion_event.payload["evidence"][0]
    span = evidence["span"]
    assert document.text[span["start"] : span["end"]] == span["exact_text"]

    model = ReadModel()
    Projector(model).replay(log.read_all())
    provenance = QueryService(model).assertion_provenance(assertion_event.payload["assertion_id"])
    assert provenance is not None
    assert provenance["evidence"] == assertion_event.payload["evidence"]
    assert (
        identity.lookup(external_source="gleif-lei", external_id=LEI).canonical_id == microsoft_id
    )

    committed_sequence = log.last_sequence
    restarted_identity = rebuild_identity(log)
    restarted_pipeline = make_pipeline(tmp_path, restarted_identity, log)
    for entity in entities:
        replayed = ingest_entity_record(restarted_pipeline, restarted_identity, log, entity)
        assert replayed.accepted and replayed.replayed
    repeated_document = restarted_pipeline.ingest_document(
        uri=document.source_uri,
        title=document.title,
        source_system=document.source_id,
        text=document.text,
        content_ref=document.source_uri,
        record_id=document.record_id,
        extractor=CandidateFactExtractor(document.candidate_facts),
    )
    assert repeated_document.document_id == document_result.document_id
    assert repeated_document.assertion_results[0].replayed
    assert log.last_sequence == committed_sequence


def test_crosswalk_collision_is_not_bound_and_routes_to_review(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    identity = IdentityService()
    pipeline = make_pipeline(tmp_path, identity, log)
    first = pipeline.ingest_entity(
        name="Company One",
        entity_type="Organization",
        source_id="fixture-a",
        external_source="wikidata",
        external_id="Q100",
    )
    second = pipeline.ingest_entity(
        name="Company Two",
        entity_type="Organization",
        source_id="fixture-b",
        external_source="wikidata",
        external_id="Q101",
    )
    identity.add_external_id(second.canonical_id, "gleif-lei", LEI)
    log.append(
        make_event(
            "ExternalIdBound",
            {"entity_id": second.canonical_id, "source": "gleif-lei", "external_id": LEI},
        )
    )
    record = WikidataEntityMapper().map(
        raw_record(
            "wikidata",
            "entities:0-1",
            "https://www.wikidata.org/w/api.php",
            {"entities": {"Q100": wikidata_entity("Q100", "Company One", "Q783794", lei=LEI)}},
        )
    )[0]

    result = ingest_entity_record(pipeline, identity, log, record)

    assert result.accepted and result.canonical_id == first.canonical_id
    assert identity.store.external_owners("gleif-lei", LEI) == {second.canonical_id}
    queued = [event for event in log.read_all() if event.event_type == "ResolutionReviewQueued"]
    assert queued
    assert second.canonical_id in queued[-1].payload["candidates"]

    restarted_identity = rebuild_identity(log)
    restarted_pipeline = make_pipeline(tmp_path, restarted_identity, log)
    replay = ingest_entity_record(restarted_pipeline, restarted_identity, log, record)

    assert replay.accepted and replay.replayed
    assert (
        len([event for event in log.read_all() if event.event_type == "ResolutionReviewQueued"])
        == 1
    )
