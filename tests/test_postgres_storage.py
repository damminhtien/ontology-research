"""Integration checks for PostgreSQL identity and read projections."""

from __future__ import annotations

import os

import pytest

from foundry.events import EventLog, make_event
from foundry.identity import IdentityEvidence, IdentityService, SourceRecordIdentity
from foundry.merge import apply_identity_event
from foundry.namespaces import CORE_LOCATED_AT
from foundry.postgres import PostgresDatabase, PostgresIdentityStore
from foundry.postgres_readmodel import PostgresReadModelStore
from foundry.projector import Projector, replay_log
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
    assert rows == [("001_operational_store.sql",)]


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

    reopened = PostgresReadModelStore(PostgresDatabase(postgres_database.dsn))
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
    reopened_identity = IdentityService(
        PostgresIdentityStore(PostgresDatabase(postgres_database.dsn))
    )
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
