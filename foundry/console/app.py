"""Ontology Console - FastAPI application factory.

Console over the existing, tested tooling: read views use the ontology,
registry, event log and read model, while token-gated admin routes append
corrections to the production log. Query API provenance is source-scoped by
``FOUNDRY_PROVENANCE_TOKENS``; admin access uses a separate token.

Run locally (Uvicorn binds to localhost by default). Set
``FOUNDRY_CONSOLE_TOKEN`` to enable admin operations:

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
    admin,
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
        description=(
            "Read-only monitoring and token-gated, audited corrections for the semantic platform."
        ),
    )
    for router in (
        overview.router,
        ontology.router,
        registry.router,
        impact.router,
        projection.router,
        monitor.router,
        admin.router,
    ):
        application.include_router(router, prefix="/api")

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """Serve the single-page app shell."""
        return FileResponse(STATIC_DIR / "index.html")

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return application


app = create_app()
