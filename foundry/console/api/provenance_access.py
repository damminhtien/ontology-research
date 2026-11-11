"""Source-scoped access for provenance fields in read API responses."""

from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, HTTPException

TOKEN_SCOPES_ENV = "FOUNDRY_PROVENANCE_TOKENS"


@dataclass(frozen=True)
class ProvenanceScope:
    """The source identifiers visible to one query API caller."""

    source_ids: frozenset[str] = frozenset()
    all_sources: bool = False

    def allows(self, source_id: object) -> bool:
        """Return whether this scope permits one source identifier."""
        return isinstance(source_id, str) and (self.all_sources or source_id in self.source_ids)


def _configuration_error() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=(
            f"{TOKEN_SCOPES_ENV} must be a JSON object mapping bearer tokens "
            "to arrays of source IDs"
        ),
    )


def _configured_scopes() -> dict[str, ProvenanceScope]:
    raw = os.environ.get(TOKEN_SCOPES_ENV)
    if raw is None or not raw.strip():
        return {}
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _configuration_error() from exc
    if not isinstance(configured, dict):
        raise _configuration_error()

    scopes: dict[str, ProvenanceScope] = {}
    for token, source_ids in configured.items():
        if (
            not isinstance(token, str)
            or not token.strip()
            or not isinstance(source_ids, list)
            or any(
                not isinstance(source_id, str) or not source_id.strip() for source_id in source_ids
            )
        ):
            raise _configuration_error()
        if "*" in source_ids and len(source_ids) != 1:
            raise _configuration_error()
        scopes[token] = ProvenanceScope(
            source_ids=frozenset(source_ids) if "*" not in source_ids else frozenset(),
            all_sources="*" in source_ids,
        )
    return scopes


def provenance_scope(
    authorization: Annotated[str | None, Header()] = None,
) -> ProvenanceScope:
    """Resolve an optional bearer token; anonymous callers see no sources."""
    scopes = _configured_scopes()
    if authorization is None:
        return ProvenanceScope()

    scheme, separator, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not supplied.strip():
        raise HTTPException(status_code=401, detail="valid provenance bearer token required")

    supplied = supplied.strip()
    matched_scope = None
    for configured_token, scope in scopes.items():
        if hmac.compare_digest(supplied.encode("utf-8"), configured_token.encode("utf-8")):
            matched_scope = scope
    if matched_scope is None:
        raise HTTPException(status_code=401, detail="invalid provenance token")
    return matched_scope


def visible_source_ids(source_ids: object, scope: ProvenanceScope) -> list[str]:
    """Return only source IDs granted to this caller, preserving input order."""
    if not isinstance(source_ids, (list, tuple)):
        return []
    return [source_id for source_id in source_ids if scope.allows(source_id)]
