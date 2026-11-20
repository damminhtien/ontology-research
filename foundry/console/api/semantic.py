"""Constrained natural-language planning for registered semantic queries."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from rdflib import Dataset

from foundry.console.api.provenance_access import ProvenanceScope, provenance_scope
from foundry.semantic_query import (
    IntentSelector,
    KeywordIntentSelector,
    SemanticQueryService,
    load_vietnam_profile,
)

router = APIRouter(prefix="/v1/semantic", tags=["semantic queries"])

_graph_cache: Dataset | None = None
_graph_fingerprint: tuple[tuple[str, int, int], ...] | None = None
_graph_lock = threading.Lock()
_RDF_FORMATS = {
    ".n3": "n3",
    ".nt": "nt",
    ".nq": "nquads",
    ".trig": "trig",
    ".ttl": "turtle",
}


class SemanticQueryRequest(BaseModel):
    """Question request; raw SPARQL fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2_000)
    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100_000)


def _dataset_paths() -> tuple[Path, ...]:
    configured = os.environ.get("FOUNDRY_SEMANTIC_QUERY_DATA")
    if not configured:
        raise HTTPException(
            status_code=503,
            detail=(
                "Semantic RDF data is not configured. Set FOUNDRY_SEMANTIC_QUERY_DATA "
                "to an RDF file or directory; operational PostgreSQL queries are separate."
            ),
        )
    root = Path(configured).expanduser().resolve()
    if root.is_file():
        paths = [root]
    elif root.is_dir():
        paths = sorted(
            path.resolve()
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in _RDF_FORMATS
        )
    else:
        raise HTTPException(status_code=503, detail="Configured semantic RDF path is missing.")
    if not paths:
        raise HTTPException(
            status_code=503,
            detail="Configured path contains no supported RDF files.",
        )
    unsupported = [path.name for path in paths if path.suffix.lower() not in _RDF_FORMATS]
    if unsupported:
        raise HTTPException(
            status_code=415,
            detail="Semantic RDF path must use .ttl, .trig, .n3, .nt, or .nq.",
        )
    return tuple(paths)


def _semantic_graph() -> Dataset:
    """Load and cache only an explicitly configured optional RDF projection."""
    global _graph_cache, _graph_fingerprint
    paths = _dataset_paths()
    fingerprint = tuple(
        (path.as_posix(), path.stat().st_size, path.stat().st_mtime_ns) for path in paths
    )
    with _graph_lock:
        if _graph_cache is not None and _graph_fingerprint == fingerprint:
            return _graph_cache
        graph = Dataset(default_union=True)
        try:
            for path in paths:
                graph.parse(path.as_posix(), format=_RDF_FORMATS[path.suffix.lower()])
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Could not load semantic RDF data: {exc}",
            ) from exc
        _graph_cache = graph
        _graph_fingerprint = fingerprint
        return graph


def _selector(request: Request) -> IntentSelector:
    selector = getattr(request.app.state, "semantic_intent_selector", None)
    return selector if selector is not None else KeywordIntentSelector()


def reset_cache() -> None:
    """Clear the graph cache for tests and local dataset replacement."""
    global _graph_cache, _graph_fingerprint
    with _graph_lock:
        _graph_cache = None
        _graph_fingerprint = None


@router.get("/profile")
def get_profile() -> dict:
    """Return profile metadata without loading the optional RDF projection."""
    profile = load_vietnam_profile()
    return {
        "profile_id": profile["profile_id"],
        "name": profile["name"],
        "preferred_languages": profile["preferred_languages"],
        "area_of_interest": profile["area_of_interest"],
        "entity_type_mappings": profile["entity_type_mappings"],
        "relationship_mappings": profile["relationship_mappings"],
    }


@router.get("/templates")
def get_templates(request: Request) -> dict:
    """List registered templates and their example phrasings."""
    selector = _selector(request)
    service = SemanticQueryService(Dataset(), selector=selector)
    profile = load_vietnam_profile()
    return {
        "profile_id": profile["profile_id"],
        "planner": getattr(selector, "name", "custom"),
        "templates": service.template_catalog(),
    }


@router.post("/query")
def ask_semantic_question(
    request: Request,
    payload: SemanticQueryRequest,
    scope: Annotated[ProvenanceScope, Depends(provenance_scope)],
) -> dict:
    """Run one bounded CQ selected from the profile's allowlisted catalog."""
    try:
        service = SemanticQueryService(_semantic_graph(), selector=_selector(request))
        return service.query(
            payload.question,
            limit=payload.limit,
            offset=payload.offset,
            source_scope=scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
