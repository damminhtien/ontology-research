"""Bearer-token-controlled crawl configuration and run monitoring."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from foundry.console.api.admin import _check_token
from foundry.console.crawl_control import CrawlRunManager, get_crawl_manager

router = APIRouter(prefix="/crawler", tags=["crawler"])


class CrawlConfigRequest(BaseModel):
    """Persist source selection and resource limits for the next run."""

    source_id: str
    max_records: int = Field(default=200, ge=1, le=1000)
    duration_seconds: int = Field(default=15, ge=1, le=300)


def _manager() -> CrawlRunManager:
    return get_crawl_manager()


@router.get("/state")
def crawler_state() -> dict:
    """List sources, saved settings and recent local crawl runs."""
    return _manager().state()


@router.put("/config")
def save_crawler_config(
    request: CrawlConfigRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """Save crawl settings after validating the source allowlist and limits."""
    _check_token(authorization)
    try:
        config = _manager().save_config(
            source_id=request.source_id,
            max_records=request.max_records,
            duration_seconds=request.duration_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "saved", "config": config}


@router.post("/runs")
def start_crawler_run(authorization: Annotated[str | None, Header()] = None) -> dict:
    """Launch the configured source through the existing ingestion CLI."""
    _check_token(authorization)
    try:
        return _manager().start_run()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        detail = str(exc)
        code = 409 if "already active" in detail else 503
        raise HTTPException(status_code=code, detail=detail) from exc


@router.get("/runs/{run_id}")
def crawler_run(run_id: str) -> dict:
    """Return the public status and filtered summary for one run."""
    run = _manager().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="crawl run not found")
    return run


@router.get("/runs/{run_id}/logs")
def crawler_run_logs(
    run_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """Return a bounded run log after checking the console bearer token."""
    _check_token(authorization)
    try:
        logs = _manager().logs(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="crawl run not found") from exc
    return {"run_id": run_id, "logs": logs}


@router.post("/runs/{run_id}/stop")
def stop_crawler_run(
    run_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """Request termination of an active crawl after checking the token."""
    _check_token(authorization)
    run = _manager().stop_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="crawl run not found")
    if run["status"] not in {"stopping", "stopped"}:
        raise HTTPException(status_code=409, detail=f"crawl run is {run['status']}")
    return run
