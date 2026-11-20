"""Ontology Console - FastAPI application factory.

Console over the existing, tested tooling: read views use the ontology,
registry, event log and read model, while token-gated admin routes append
corrections to the production log. Query API provenance is source-scoped by
``FOUNDRY_PROVENANCE_TOKENS``; admin access uses a separate token.

Run locally with ``make console``. The local launcher persists a private
``FOUNDRY_CONSOLE_TOKEN`` outside the repository and serves only on loopback:

    make console
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Console reuses tool modules that live outside the package.
for _path in (REPO_ROOT / "tools", REPO_ROOT):
    if _path.as_posix() not in sys.path:
        sys.path.insert(0, _path.as_posix())

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from foundry.console.api import (  # noqa: E402
    admin,
    crawler,
    impact,
    intelligence,
    monitor,
    ontology,
    overview,
    projection,
    registry,
    semantic,
)
from foundry.console.crawl_control import shutdown_crawl_manager  # noqa: E402
from foundry.query_service import QueryTimeoutError  # noqa: E402
from foundry.semantic_query import IntentSelector, KeywordIntentSelector  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(*, semantic_intent_selector: IntentSelector | None = None) -> FastAPI:
    """Build the console application with optional constrained intent planning."""

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        """Stop crawl child processes when the console lifespan ends."""
        try:
            yield
        finally:
            shutdown_crawl_manager()

    application = FastAPI(
        title="Ontology Console",
        version="0.1.0",
        description=(
            "Read-only monitoring and token-gated, audited corrections for the semantic platform."
        ),
        lifespan=lifespan,
    )
    application.state.semantic_intent_selector = semantic_intent_selector or KeywordIntentSelector()

    @application.exception_handler(QueryTimeoutError)
    async def query_timeout_handler(
        _request: Request, _exception: QueryTimeoutError
    ) -> JSONResponse:
        """Return a gateway-timeout response for canceled operational queries."""
        return JSONResponse(status_code=504, content={"detail": "operational query timed out"})

    for router in (
        overview.router,
        ontology.router,
        registry.router,
        semantic.router,
        impact.router,
        projection.router,
        monitor.router,
        crawler.router,
        intelligence.router,
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
