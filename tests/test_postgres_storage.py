"""Integration checks for PostgreSQL identity and read projections."""

from __future__ import annotations

import importlib.util
import os
from dataclasses import replace

import pytest

from foundry.events import EVENT_SCHEMA_VERSION, EventLog, SemanticEvent, make_event, utc_now_iso
from foundry.identity import IdentityEvidence, IdentityService, SourceRecordIdentity
from foundry.merge import apply_identity_event
from foundry.namespaces import CORE_LOCATED_AT
from foundry.postgres import PostgresDatabase, PostgresIdentityStore
from foundry.postgres_readmodel import PostgresReadModelStore
from foundry.projector import Projector, replay_log
from foundry.query_service import QueryService, QueryTimeoutError
from foundry.readmodel import parse_instant
from tools.project_events import project_once


@pytest.fixture()
def postgres_database():
    """Use an isolated test database supplied explicitly by the caller."""
    dsn = os.environ.get("FOUNDRY_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set FOUNDRY_TEST_DATABASE_URL to run PostgreSQL integration tests")
    database = PostgresDatabase(dsn)
    database.migrate()
    database.reset_projection()
    yield database
    database.reset_projection()


def test_migrations_are_applied_once(postgres_database):
    postgres_database.migrate()
    with postgres_database.connection() as connection:
        rows = connection.execute(
            "SELECT version FROM foundry_schema_migrations ORDER BY version"
        ).fetchall()
    assert rows == [
        ("001_operational_store.sql",),
        ("002_assertion_surface_lookup.sql",),
    ]


def test_query_service_uses_postgres_indexes_and_bounded_contract(postgres_database, tmp_path):
    log = EventLog(tmp_path / "query-service.jsonl")
    entity_a = "urn:entity:query-a"
    entity_b = "urn:entity:query-b"
    entity_c = "urn:entity:query-c"
    relation = "https://example.test/ontology#connectedTo"
    events = [
        make_event(
            "EntityCreated",
            {
                "entity_id": entity_a,
                "entity_type": "Platform",
                "name": "Query Alpha",
                "name_aliases": ["Alpha Alias"],
            },
        ),
        make_event(
            "EntityCreated",
            {"entity_id": entity_b, "entity_type": "Platform", "name": "Query Bravo"},
        ),
        make_event(
            "EntityCreated",
            {"entity_id": entity_c, "entity_type": "Platform", "name": "Query Charlie"},
        ),
    ]
    for assertion_id, subject, object_id in (
        ("urn:assert:query-a-b", entity_a, entity_b),
        ("urn:assert:query-b-c", entity_b, entity_c),
    ):
        events.append(
            make_event(
                "AssertionMade",
                {
                    "assertion_id": assertion_id,
                    "subject_id": subject,
                    "predicate_iri": relation,
                    "object": {"kind": "entity", "value": object_id},
                    "valid_from": "2026-08-01T00:00:00Z",
                    "valid_until": None,
                    "recorded_at": "2026-08-02T00:00:00Z",
                    "source_ids": ["https://example.test/source/query"],
                    "evidence": [
                        {
                            "source_id": "https://example.test/source/query",
                            "locator": "p. 2",
                            "span": None,
                        }
                    ],
                    "evidence_quality": "primary",
                },
            )
        )
    log.extend(events)

    store = PostgresReadModelStore(postgres_database)
    Projector(store).replay_in_order(log.iter_all())
    queries = QueryService(store)
    at = parse_instant("2026-08-10T00:00:00Z")

    candidates = queries.entity_search("alpha alias")
    assert [entity.entity_id for entity in candidates.items] == [entity_a]
    direct = queries.relationships_as_of(entity_a, at, predicate_iri=relation)
    assert [row["object"]["value"] for row in direct.items] == [entity_b]
    paths = queries.traverse_relationships(entity_a, at, max_hops=2, predicate_iri=relation)
    assert any(path.nodes == (entity_a, entity_b, entity_c) for path in paths.items)
    provenance = queries.assertion_provenance("urn:assert:query-a-b")
    assert provenance["source_ids"] == ["https://example.test/source/query"]
    assert provenance["evidence"][0]["locator"] == "p. 2"
    history = queries.entity_timeline(entity_a, include_superseded=True)
    assert [row["assertion_id"] for row in history.items] == [
        "urn:assert:query-a-b",
    ]

    with (
        pytest.raises(QueryTimeoutError),
        store.query_timeout(100),
        postgres_database.connection() as connection,
    ):
        connection.execute("SELECT pg_sleep(1)")


@pytest.mark.skipif(importlib.util.find_spec("pyoxigraph") is None, reason="optional Oxigraph")
def test_query_adapter_evaluation_matches_postgres_and_oxigraph(postgres_database, tmp_path):
    """Run the same lookup, temporal and provenance probes on both stores."""
    from tools.compare_query_adapters import compare

    log = EventLog(tmp_path / "query-adapter-eval.jsonl")
    subject_id = "urn:entity:adapter-eval-subject"
    object_id = "urn:entity:adapter-eval-object"
    log.extend(
        [
            make_event(
                "EntityCreated",
                {"entity_id": subject_id, "entity_type": "Platform", "name": "Probe Subject"},
            ),
            make_event(
                "EntityCreated",
                {"entity_id": object_id, "entity_type": "Organization", "name": "Probe Object"},
            ),
            make_event(
                "AssertionMade",
                {
                    "assertion_id": "urn:assert:adapter-eval",
                    "subject_id": subject_id,
                    "predicate_iri": "https://example.test/ontology#connectedTo",
                    "object": {"kind": "entity", "value": object_id},
                    "valid_from": "2026-08-01T00:00:00Z",
                    "valid_until": None,
                    "recorded_at": "2026-08-02T00:00:00Z",
                    "source_ids": ["https://example.test/source/adapter-eval"],
                    "evidence": [
                        {
                            "source_id": "https://example.test/source/adapter-eval",
                            "locator": "p. 1",
                            "span": {
                                "start": 0,
                                "end": 12,
                                "unit": "unicode_codepoint",
                                "exact_text": "Probe Subject",
                            },
                        }
                    ],
                    "evidence_quality": "primary",
                },
            ),
            make_event(
                "AssertionMade",
                {
                    "assertion_id": "urn:assert:adapter-eval-earlier",
                    "subject_id": subject_id,
                    "predicate_iri": "https://example.test/ontology#connectedTo",
                    "object": {"kind": "entity", "value": object_id},
                    "valid_from": "2026-07-01T00:00:00Z",
                    "valid_until": "2026-08-01T00:00:00Z",
                    "recorded_at": "2026-08-02T00:00:00Z",
                    "source_ids": ["https://example.test/source/adapter-eval"],
                    "evidence": [],
                    "evidence_quality": "primary",
                },
            ),
        ]
    )
    Projector(PostgresReadModelStore(postgres_database)).replay_in_order(log.iter_all())

    result = compare(
        tmp_path / "query-adapter-eval.jsonl",
        tmp_path / "oxigraph-snapshots",
        postgres_database.dsn,
        runs=1,
    )
    assert result["equivalent"] is True
    assert result["results"]["as_of_assertions"] == 1
    assert result["latency_scope"]["comparable"] is True
    assert result["workload"]["boundary"] == "OperationalQueryBoundary"


def test_identity_bindings_survive_adapter_restart_and_split(postgres_database):
    first_id = "urn:world:entity:postgres-first"
    second_id = "urn:world:entity:postgres-second"
    first = IdentityService(PostgresIdentityStore(postgres_database))
    first.register(entity_id=first_id, entity_type="Platform", aliases=["Patrol Seven"])
    first.register(
        entity_id=second_id,
        entity_type="Platform",
        aliases=["Vessel Seven"],
        external_ids={"ais-mmsi": "123456789"},
    )
    key = SourceRecordIdentity("fixture-source", "row-7")
    first.store.bind_source_record(key, second_id)

    reopened = IdentityService(PostgresIdentityStore(PostgresDatabase(postgres_database.dsn)))
    assert len(reopened) == 2
    assert reopened.store.alias_owners("patrol seven") == {first_id}
    assert reopened.store.external_owners("ais-mmsi", "123456789") == {second_id}
    assert reopened.store.source_record_owner(key) == second_id
    evidence = IdentityEvidence("merge-7", "review", "explicit", "analyst")
    reopened.merge_entities(first_id, second_id, same_entity_evidence=evidence)
    after_merge = IdentityService(PostgresIdentityStore(PostgresDatabase(postgres_database.dsn)))
    assert len(after_merge) == 1
    assert after_merge.merged_into(second_id) == first_id
    assert after_merge.store.alias_owners("vessel seven") == {first_id}
    after_merge.split_entity(first_id, second_id, ("Vessel Seven",), (("ais-mmsi", "123456789"),))
    after_split = IdentityService(PostgresIdentityStore(PostgresDatabase(postgres_database.dsn)))
    assert len(after_split) == 2
    assert after_split.merged_into(second_id) == ""
    assert after_split.store.alias_owners("vessel seven") == {second_id}
    after_split.add_cannot_link(
        first_id,
        second_id,
        IdentityEvidence("evidence-7", "fixture", "distinct-records", "test"),
    )
    final = IdentityService(PostgresIdentityStore(PostgresDatabase(postgres_database.dsn)))
    assert (first_id, second_id) in final.store.cannot_links()


def test_postgres_cannot_link_follows_merge_components(postgres_database):
    survivor, duplicate, separate = (
        "urn:world:entity:postgres-" + suffix for suffix in ("survivor", "duplicate", "separate")
    )
    identity = IdentityService(PostgresIdentityStore(postgres_database))
    for entity_id in (survivor, duplicate, separate):
        identity.register(entity_id=entity_id, entity_type="Platform")
    cannot_link = IdentityEvidence("different-vessels", "review", "different-hulls", "analyst")
    identity.add_cannot_link(duplicate, separate, cannot_link)
    same_entity = IdentityEvidence("same-vessel", "registry", "hull-match", "analyst")
    identity.merge_entities(survivor, duplicate, same_entity_evidence=same_entity)

    assert identity.cannot_link_evidence_between_components(survivor, separate) == cannot_link
    with pytest.raises(ValueError, match="cannot-link"):
        identity.preview_merge(survivor, separate, same_entity_evidence=same_entity)


def test_projector_restarts_and_replays_only_new_suffix(postgres_database, tmp_path, monkeypatch):
    log = EventLog(tmp_path / "events.jsonl", max_segment_bytes=1)
    entity_id = "urn:world:entity:postgres-platform"
    entity = make_event(
        "EntityCreated",
        {
            "entity_id": entity_id,
            "entity_type": "Platform",
            "name": "Persistent Patrol Vessel",
            "name_aliases": ["Patrol Seven"],
            "source_id": "fixture-source",
            "source_record_id": "record-1",
            "external_ids": [{"source": "wikidata", "external_id": "Q7"}],
        },
    )
    assertion = make_event(
        "AssertionMade",
        {
            "assertion_id": "urn:assertion:postgres-location-1",
            "subject_id": entity_id,
            "predicate_iri": CORE_LOCATED_AT,
            "object": {"kind": "location", "value": "https://example.test/loc/7"},
            "valid_from": "2026-11-01T00:00:00Z",
            "valid_until": None,
            "recorded_at": "2026-11-01T00:01:00Z",
            "source_ids": ["fixture-source"],
            "evidence": [{"source_id": "fixture-source", "locator": "row-7", "span": None}],
            "confidence": 0.8,
            "evidence_quality": "primary",
        },
    )
    log.extend([entity, assertion])

    identity = IdentityService(PostgresIdentityStore(postgres_database))
    model = PostgresReadModelStore(postgres_database)
    result = project_once(log, identity, model)
    assert result["applied"] == 2
    assert result["checkpoint_sequence"] == 2

    reopened_database = PostgresDatabase(postgres_database.dsn)
    reopened = PostgresReadModelStore(reopened_database)
    reference, _reference_stats = replay_log(log)
    view = reopened.get_entity(entity_id)
    assert view is not None and view.name == "Persistent Patrol Vessel"
    assert view == reference.get_entity(entity_id)
    assert reopened.location_as_of(
        entity_id, parse_instant("2026-11-15T00:00:00Z")
    ) == reference.location_as_of(entity_id, parse_instant("2026-11-15T00:00:00Z"))
    assert reopened.get_assertion("urn:assertion:postgres-location-1") == reference.get_assertion(
        "urn:assertion:postgres-location-1"
    )
    assert reopened.entities_at("https://example.test/loc/7") == reference.entities_at(
        "https://example.test/loc/7"
    )
    reopened_identity = IdentityService(PostgresIdentityStore(reopened_database))
    assert reopened_identity.store.external_owners("wikidata", "Q7") == {entity_id}

    binding = make_event(
        "ExternalIdBound",
        {"entity_id": entity_id, "source": "wikidata", "external_id": "Q8"},
    )
    log.append(binding)
    monkeypatch.setattr(log, "read_all", lambda: pytest.fail("full event-log read is forbidden"))
    result = project_once(
        log,
        reopened_identity,
        reopened,
    )
    assert result["applied"] == 1
    assert result["checkpoint_sequence"] == 3
    assert reopened_identity.store.external_owners("wikidata", "Q8") == {entity_id}
    assert postgres_database.verify_projection(log.last_sequence)["consistent"] is True


def test_projection_rows_and_checkpoint_rollback_together(postgres_database):
    model = PostgresReadModelStore(postgres_database)
    projector = Projector(model)
    identity = IdentityService(PostgresIdentityStore(postgres_database))
    event = make_event(
        "EntityCreated",
        {
            "entity_id": "urn:world:entity:rollback",
            "entity_type": "Platform",
            "name": "Rollback Fixture",
        },
    )
    with pytest.raises(RuntimeError, match="injected failure"), model.transaction():
        apply_identity_event(identity, event)
        assert projector.apply(event) is True
        raise RuntimeError("injected failure")

    reopened = PostgresReadModelStore(PostgresDatabase(postgres_database.dsn))
    assert reopened.get_entity("urn:world:entity:rollback") is None
    reopened_identity = IdentityService(
        PostgresIdentityStore(PostgresDatabase(postgres_database.dsn))
    )
    assert reopened_identity.store.get("urn:world:entity:rollback") is None
    assert reopened.checkpoint_sequence is None
    assert reopened.applied_event_count == 0


def test_projector_group_commit_rolls_back_and_replays_idempotently(postgres_database, tmp_path):
    log = EventLog(tmp_path / "group-commit.jsonl")
    entity_id = "urn:world:entity:group-commit"
    log.extend(
        [
            make_event(
                "EntityCreated",
                {"entity_id": entity_id, "entity_type": "Platform", "name": "Group Commit"},
            ),
            make_event(
                "AssertionMade",
                {
                    "assertion_id": "urn:assertion:group-commit",
                    "subject_id": entity_id,
                    "predicate_iri": CORE_LOCATED_AT,
                    "object": {"kind": "location", "value": "https://example.test/loc/1"},
                    "valid_from": "2026-11-01T00:00:00Z",
                    "valid_until": None,
                    "recorded_at": "2026-11-01T00:01:00Z",
                    "source_ids": ["fixture-source"],
                    "evidence": [],
                },
            ),
        ]
    )
    events = list(log.iter_all())
    model = PostgresReadModelStore(postgres_database)
    identity = IdentityService(PostgresIdentityStore(postgres_database))
    projector = Projector(model)

    def fail_after_second_event(event):
        apply_identity_event(identity, event)
        if event.event_id == events[1].event_id:
            raise RuntimeError("injected group failure")

    with pytest.raises(RuntimeError, match="injected group failure"):
        projector.apply_batch(events, before_apply=fail_after_second_event)

    assert model.get_entity(entity_id) is None
    assert model.get_assertion("urn:assertion:group-commit") is None
    assert identity.store.get(entity_id) is None
    assert model.checkpoint_sequence is None
    assert model.applied_event_count == 0

    result = projector.apply_batch(
        events,
        before_apply=lambda event: apply_identity_event(identity, event),
    )
    assert (result.applied, result.skipped, result.duplicates) == (2, 0, 0)
    replayed = projector.apply_batch(
        events,
        before_apply=lambda event: apply_identity_event(identity, event),
    )
    assert (replayed.applied, replayed.skipped, replayed.duplicates) == (0, 0, 2)
    assert model.checkpoint_sequence == 2
    assert model.applied_event_count == 2
    assert postgres_database.verify_projection(log.last_sequence)["consistent"] is True


def test_new_name_collision_reopens_resolved_pending_assertions(postgres_database, tmp_path):
    log = EventLog(tmp_path / "pending-name-collision.jsonl")
    first_id = "urn:world:entity:pending-first"
    second_id = "urn:world:entity:pending-second"
    log.extend(
        [
            make_event(
                "AssertionMade",
                {
                    "assertion_id": "urn:assertion:pending-name-collision",
                    "subject_id": "",
                    "subject_ref": {"surface_form": "Ghost Vessel", "entity_type": "Platform"},
                    "predicate_iri": CORE_LOCATED_AT,
                    "object": {"kind": "location", "value": "https://example.test/loc/2"},
                    "valid_from": "2026-11-01T00:00:00Z",
                    "valid_until": None,
                    "recorded_at": "2026-11-01T00:01:00Z",
                    "source_ids": ["fixture-source"],
                    "evidence": [],
                },
            ),
            make_event(
                "EntityCreated",
                {"entity_id": first_id, "entity_type": "Platform", "name": "Ghost Vessel"},
            ),
        ]
    )
    model = PostgresReadModelStore(postgres_database)
    identity = IdentityService(PostgresIdentityStore(postgres_database))

    first = project_once(log, identity, model, limit=2)
    assert first["applied"] == 2
    resolved = model.get_assertion("urn:assertion:pending-name-collision")
    assert resolved["subject_id"] == first_id
    assert resolved["subject_status"] == "resolved"

    log.append(
        make_event(
            "EntityCreated",
            {"entity_id": second_id, "entity_type": "Platform", "name": "Ghost Vessel"},
        )
    )
    project_once(log, identity, model)
    ambiguous = model.get_assertion("urn:assertion:pending-name-collision")
    assert ambiguous["subject_id"] == ""
    assert ambiguous["subject_status"] == "ambiguous"


def test_group_ledger_records_skipped_events_with_checkpoint(postgres_database):
    entity_id = "urn:world:entity:group-ledger"
    created = replace(
        make_event(
            "EntityCreated",
            {"entity_id": entity_id, "entity_type": "Platform", "name": "Ledger Test"},
        ),
        sequence=1,
    )
    unknown = SemanticEvent(
        event_id="future-unsupported-ledger-event",
        event_type="FutureUnsupportedEvent",
        schema_version=EVENT_SCHEMA_VERSION,
        occurred_at=utc_now_iso(),
        payload={"payload": "ignored by this projector"},
        sequence=2,
    )
    model = PostgresReadModelStore(postgres_database)
    projector = Projector(model)

    result = projector.apply_batch([created, unknown])

    assert (result.applied, result.skipped, result.duplicates) == (1, 1, 0)
    assert model.checkpoint_sequence == 2
    assert model.applied_event_count == 1
    with postgres_database.connection() as connection:
        rows = connection.execute(
            "SELECT sequence, applied FROM projection_events ORDER BY sequence"
        ).fetchall()
    assert rows == [(1, True), (2, False)]
    assert postgres_database.verify_projection(2)["consistent"] is True
