"""Contracts for token-gated crawl control and local process supervision."""

from __future__ import annotations

import io
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from foundry.console.api import crawler as crawler_api
from foundry.console.app import app
from foundry.console.crawl_control import CrawlRunManager


class FakeProcess:
    def __init__(self, command, **kwargs):
        """Capture the proposed process invocation without launching a child."""
        self.command = command
        self.kwargs = kwargs
        self.pid = 987654
        self.stdout = io.StringIO("https://example.test/?MAP_KEY=hidden\n")
        self.returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture()
def manager(tmp_path):
    return CrawlRunManager(tmp_path / "service", tmp_path / "crawl-runs")


def test_config_is_persistent_and_source_ids_are_allowlisted(manager):
    config = manager.save_config(source_id="adsb-fi", max_records=25, duration_seconds=45)
    assert config == {
        "source_id": "adsb-fi",
        "max_records": 25,
        "duration_seconds": 45,
    }
    assert CrawlRunManager(manager.service_root, manager.raw_root).get_config() == config
    with pytest.raises(ValueError, match="unknown regional source"):
        manager.save_config(source_id="../../events", max_records=25, duration_seconds=45)
    with pytest.raises(ValueError, match="max_records"):
        manager.save_config(source_id="adsb-fi", max_records=1001, duration_seconds=45)


def test_database_connection_context_closes_handle(manager):
    with manager._connect() as connection:
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_run_uses_allowlisted_cli_args_redacts_logs_and_blocks_parallel_runs(manager, monkeypatch):
    monkeypatch.setenv("FIRMS_MAP_KEY", "hidden")
    monkeypatch.setenv("FOUNDRY_CONSOLE_TOKEN", "console-secret")
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", "postgresql://user:pass@localhost/db")
    manager.save_config(source_id="nasa-firms", max_records=10, duration_seconds=30)
    spawned = []

    def fake_popen(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr("foundry.console.crawl_control.subprocess.Popen", fake_popen)
    run = manager.start_run()
    manager._log_readers[run["run_id"]].join(timeout=1)

    assert run["status"] == "running"
    assert spawned[0].command[spawned[0].command.index("--source") + 1] == "nasa-firms"
    assert "hidden" not in spawned[0].command
    assert "FIRMS_MAP_KEY" in spawned[0].kwargs["env"]
    assert "FOUNDRY_CONSOLE_TOKEN" not in spawned[0].kwargs["env"]
    assert "FOUNDRY_DATABASE_URL" not in spawned[0].kwargs["env"]
    log = manager.logs(run["run_id"])
    assert "hidden" not in log
    assert "[REDACTED]" in log
    with pytest.raises(RuntimeError, match="already active"):
        manager.start_run()


def test_gdelt_registry_id_is_translated_to_cli_argument(manager, monkeypatch):
    spawned = []
    monkeypatch.setattr(
        "foundry.console.crawl_control.subprocess.Popen",
        lambda command, **kwargs: spawned.append(FakeProcess(command, **kwargs)) or spawned[-1],
    )
    manager.save_config(source_id="gdelt-events", max_records=8, duration_seconds=10)

    manager.start_run()

    source_index = spawned[0].command.index("--source")
    assert spawned[0].command[source_index + 1] == "gdelt"


def test_missing_source_credential_is_reported_before_launch(manager, monkeypatch):
    monkeypatch.delenv("AISSTREAM_API_KEY", raising=False)
    manager.save_config(source_id="aisstream", max_records=10, duration_seconds=30)
    monkeypatch.setattr(
        "foundry.console.crawl_control.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("crawler should not launch without its key"),
    )

    with pytest.raises(ValueError, match="AISSTREAM_API_KEY"):
        manager.start_run()


def test_crawler_control_api_requires_token_for_configuration_and_runs(manager, monkeypatch):
    monkeypatch.setattr(crawler_api, "get_crawl_manager", lambda: manager)
    monkeypatch.setenv("FOUNDRY_CONSOLE_TOKEN", "console-secret")
    client = TestClient(app)

    state = client.get("/api/crawler/state")
    assert state.status_code == 200
    assert len(state.json()["sources"]) == 10
    assert "console-secret" not in json.dumps(state.json())

    payload = {
        "source_id": "adsb-lol",
        "max_records": 12,
        "duration_seconds": 20,
    }
    assert client.put("/api/crawler/config", json=payload).status_code == 401
    saved = client.put(
        "/api/crawler/config",
        json=payload,
        headers={"Authorization": "Bearer console-secret"},
    )
    assert saved.status_code == 200
    assert saved.json()["config"] == payload
    assert client.post("/api/crawler/runs", json={}).status_code == 401
    invalid = client.put(
        "/api/crawler/config",
        json={**payload, "source_id": "not-a-source"},
        headers={"Authorization": "Bearer console-secret"},
    )
    assert invalid.status_code == 422

    monkeypatch.setattr(
        "foundry.console.crawl_control.subprocess.Popen",
        lambda command, **kwargs: FakeProcess(command, **kwargs),
    )
    monkeypatch.setattr("foundry.console.crawl_control.os.killpg", lambda *_args: None)
    headers = {"Authorization": "Bearer console-secret"}
    started = client.post("/api/crawler/runs", json={}, headers=headers)
    assert started.status_code == 200
    run_id = started.json()["run_id"]
    stopped = client.post(f"/api/crawler/runs/{run_id}/stop", json={}, headers=headers)
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopping"


def test_crawler_state_does_not_return_credential_values(manager, monkeypatch):
    monkeypatch.setenv("AISSTREAM_API_KEY", "do-not-return-this")
    state = manager.state()
    aisstream = next(source for source in state["sources"] if source["id"] == "aisstream")
    assert aisstream["credential_configured"] is True
    assert "do-not-return-this" not in json.dumps(state)


def test_summary_drops_filesystem_paths_and_raw_source_errors(manager):
    summary_path = manager.raw_root / "run-safe" / "summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(
        json.dumps(
            {
                "run_root": "/private/crawl/run-safe",
                "event_log": "/private/crawl/run-safe/events.jsonl",
                "total_events": 3,
                "event_counts": {"AssertionCreated": 3},
                "sources": {
                    "aisstream": {
                        "status": "error",
                        "error": "request failed with token=top-secret",
                        "mapped_records": 4,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    summary = manager._read_summary("run-safe")

    assert summary == {
        "sources": {"aisstream": {"status": "error", "mapped_records": 4}},
        "event_counts": {"AssertionCreated": 3},
        "total_events": 3,
    }
