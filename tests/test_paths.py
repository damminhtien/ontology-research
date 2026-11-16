"""Defaults for repository data live outside the Git worktree."""

from __future__ import annotations

from foundry.paths import (
    crawl_path,
    data_root,
    demo_event_log_path,
    lake_path,
    postgres_data_path,
    production_event_log_path,
    reference_path,
)


def test_default_storage_paths_share_external_data_root(monkeypatch, tmp_path):
    monkeypatch.setenv("FOUNDRY_DATA_ROOT", str(tmp_path / "external"))
    for name in (
        "FOUNDRY_ADMIN_LOG",
        "FOUNDRY_EVENT_LOG",
        "FOUNDRY_LAKE_ROOT",
        "FOUNDRY_REFERENCE_ROOT",
        "FOUNDRY_CRAWL_ROOT",
        "FOUNDRY_POSTGRES_DATA",
    ):
        monkeypatch.delenv(name, raising=False)

    root = tmp_path / "external"
    assert data_root() == root
    assert production_event_log_path() == root / "data" / "production.jsonl"
    assert demo_event_log_path() == root / "data" / "events.jsonl"
    assert lake_path() == root / "data" / "lake"
    assert reference_path() == root / "data" / "reference"
    assert crawl_path() == root / "data" / "crawl-runs"
    assert postgres_data_path() == root / "postgresql"


def test_individual_storage_overrides_take_precedence(monkeypatch, tmp_path):
    overrides = {
        "FOUNDRY_ADMIN_LOG": tmp_path / "events" / "prod.jsonl",
        "FOUNDRY_EVENT_LOG": tmp_path / "events" / "demo.jsonl",
        "FOUNDRY_LAKE_ROOT": tmp_path / "lake",
        "FOUNDRY_REFERENCE_ROOT": tmp_path / "reference",
        "FOUNDRY_CRAWL_ROOT": tmp_path / "crawl",
        "FOUNDRY_POSTGRES_DATA": tmp_path / "postgres",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, str(value))

    assert production_event_log_path() == overrides["FOUNDRY_ADMIN_LOG"]
    assert demo_event_log_path() == overrides["FOUNDRY_EVENT_LOG"]
    assert lake_path() == overrides["FOUNDRY_LAKE_ROOT"]
    assert reference_path() == overrides["FOUNDRY_REFERENCE_ROOT"]
    assert crawl_path() == overrides["FOUNDRY_CRAWL_ROOT"]
    assert postgres_data_path() == overrides["FOUNDRY_POSTGRES_DATA"]
