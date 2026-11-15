"""Default external locations for operational and downloaded data."""

from __future__ import annotations

import os
from pathlib import Path

DATA_ROOT_ENV = "FOUNDRY_DATA_ROOT"
DEFAULT_DATA_ROOT = Path.home() / "Data" / "ontology-research"


def data_root() -> Path:
    """Return the shared external data root, honoring an explicit override."""
    return Path(os.environ.get(DATA_ROOT_ENV, DEFAULT_DATA_ROOT)).expanduser()


def production_event_log_path() -> Path:
    """Resolve the canonical operational event log path."""
    configured = os.environ.get("FOUNDRY_ADMIN_LOG")
    return (
        Path(configured).expanduser() if configured else data_root() / "data" / "production.jsonl"
    )


def demo_event_log_path() -> Path:
    """Resolve the console seed/demo event log path."""
    configured = os.environ.get("FOUNDRY_EVENT_LOG")
    return Path(configured).expanduser() if configured else data_root() / "data" / "events.jsonl"


def lake_path() -> Path:
    """Resolve the Parquet lake directory."""
    configured = os.environ.get("FOUNDRY_LAKE_ROOT")
    return Path(configured).expanduser() if configured else data_root() / "data" / "lake"


def reference_path() -> Path:
    """Resolve the downloaded reference snapshot directory."""
    configured = os.environ.get("FOUNDRY_REFERENCE_ROOT")
    return Path(configured).expanduser() if configured else data_root() / "data" / "reference"


def crawl_path() -> Path:
    """Resolve the raw crawl-run directory."""
    configured = os.environ.get("FOUNDRY_CRAWL_ROOT")
    return Path(configured).expanduser() if configured else data_root() / "data" / "crawl-runs"


def postgres_data_path() -> Path:
    """Resolve the local PostgreSQL cluster directory."""
    configured = os.environ.get("FOUNDRY_POSTGRES_DATA")
    return Path(configured).expanduser() if configured else data_root() / "postgresql"
