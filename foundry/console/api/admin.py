"""Write-capable admin API for the console (Phase 5 completion).

Every mutation is **gated and audited**:

- **Auth**: ``Authorization: Bearer <token>`` compared constant-time against
  ``$FOUNDRY_CONSOLE_TOKEN``. When the variable is unset the whole admin
  surface is disabled (503) — the console can never gain write access by
  accident.
- **Audit trail**: every accepted mutation appends its canonical event
  (``EntityMerged`` / ``EntitySplit`` / ``ExternalIdBound``) to the production
  event log with the acting principal recorded in the payload ``actor``
  field — the append-only log *is* the audit trail (ADR-0002).
- **Log discipline**: admin operates on the production log
  (``$FOUNDRY_ADMIN_LOG``, default ``data/production.jsonl``) — never the
  seed demo log — and the identity registry is rebuilt from that log before
  any mutation, so corrections validate against real canonical state.
"""

from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (REPO_ROOT / "tools", REPO_ROOT):
    if _path.as_posix() not in sys.path:
        sys.path.insert(0, _path.as_posix())

from foundry.events import EVENT_TYPE_REVIEW_QUEUED, EventLog  # noqa: E402
from foundry.lake import default_lake_root, persist_events  # noqa: E402
from foundry.merge import merge_entities, rebuild_identity, split_entities  # noqa: E402

router = APIRouter(prefix="/admin", tags=["admin"])

TOKEN_ENV = "FOUNDRY_CONSOLE_TOKEN"
LOG_ENV = "FOUNDRY_ADMIN_LOG"
DEFAULT_LOG = "data/production.jsonl"


def _admin_log_path() -> Path:
    return Path(os.environ.get(LOG_ENV, DEFAULT_LOG))


def _check_token(authorization: str | None) -> str:
    expected = os.environ.get(TOKEN_ENV)
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=f"admin API disabled: {TOKEN_ENV} is not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="admin token required")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")
    return supplied


class CorrectionRequest(BaseModel):
    """Body for merge/split corrections."""

    survivor_id: str
    duplicate_id: str
    reason: str = ""
    actor: str = "console"


def _persist_to_lake(events: list) -> int:
    root = default_lake_root()
    root.mkdir(parents=True, exist_ok=True)
    files = persist_events(events, root)
    return sum(f.rows for f in files)


@router.post("/review")
def list_review_queue(authorization: Annotated[str | None, Header()] = None) -> dict:
    """Drain view: every ``ResolutionReviewQueued`` fact in the admin log.

    POST (not GET) because the list is token-gated — bearer credentials never
    travel in a cacheable URL fetch.
    """
    _check_token(authorization)
    log = EventLog(_admin_log_path())
    queued = [
        {"event_id": event.event_id, "occurred_at": event.occurred_at, **event.payload}
        for event in log.read_all()
        if event.event_type == EVENT_TYPE_REVIEW_QUEUED
    ]
    return {"count": len(queued), "items": queued}


@router.post("/merge")
def admin_merge(
    request: CorrectionRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """Merge a duplicate entity into its survivor (audited on the log)."""
    _check_token(authorization)
    log = EventLog(_admin_log_path())
    identity = rebuild_identity(log)
    try:
        result = merge_entities(
            identity=identity,
            log=log,
            survivor_id=request.survivor_id,
            duplicate_id=request.duplicate_id,
            reason=request.reason,
            actor=request.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rows = _persist_to_lake([result.event])
    return {
        "status": "merged",
        "event_id": result.event.event_id,
        "actor": request.actor,
        "moved_aliases": list(result.moved_aliases),
        "moved_external_ids": [
            {"source": s, "external_id": e} for s, e in result.moved_external_ids
        ],
        "lake_rows": rows,
    }


@router.post("/split")
def admin_split(
    request: CorrectionRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """Undo a previously recorded merge (audited on the log)."""
    _check_token(authorization)
    log = EventLog(_admin_log_path())
    identity = rebuild_identity(log)
    try:
        result = split_entities(
            identity=identity,
            log=log,
            survivor_id=request.survivor_id,
            duplicate_id=request.duplicate_id,
            reason=request.reason,
            actor=request.actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    rows = _persist_to_lake([result.event])
    return {
        "status": "split",
        "event_id": result.event.event_id,
        "actor": request.actor,
        "restored_aliases": list(result.restored_aliases),
        "retained_aliases": list(result.retained_aliases),
        "lake_rows": rows,
    }


@router.post("/backfill-external-ids")
def admin_backfill(authorization: Annotated[str | None, Header()] = None) -> dict:
    """Re-derive missing Wikidata bindings (idempotent, audited on the log)."""
    _check_token(authorization)
    import backfill_external_ids as backfill

    log = EventLog(_admin_log_path())
    events = backfill.collect_missing_binding_events(log)
    if events:
        log.extend(events)
        rows = _persist_to_lake(events)
    else:
        rows = 0
    return {"status": "backfilled", "appended": len(events), "lake_rows": rows}
