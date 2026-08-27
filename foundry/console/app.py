"""Ontology Console - FastAPI application factory.

Read-only admin console over the existing, tested tooling: API handlers call
functions from ``tools/`` and ``foundry/`` directly, so the console is a
projection of the same logic the CLI exposes (no business logic duplication).

Run locally (binds localhost; no auth by design in v1):

    .venv/bin/python -m uvicorn foundry.console.app:app --port 8787
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Console reuses tool modules that live outside the package.
for _path in (REPO_ROOT / "tools", REPO_ROOT):
    if _path.as_posix() not in sys.path:
        sys.path.insert(0, _path.as_posix())

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from foundry.console.api import (  # noqa: E402
    impact,
    monitor,
    ontology,
    overview,
    projection,
    registry,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    """Build the console application with all API routers and the SPA."""
    application = FastAPI(
        title="Ontology Console",
        version="0.1.0",
        description="Read-only administration and monitoring for the semantic platform.",
    )
    for router in (
        overview.router,
        ontology.router,
        registry.router,
        impact.router,
        projection.router,
        monitor.router,
    ):
        application.include_router(router, prefix="/api")

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """Serve the single-page app shell."""
        return FileResponse(STATIC_DIR / "index.html")

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return application


app = create_app()
