"""Bounded operational query contract over interchangeable read adapters."""

from __future__ import annotations

import json
from collections import deque
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from foundry.namespaces import require_absolute_iri

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_PAGE_OFFSET = 100_000
DEFAULT_TIMEOUT_MS = 250
MIN_TIMEOUT_MS = 10
MAX_TIMEOUT_MS = 5_000
MAX_TRAVERSAL_HOPS = 3
MAX_TRAVERSAL_NODES = 500
MAX_TRAVERSAL_EDGES = 2_000
MAX_CONTRADICTION_SCAN = 1_000
ENTITY_SORT_KEYS = frozenset({"entity_id", "name", "entity_type", "first_seen", "last_event_time"})


class QueryTimeoutError(TimeoutError):
    """An adapter canceled an operational query at its configured deadline."""


@dataclass(frozen=True)
class QueryPage[T]:
    """A bounded, deterministic page of operational query results."""

    items: tuple[T, ...]
    total: int
    limit: int
    offset: int
    has_more: bool
    truncated: bool = False


class OperationalQueryAdapter(Protocol):
    """Read operations required by QueryService, independent of storage code."""

    def query_timeout(self, timeout_ms: int) -> AbstractContextManager[object]:
        """Apply a transaction-local deadline to the operation."""
        ...

    def get_entity(self, entity_id: str):
        """Return one canonical entity by its stable identifier."""
        ...

    def find_entities_page(
        self,
        name: str,
        *,
        limit: int,
        offset: int,
        sort_by: str,
        descending: bool,
    ) -> QueryPage[object]:
        """Find normalized primary-name or alias candidates."""
        ...

    def current_location(self, entity_id: str):
        """Return a unique location valid at the current instant."""
        ...

    def location_as_of(self, entity_id: str, instant: datetime):
        """Return a unique location valid at the supplied instant."""
        ...

    def location_resolution(self, entity_id: str, instant: datetime):
        """Return none, one value, or all conflicting location values."""
        ...

    def assertions_as_of_page(
        self,
        subject_id: str,
        instant: datetime,
        *,
        predicate_iri: str | None,
        object_kind: str | None,
        limit: int,
        offset: int = 0,
    ) -> QueryPage[dict]:
        """Return active assertion rows for one subject and valid-time instant."""
        ...

    def get_assertion(self, assertion_id: str) -> dict | None:
        """Return one assertion and its provenance fields."""
        ...

    def assertion_history_page(
        self,
        subject_id: str,
        *,
        include_superseded: bool,
        limit: int,
        offset: int,
        sort_by: str,
        descending: bool,
    ) -> QueryPage[dict]:
        """Return a bounded, stable page of valid-time and recorded history."""
        ...

    def entities_at_page(
        self, location_uri: str, instant: datetime, *, limit: int, offset: int
    ) -> QueryPage[str]:
        """Return entities uniquely at the location and instant."""
        ...

    def get_track(self, track_id: str) -> dict | None:
        """Return one track and its lineage references."""
        ...

    def tracks_page(self, entity_id: str, *, limit: int, offset: int) -> QueryPage[dict]:
        """Return a bounded page of tracks for an entity."""
        ...


@dataclass(frozen=True)
class RelationshipPath:
    """One simple path through active entity-valued assertions."""

    nodes: tuple[str, ...]
    assertions: tuple[dict, ...]

    @property
    def hops(self) -> int:
        """Number of assertion edges in the path."""
        return len(self.assertions)


class QueryService:
    """Validated operational query surface shared by API and read adapters."""

    def __init__(self, adapter: OperationalQueryAdapter) -> None:
        """Bind one operational read adapter to this query facade."""
        self._adapter = adapter

    def entity_lookup(self, entity_id: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        """Return one canonical entity by stable identifier."""
        timeout = _validate_timeout(timeout_ms)
        with self._adapter.query_timeout(timeout):
            return self._adapter.get_entity(entity_id)

    def current_location(self, entity_id: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        """Return a unique current location, if one exists."""
        timeout = _validate_timeout(timeout_ms)
        with self._adapter.query_timeout(timeout):
            return self._adapter.current_location(entity_id)

    def location_as_of(
        self, entity_id: str, instant: datetime, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ):
        """Return a unique location active at a half-open valid-time instant."""
        instant = _validate_instant(instant)
        timeout = _validate_timeout(timeout_ms)
        with self._adapter.query_timeout(timeout):
            return self._adapter.location_as_of(entity_id, instant)

    def location_resolution(
        self, entity_id: str, instant: datetime, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ):
        """Return none, a unique location, or all conflicting location values."""
        instant = _validate_instant(instant)
        timeout = _validate_timeout(timeout_ms)
        with self._adapter.query_timeout(timeout):
            return self._adapter.location_resolution(entity_id, instant)

    def entity_search(
        self,
        name: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        sort_by: str = "entity_id",
        descending: bool = False,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> QueryPage[object]:
        """Find exact normalized primary-name or alias candidates."""
        page_limit, page_offset = _validate_page(limit, offset)
        if sort_by not in ENTITY_SORT_KEYS:
            raise ValueError(f"sort_by must be one of {sorted(ENTITY_SORT_KEYS)}")
        if not isinstance(descending, bool):
            raise ValueError("descending must be a boolean")
        timeout = _validate_timeout(timeout_ms)
        with self._adapter.query_timeout(timeout):
            return self._adapter.find_entities_page(
                name,
                limit=page_limit,
                offset=page_offset,
                sort_by=sort_by,
                descending=descending,
            )

    def relationships_as_of(
        self,
        subject_id: str,
        instant: datetime,
        *,
        predicate_iri: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> QueryPage[dict]:
        """Return entity-valued assertions active at a half-open valid time."""
        instant = _validate_instant(instant)
        page_limit, page_offset = _validate_page(limit, offset)
        predicate = _validate_predicate(predicate_iri)
        timeout = _validate_timeout(timeout_ms)
        with self._adapter.query_timeout(timeout):
            page = self._adapter.assertions_as_of_page(
                subject_id,
                instant,
                predicate_iri=predicate,
                object_kind="entity",
                limit=page_limit,
                offset=page_offset,
            )
        return page

    def traverse_relationships(
        self,
        subject_id: str,
        instant: datetime,
        *,
        max_hops: int = 1,
        predicate_iri: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> QueryPage[RelationshipPath]:
        """Breadth-first traversal with explicit hop, edge and node ceilings."""
        instant = _validate_instant(instant)
        page_limit, page_offset = _validate_page(limit, offset)
        if not isinstance(max_hops, int) or isinstance(max_hops, bool):
            raise ValueError("max_hops must be an integer from 1 to 3")
        if not 1 <= max_hops <= MAX_TRAVERSAL_HOPS:
            raise ValueError("max_hops must be from 1 to 3")
        predicate = _validate_predicate(predicate_iri)
        timeout = _validate_timeout(timeout_ms)

        paths: list[RelationshipPath] = []
        pending = deque([(subject_id, (subject_id,), ())])
        expanded_nodes = {subject_id}
        expanded_edges = 0
        truncated = False
        with self._adapter.query_timeout(timeout):
            for _depth in range(max_hops):
                next_pending = deque()
                while pending:
                    current_id, nodes, assertions = pending.popleft()
                    remaining_edges = MAX_TRAVERSAL_EDGES - expanded_edges
                    if remaining_edges <= 0:
                        truncated = True
                        break
                    edge_limit = min(MAX_PAGE_SIZE, remaining_edges + 1)
                    edge_page = self._adapter.assertions_as_of_page(
                        current_id,
                        instant,
                        predicate_iri=predicate,
                        object_kind="entity",
                        limit=edge_limit,
                    )
                    if edge_page.has_more or edge_page.truncated:
                        truncated = True
                    for assertion in edge_page.items[:remaining_edges]:
                        expanded_edges += 1
                        target_id = assertion.get("object", {}).get("value")
                        if not isinstance(target_id, str) or target_id in nodes:
                            continue
                        path = RelationshipPath(
                            nodes=(*nodes, target_id),
                            assertions=(*assertions, assertion),
                        )
                        paths.append(path)
                        if path.hops < max_hops:
                            if target_id in expanded_nodes:
                                continue
                            if len(expanded_nodes) >= MAX_TRAVERSAL_NODES:
                                truncated = True
                                continue
                            expanded_nodes.add(target_id)
                            next_pending.append((target_id, path.nodes, path.assertions))
                    if expanded_edges >= MAX_TRAVERSAL_EDGES:
                        truncated = truncated or bool(pending) or bool(next_pending)
                        break
                pending = next_pending
                if not pending or expanded_edges >= MAX_TRAVERSAL_EDGES:
                    break

        paths.sort(
            key=lambda path: (
                path.hops,
                path.nodes,
                tuple(a["assertion_id"] for a in path.assertions),
            )
        )
        page_items = tuple(paths[page_offset : page_offset + page_limit])
        has_more = truncated or page_offset + page_limit < len(paths)
        return QueryPage(
            items=page_items,
            total=len(paths),
            limit=page_limit,
            offset=page_offset,
            has_more=has_more,
            truncated=truncated,
        )

    def assertion_provenance(
        self, assertion_id: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> dict | None:
        """Return an assertion with source identifiers and evidence locators."""
        timeout = _validate_timeout(timeout_ms)
        with self._adapter.query_timeout(timeout):
            return self._adapter.get_assertion(assertion_id)

    def entity_timeline(
        self,
        subject_id: str,
        *,
        include_superseded: bool = True,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        sort_by: str = "recorded_at",
        descending: bool = True,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> QueryPage[dict]:
        """Return a bounded subject history ordered by recorded or valid time."""
        page_limit, page_offset = _validate_page(limit, offset)
        if sort_by not in {"recorded_at", "valid_from"}:
            raise ValueError("sort_by must be 'recorded_at' or 'valid_from'")
        if not isinstance(include_superseded, bool) or not isinstance(descending, bool):
            raise ValueError("timeline flags must be booleans")
        timeout = _validate_timeout(timeout_ms)
        with self._adapter.query_timeout(timeout):
            return self._adapter.assertion_history_page(
                subject_id,
                include_superseded=include_superseded,
                limit=page_limit,
                offset=page_offset,
                sort_by=sort_by,
                descending=descending,
            )

    def entity_conflicts(
        self,
        subject_id: str,
        instant: datetime,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> QueryPage[dict]:
        """Find predicates with competing active object values for one subject."""
        instant = _validate_instant(instant)
        page_limit, page_offset = _validate_page(limit, offset)
        timeout = _validate_timeout(timeout_ms)
        with self._adapter.query_timeout(timeout):
            source_page = self._adapter.assertions_as_of_page(
                subject_id,
                instant,
                predicate_iri=None,
                object_kind=None,
                limit=MAX_CONTRADICTION_SCAN + 1,
            )

        grouped: dict[str, dict[str, list[dict]]] = {}
        for assertion in source_page.items[:MAX_CONTRADICTION_SCAN]:
            predicate = assertion["predicate_iri"]
            object_key = json.dumps(assertion["object"], sort_keys=True, ensure_ascii=False)
            grouped.setdefault(predicate, {}).setdefault(object_key, []).append(assertion)

        conflicts = []
        for predicate, alternatives in sorted(grouped.items()):
            if len(alternatives) < 2:
                continue
            conflicts.append(
                {
                    "subject_id": subject_id,
                    "predicate_iri": predicate,
                    "valid_at": _format_instant(instant),
                    "review_status": "unreviewed",
                    "review_status_source": "derived_from_active_assertions",
                    "alternatives": [
                        {
                            "object": values[0]["object"],
                            "assertions": sorted(values, key=lambda item: item["assertion_id"]),
                        }
                        for _, values in sorted(alternatives.items())
                    ],
                }
            )

        truncated = (
            len(source_page.items) > MAX_CONTRADICTION_SCAN
            or source_page.has_more
            or source_page.truncated
        )
        items = tuple(conflicts[page_offset : page_offset + page_limit])
        return QueryPage(
            items=items,
            total=len(conflicts),
            limit=page_limit,
            offset=page_offset,
            has_more=truncated or page_offset + len(items) < len(conflicts),
            truncated=truncated,
        )

    def contradictions(
        self,
        subject_id: str,
        predicate_iri: str,
        instant: datetime,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> QueryPage[dict]:
        """Return distinct competing object values active at one instant."""
        instant = _validate_instant(instant)
        page_limit, page_offset = _validate_page(limit, offset)
        predicate = require_absolute_iri(predicate_iri, "predicate_iri")
        timeout = _validate_timeout(timeout_ms)
        with self._adapter.query_timeout(timeout):
            source_page = self._adapter.assertions_as_of_page(
                subject_id,
                instant,
                predicate_iri=predicate,
                object_kind=None,
                limit=MAX_CONTRADICTION_SCAN + 1,
            )
        grouped: dict[str, list[dict]] = {}
        for assertion in source_page.items[:MAX_CONTRADICTION_SCAN]:
            key = json.dumps(assertion["object"], sort_keys=True, ensure_ascii=False)
            grouped.setdefault(key, []).append(assertion)
        alternatives = [
            {
                "object": values[0]["object"],
                "assertions": sorted(values, key=lambda item: item["assertion_id"]),
            }
            for values in grouped.values()
            if len(values) > 1 or len(grouped) > 1
        ]
        if len(alternatives) < 2:
            alternatives = []
        result = (
            [
                {
                    "subject_id": subject_id,
                    "predicate_iri": predicate,
                    "valid_at": _format_instant(instant),
                    "alternatives": sorted(
                        alternatives, key=lambda item: json.dumps(item["object"], sort_keys=True)
                    ),
                }
            ]
            if alternatives
            else []
        )
        truncated = (
            len(source_page.items) > MAX_CONTRADICTION_SCAN
            or source_page.has_more
            or source_page.truncated
        )
        selected = tuple(result[page_offset : page_offset + page_limit])
        return QueryPage(
            items=selected,
            total=len(result),
            limit=page_limit,
            offset=page_offset,
            has_more=truncated or page_offset + page_limit < len(result),
            truncated=truncated,
        )

    def entities_at_location(
        self,
        location_uri: str,
        instant: datetime,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> QueryPage[str]:
        """Find entities uniquely asserted at a location and valid time."""
        instant = _validate_instant(instant)
        location_uri = require_absolute_iri(location_uri, "location_uri")
        page_limit, page_offset = _validate_page(limit, offset)
        timeout = _validate_timeout(timeout_ms)
        with self._adapter.query_timeout(timeout):
            return self._adapter.entities_at_page(
                location_uri, instant, limit=page_limit, offset=page_offset
            )

    def track_provenance(
        self, track_id: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> dict | None:
        """Return one track and its observation/source lineage."""
        timeout = _validate_timeout(timeout_ms)
        with self._adapter.query_timeout(timeout):
            return self._adapter.get_track(track_id)

    def tracks_for_entity(
        self,
        entity_id: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> QueryPage[dict]:
        """Return paginated track hypotheses associated with an entity."""
        page_limit, page_offset = _validate_page(limit, offset)
        timeout = _validate_timeout(timeout_ms)
        with self._adapter.query_timeout(timeout):
            return self._adapter.tracks_page(entity_id, limit=page_limit, offset=page_offset)


def _validate_page(limit: int, offset: int) -> tuple[int, int]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be from 1 to {MAX_PAGE_SIZE}")
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or not 0 <= offset <= MAX_PAGE_OFFSET
    ):
        raise ValueError(f"offset must be from 0 to {MAX_PAGE_OFFSET}")
    return limit, offset


def _validate_timeout(timeout_ms: int) -> int:
    if (
        not isinstance(timeout_ms, int)
        or isinstance(timeout_ms, bool)
        or not MIN_TIMEOUT_MS <= timeout_ms <= MAX_TIMEOUT_MS
    ):
        raise ValueError(f"timeout_ms must be from {MIN_TIMEOUT_MS} to {MAX_TIMEOUT_MS}")
    return timeout_ms


def _validate_instant(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("instant must be timezone-aware")
    return instant.astimezone(UTC)


def _validate_predicate(predicate_iri: str | None) -> str | None:
    return require_absolute_iri(predicate_iri, "predicate_iri") if predicate_iri else None


def _format_instant(instant: datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")
