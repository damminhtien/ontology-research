"""Bounded OSINT entity and evidence resources for analyst workflows."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from foundry.console.api.monitor import event_log_path
from foundry.console.api.projection import (
    _page_payload,
    _query_service,
    _read_model,
    _visible_assertion,
)
from foundry.console.api.provenance_access import ProvenanceScope, provenance_scope
from foundry.events import EventLog
from foundry.identity import DEFAULT_IDENTIFIER_POLICIES, IdentityService, SourceRecordIdentity
from foundry.merge import rebuild_identity
from foundry.provenance import normalize_source_id
from foundry.query_service import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_TIMEOUT_MS,
    MAX_PAGE_SIZE,
    QueryPage,
    QueryService,
)
from foundry.readmodel import ReadModelStore
from foundry.storage import identity_service_for_log

router = APIRouter(prefix="/v1", tags=["OSINT intelligence"])

_identity_cache: IdentityService | None = None
_identity_cache_key: tuple | None = None
_identity_lock = threading.Lock()


class IdentityCandidateRequest(BaseModel):
    """Incoming source identity, or a direct review of two known entities."""

    name: str | None = Field(default=None, max_length=500)
    entity_type: str | None = Field(default=None, max_length=100)
    external_namespace: str | None = Field(default=None, max_length=200)
    external_id: str | None = Field(default=None, max_length=500)
    source_id: str | None = Field(default=None, max_length=200)
    source_record_id: str | None = Field(default=None, max_length=500)
    entity_id: str | None = Field(default=None, max_length=1000)
    candidate_entity_id: str | None = Field(default=None, max_length=1000)

    @field_validator(
        "name",
        "entity_type",
        "external_namespace",
        "external_id",
        "source_id",
        "source_record_id",
        "entity_id",
        "candidate_entity_id",
        mode="before",
    )
    @classmethod
    def strip_optional_strings(cls, value: object) -> object:
        """Trim request identifiers and treat whitespace-only values as absent."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def validate_request(self) -> IdentityCandidateRequest:
        """Require either a complete entity pair or a complete incoming record."""
        pair_mode = self.entity_id is not None or self.candidate_entity_id is not None
        record_mode = any(
            value is not None
            for value in (
                self.name,
                self.entity_type,
                self.external_namespace,
                self.external_id,
                self.source_id,
                self.source_record_id,
            )
        )
        if pair_mode:
            if not self.entity_id or not self.candidate_entity_id or record_mode:
                raise ValueError("provide both entity IDs, or provide an incoming source record")
            return self
        if not self.entity_type or not self.entity_type.strip():
            raise ValueError("entity_type is required for an incoming source record")
        if not self.name and not self.external_id:
            raise ValueError("name or external_id is required")
        if bool(self.external_namespace) != bool(self.external_id):
            raise ValueError("external_namespace and external_id must be provided together")
        if bool(self.source_id) != bool(self.source_record_id):
            raise ValueError("source_id and source_record_id must be provided together")
        return self


def _identity_service() -> IdentityService:
    """Return persistent identity indexes or a fingerprint-cached log projection."""
    global _identity_cache, _identity_cache_key
    path = event_log_path()
    log = EventLog(path)
    dsn = os.environ.get("FOUNDRY_DATABASE_URL")
    if dsn:
        return identity_service_for_log(log)[0]

    key = (path.as_posix(), log.fingerprint())
    with _identity_lock:
        if _identity_cache is None or _identity_cache_key != key:
            _identity_cache = rebuild_identity(log)
            _identity_cache_key = key
        return _identity_cache


def _entity_summary(
    model: ReadModelStore,
    identity: IdentityService,
    entity_id: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> dict:
    """Resolve a possibly merged ID and return its current profile summary."""
    survivor = identity.merged_into(entity_id) or model.merged_into(entity_id) or entity_id
    with model.query_timeout(timeout_ms):
        view = model.get_entity(survivor)
        if view is None:
            raise HTTPException(status_code=404, detail=f"unknown entity {entity_id}")
        try:
            entity_type, aliases, external_ids = identity.identity(survivor)
        except KeyError:
            entity_type, aliases, external_ids = view.entity_type, frozenset(), {}
    return {
        "entity_id": view.entity_id,
        "entity_type": entity_type,
        "name": view.name,
        "first_seen": view.first_seen,
        "last_event_time": view.last_event_time,
        "aliases": sorted(aliases),
        "external_ids": {
            namespace: sorted(values) for namespace, values in sorted(external_ids.items())
        },
        "requested_entity_id": entity_id,
        "identity_status": "merged" if survivor != entity_id else "canonical",
        "merged_into": survivor if survivor != entity_id else None,
    }


def _visible_with_counts(assertion: dict, scope: ProvenanceScope) -> dict:
    """Apply source scope and disclose how many linked fields were redacted."""
    visible = _visible_assertion(assertion, scope)
    visible["redacted_source_count"] = len(assertion.get("source_ids", [])) - len(
        visible["source_ids"]
    )
    visible["redacted_evidence_count"] = len(assertion.get("evidence", [])) - len(
        visible["evidence"]
    )
    return visible


def _scope_allows_source(scope: ProvenanceScope, source_id: str) -> bool:
    """Accept both stored source IRIs and short source names in identity evidence."""
    if scope.allows(source_id):
        return True
    try:
        return scope.allows(normalize_source_id(source_id))
    except ValueError:
        return False


def _relationship_payload(assertion: dict, model: ReadModelStore, scope: ProvenanceScope) -> dict:
    """Enrich an entity-valued assertion with a bounded target profile."""
    target_id = assertion.get("object", {}).get("value")
    target = None
    if isinstance(target_id, str):
        canonical_target = model.merged_into(target_id) or target_id
        with model.query_timeout(DEFAULT_TIMEOUT_MS):
            target_view = model.get_entity(canonical_target)
        if target_view is not None:
            target = {
                "entity_id": target_view.entity_id,
                "entity_type": target_view.entity_type,
                "name": target_view.name,
                "requested_entity_id": target_id,
            }
    return {**_visible_with_counts(assertion, scope), "target": target}


def _page_response(page: QueryPage, key: str, items: list[dict]) -> dict:
    """Return one consistent bounded collection envelope."""
    return {key: items, **_page_payload(page)}


@router.get("/entities")
def search_entities(
    q: Annotated[str, Query(min_length=1, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    sort_by: Annotated[
        str,
        Query(pattern="^(entity_id|name|entity_type|first_seen|last_event_time)$"),
    ] = "name",
    descending: bool = False,
    timeout_ms: Annotated[int, Query(ge=10, le=5_000)] = DEFAULT_TIMEOUT_MS,
) -> dict:
    """Search exact primary names and indexed aliases, with bounded paging."""
    model = _read_model()
    identity = _identity_service()
    with model.query_timeout(timeout_ms):
        direct = model.get_entity(q)
    if direct is not None and offset == 0:
        page = QueryPage((direct,), 1, limit, offset, False)
    else:
        try:
            page = QueryService(model).entity_search(
                q,
                limit=limit,
                offset=offset,
                sort_by=sort_by,
                descending=descending,
                timeout_ms=timeout_ms,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "query": q,
        "entities": [
            _entity_summary(model, identity, view.entity_id, timeout_ms=timeout_ms)
            for view in page.items
        ],
        **_page_payload(page),
    }


@router.get("/entities/{entity_id}")
def get_entity(
    entity_id: str,
    timeout_ms: Annotated[int, Query(ge=10, le=5_000)] = DEFAULT_TIMEOUT_MS,
) -> dict:
    """Return one canonical profile and its current merge state."""
    return {
        "entity": _entity_summary(
            _read_model(), _identity_service(), entity_id, timeout_ms=timeout_ms
        )
    }


@router.get("/entities/{entity_id}/identifiers")
def get_entity_identifiers(
    entity_id: str,
    timeout_ms: Annotated[int, Query(ge=10, le=5_000)] = DEFAULT_TIMEOUT_MS,
) -> dict:
    """Return aliases and external IDs with their namespace uniqueness policies."""
    entity = _entity_summary(_read_model(), _identity_service(), entity_id, timeout_ms=timeout_ms)
    return {
        "entity_id": entity["entity_id"],
        "aliases": entity["aliases"],
        "external_ids": [
            {
                "namespace": namespace,
                "values": values,
                "unique_authoritative": DEFAULT_IDENTIFIER_POLICIES.get(
                    namespace.casefold()
                ).unique_authoritative
                if namespace.casefold() in DEFAULT_IDENTIFIER_POLICIES
                else False,
            }
            for namespace, values in entity["external_ids"].items()
        ],
    }


@router.get("/entities/{entity_id}/relations")
def get_entity_relations(
    entity_id: str,
    scope: Annotated[ProvenanceScope, Depends(provenance_scope)],
    at: datetime | None = None,
    depth: Annotated[int, Query(ge=1, le=3)] = 1,
    predicate_iri: Annotated[str | None, Query(max_length=2000)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    timeout_ms: Annotated[int, Query(ge=10, le=5_000)] = DEFAULT_TIMEOUT_MS,
) -> dict:
    """Return one-to-three-hop entity relations at a valid-time instant."""
    instant = at or datetime.now(UTC)
    if instant.tzinfo is None:
        raise HTTPException(status_code=400, detail="at must include a timezone")
    model = _read_model()
    _ensure_entity(model, entity_id)
    query = QueryService(model)
    try:
        if depth == 1:
            page = query.relationships_as_of(
                entity_id,
                instant,
                predicate_iri=predicate_iri,
                limit=limit,
                offset=offset,
                timeout_ms=timeout_ms,
            )
            relations = [_relationship_payload(assertion, model, scope) for assertion in page.items]
            return {
                "entity_id": entity_id,
                "at": instant.isoformat(),
                "depth": depth,
                **_page_response(page, "relations", relations),
            }

        page = query.traverse_relationships(
            entity_id,
            instant,
            max_hops=depth,
            predicate_iri=predicate_iri,
            limit=limit,
            offset=offset,
            timeout_ms=timeout_ms,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    paths = [
        {
            "nodes": list(path.nodes),
            "assertions": [
                _relationship_payload(assertion, model, scope) for assertion in path.assertions
            ],
        }
        for path in page.items
    ]
    return {
        "entity_id": entity_id,
        "at": instant.isoformat(),
        "depth": depth,
        **_page_response(page, "paths", paths),
    }


def _ensure_entity(model: ReadModelStore, entity_id: str) -> None:
    """Return 404 for an unknown entity before running a broader query."""
    canonical_id = model.merged_into(entity_id) or entity_id
    if model.get_entity(canonical_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown entity {entity_id}")


@router.get("/entities/{entity_id}/timeline")
def get_entity_timeline(
    entity_id: str,
    scope: Annotated[ProvenanceScope, Depends(provenance_scope)],
    include_superseded: bool = True,
    sort_by: Annotated[str, Query(pattern="^(recorded_at|valid_from)$")] = "recorded_at",
    descending: bool = True,
    limit: Annotated[int, Query(ge=1, le=100)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    timeout_ms: Annotated[int, Query(ge=10, le=5_000)] = DEFAULT_TIMEOUT_MS,
) -> dict:
    """Return paged assertion history with valid and recorded times."""
    _ensure_entity(_read_model(), entity_id)
    try:
        page = _query_service().entity_timeline(
            entity_id,
            include_superseded=include_superseded,
            sort_by=sort_by,
            descending=descending,
            limit=limit,
            offset=offset,
            timeout_ms=timeout_ms,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "entity_id": entity_id,
        "assertions": [_visible_with_counts(item, scope) for item in page.items],
        **_page_payload(page),
    }


@router.get("/assertions/{assertion_id}/evidence")
def get_assertion_evidence(
    assertion_id: str,
    scope: Annotated[ProvenanceScope, Depends(provenance_scope)],
    timeout_ms: Annotated[int, Query(ge=10, le=5_000)] = DEFAULT_TIMEOUT_MS,
) -> dict:
    """Open source citations and exact evidence spans for one assertion."""
    assertion = _query_service().assertion_provenance(assertion_id, timeout_ms=timeout_ms)
    if assertion is None:
        raise HTTPException(status_code=404, detail=f"unknown assertion {assertion_id}")
    return _visible_with_counts(assertion, scope)


@router.get("/entities/{entity_id}/conflicts")
def get_entity_conflicts(
    entity_id: str,
    scope: Annotated[ProvenanceScope, Depends(provenance_scope)],
    at: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    timeout_ms: Annotated[int, Query(ge=10, le=5_000)] = DEFAULT_TIMEOUT_MS,
) -> dict:
    """Return active, competing assertion values for this entity at one instant."""
    instant = at or datetime.now(UTC)
    if instant.tzinfo is None:
        raise HTTPException(status_code=400, detail="at must include a timezone")
    _ensure_entity(_read_model(), entity_id)
    try:
        page = _query_service().entity_conflicts(
            entity_id, instant, limit=limit, offset=offset, timeout_ms=timeout_ms
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    conflicts = [
        {
            **conflict,
            "alternatives": [
                {
                    "object": alternative["object"],
                    "assertions": [
                        _visible_with_counts(assertion, scope)
                        for assertion in alternative["assertions"]
                    ],
                }
                for alternative in conflict["alternatives"]
            ],
        }
        for conflict in page.items
    ]
    return {
        "entity_id": entity_id,
        "at": instant.isoformat(),
        "conflicts": conflicts,
        **_page_payload(page),
    }


@router.post("/identity/candidates")
def evaluate_identity_candidate(
    request: IdentityCandidateRequest,
    scope: Annotated[ProvenanceScope, Depends(provenance_scope)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
) -> dict:
    """Evaluate a candidate or entity pair without changing identity state."""
    identity = _identity_service()
    model = _read_model()

    if request.entity_id and request.candidate_entity_id:
        left = request.entity_id
        right = request.candidate_entity_id
        try:
            left_type, _, _ = identity.identity(left)
            right_type, _, _ = identity.identity(right)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown entity {exc.args[0]}") from exc
        left_canonical = identity.merged_into(left) or left
        right_canonical = identity.merged_into(right) or right
        if left_canonical == right_canonical:
            decision, reason, evidence = (
                "MATCH",
                "both IDs already resolve to the same canonical entity",
                None,
            )
        elif left_type != right_type:
            decision, reason, evidence = (
                "CONFLICT",
                "entity types differ",
                None,
            )
        else:
            evidence = identity.cannot_link_evidence_between_components(
                left_canonical, right_canonical
            )
            if evidence is not None:
                decision, reason = "CONFLICT", "a cannot-link constraint blocks this pair"
            else:
                decision, reason = (
                    "REVIEW",
                    "candidate similarity is not same-entity evidence; human review is required",
                )
        return {
            "decision": decision,
            "method": "entity_pair_review",
            "entity_ids": [left, right],
            "canonical_ids": [left_canonical, right_canonical],
            "reason": reason,
            "cannot_link_evidence": (
                evidence.to_dict()
                if evidence and _scope_allows_source(scope, evidence.source)
                else None
            ),
            "cannot_link_evidence_redacted": bool(
                evidence and not _scope_allows_source(scope, evidence.source)
            ),
            "same_entity_evidence_required": decision == "REVIEW",
            "writes_performed": False,
        }

    source_record = None
    if request.source_id and request.source_record_id:
        try:
            source_record = SourceRecordIdentity(request.source_id, request.source_record_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        resolution = identity.resolve(
            name=request.name.strip() if request.name else None,
            entity_type=(request.entity_type or "").strip(),
            external_source=request.external_namespace.casefold()
            if request.external_namespace
            else None,
            external_id=request.external_id.strip() if request.external_id else None,
            source_record=source_record,
            prepare_only=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    candidates = []
    for candidate_id in resolution.candidates[:limit]:
        canonical_id = identity.merged_into(candidate_id) or candidate_id
        try:
            candidates.append(_entity_summary(model, identity, canonical_id))
        except HTTPException:
            candidates.append({"entity_id": canonical_id, "identity_status": "unprojected"})
    resolution_evidence = dict(resolution.evidence or {})
    evidence_redacted = resolution_evidence.get("type") == "source_record" and not scope.allows(
        resolution_evidence.get("source_id")
    )
    if evidence_redacted:
        resolution_evidence = {}
    return {
        "decision": resolution.decision.value,
        "method": resolution.method,
        "candidate_method": resolution.candidate_method,
        "candidate_score": resolution.confidence,
        "candidate_score_semantics": "resolver score; not a probability that the facts are true",
        "candidate_total": len(resolution.candidates),
        "candidate_limit": limit,
        "candidates_truncated": len(resolution.candidates) > limit,
        "canonical_id": resolution.canonical_id or None,
        "reason": resolution.reason,
        "evidence": resolution_evidence,
        "evidence_redacted": evidence_redacted,
        "candidates": candidates,
        "same_entity_evidence_required": resolution.decision.value == "REVIEW",
        "writes_performed": False,
    }
