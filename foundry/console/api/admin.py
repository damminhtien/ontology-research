"""Write-capable admin API for the console (Phase 5 completion).

Every mutation is **gated and audited**:

- **Auth**: bearer credentials are compared constant-time against
  ``$FOUNDRY_CONSOLE_TOKEN``. The local UI may use a same-origin HttpOnly
  session cookie; if the variable is unset the admin surface is disabled (503).
- **Audit trail**: every accepted mutation appends its canonical event
  (``EntityMerged`` / ``EntitySplit`` / ``ExternalIdBound``) to the production
  event log. Merge and split events record the requested actor in their
  payload; the append-only log *is* the audit trail (ADR-0002).
- **Log discipline**: admin operates on the production log
  (``$FOUNDRY_ADMIN_LOG``, default ``~/Data/ontology-research/data/production.jsonl``) — never the
  seed demo log — and identity commands require a projection caught up with the
  log, so corrections validate against real canonical state.
"""

from __future__ import annotations

import hmac
import os
import sys
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Cookie, Header, HTTPException, Request
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (REPO_ROOT / "tools", REPO_ROOT):
    if _path.as_posix() not in sys.path:
        sys.path.insert(0, _path.as_posix())

from foundry.console.local_auth import (  # noqa: E402
    CONSOLE_SESSION_COOKIE,
    is_local_same_origin_request,
)
from foundry.events import EVENT_TYPE_REVIEW_QUEUED, EventLog  # noqa: E402
from foundry.identity import IdentityEvidence, IdentityService  # noqa: E402
from foundry.lake import default_lake_root, persist_events  # noqa: E402
from foundry.merge import merge_entities, split_entities  # noqa: E402
from foundry.paths import production_event_log_path  # noqa: E402
from foundry.storage import identity_service_for_log  # noqa: E402

router = APIRouter(prefix="/admin", tags=["admin"])

TOKEN_ENV = "FOUNDRY_CONSOLE_TOKEN"
LOG_ENV = "FOUNDRY_ADMIN_LOG"
DEFAULT_LOG = production_event_log_path()


def _admin_log_path() -> Path:
    return Path(os.environ.get(LOG_ENV, str(DEFAULT_LOG)))


def _admin_identity(log: EventLog) -> tuple[IdentityService, bool]:
    """Use persistent identity indexes when configured; worker applies events."""
    return identity_service_for_log(log)


def _check_token(
    authorization: str | None,
    session_token: str | None = None,
    request: Request | None = None,
) -> str:
    expected = os.environ.get(TOKEN_ENV)
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=f"admin API disabled: {TOKEN_ENV} is not configured",
        )
    if authorization:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="admin token required")
        supplied = authorization.removeprefix("Bearer ").strip()
    elif session_token:
        if (
            request is not None
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not is_local_same_origin_request(request)
        ):
            raise HTTPException(status_code=403, detail="local same-origin request required")
        supplied = session_token
    else:
        raise HTTPException(status_code=401, detail="admin token required")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")
    return supplied


class CorrectionRequest(BaseModel):
    """Body for merge/split corrections."""

    survivor_id: str
    duplicate_id: str
    reason: str = ""
    actor: str = "console"
    same_entity_evidence: dict[str, str] | None = None


def _persist_to_lake(events: list) -> int:
    root = default_lake_root()
    root.mkdir(parents=True, exist_ok=True)
    files = persist_events(events, root)
    return sum(f.rows for f in files)


@router.post("/review")
def list_review_queue(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    session_token: Annotated[str | None, Cookie(alias=CONSOLE_SESSION_COOKIE)] = None,
) -> dict:
    """List every ``ResolutionReviewQueued`` fact in the admin log.

    This is a read-only view; it does not remove events from the append-only log.
    POST (not GET) because the list is token-gated — bearer credentials never
    travel in a cacheable URL fetch.
    """
    _check_token(authorization, session_token, request)
    log = EventLog(_admin_log_path())
    queued = []
    for event in log.iter_all():
        if event.event_type == EVENT_TYPE_REVIEW_QUEUED:
            queued.append(
                {"event_id": event.event_id, "occurred_at": event.occurred_at, **event.payload}
            )
    return {"count": len(queued), "items": queued}


@router.post("/merge")
def admin_merge(
    request: CorrectionRequest,
    http_request: Request,
    authorization: Annotated[str | None, Header()] = None,
    session_token: Annotated[str | None, Cookie(alias=CONSOLE_SESSION_COOKIE)] = None,
) -> dict:
    """Merge a duplicate entity into its survivor (audited on the log)."""
    _check_token(authorization, session_token, http_request)
    log = EventLog(_admin_log_path())
    identity, apply_projection = _admin_identity(log)
    if request.same_entity_evidence is None:
        raise HTTPException(status_code=422, detail="same-entity evidence is required")
    try:
        evidence = IdentityEvidence.from_dict(request.same_entity_evidence)
        result = merge_entities(
            identity=identity,
            log=log,
            survivor_id=request.survivor_id,
            duplicate_id=request.duplicate_id,
            same_entity_evidence=evidence,
            reason=request.reason,
            actor=request.actor,
            apply_projection=apply_projection,
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
    http_request: Request,
    authorization: Annotated[str | None, Header()] = None,
    session_token: Annotated[str | None, Cookie(alias=CONSOLE_SESSION_COOKIE)] = None,
) -> dict:
    """Undo a previously recorded merge (audited on the log)."""
    _check_token(authorization, session_token, http_request)
    log = EventLog(_admin_log_path())
    identity, apply_projection = _admin_identity(log)
    try:
        result = split_entities(
            identity=identity,
            log=log,
            survivor_id=request.survivor_id,
            duplicate_id=request.duplicate_id,
            reason=request.reason,
            actor=request.actor,
            apply_projection=apply_projection,
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
def admin_backfill(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    session_token: Annotated[str | None, Cookie(alias=CONSOLE_SESSION_COOKIE)] = None,
) -> dict:
    """Re-derive missing Wikidata bindings (idempotent, audited on the log)."""
    _check_token(authorization, session_token, request)
    import backfill_external_ids as backfill

    log = EventLog(_admin_log_path())
    events = backfill.collect_missing_binding_events(log)
    if events:
        log.extend(events)
        rows = _persist_to_lake(events)
    else:
        rows = 0
    return {"status": "backfilled", "appended": len(events), "lake_rows": rows}
