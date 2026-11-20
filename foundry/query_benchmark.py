"""Shared operational query boundary for backend evaluation."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from foundry.namespaces import ASSERTION_MIDDLE_NS, CORE_ONTOLOGY_NS, require_absolute_iri
from foundry.query_service import (
    MAX_PAGE_SIZE,
    MAX_TIMEOUT_MS,
    MIN_TIMEOUT_MS,
    QueryService,
    QueryTimeoutError,
)
from foundry.readmodel import EntityView


@dataclass(frozen=True)
class EntityLookup:
    """Stable entity fields returned by the matched backend boundary."""

    entity_id: str
    entity_type: str
    name: str


@dataclass(frozen=True)
class AssertionProvenance:
    """Provenance fields required by the common benchmark workload."""

    assertion_id: str
    status: str
    source_ids: tuple[str, ...]
    evidence: tuple[tuple[str | None, ...], ...]


class OperationalQueryBackend(Protocol):
    """Backend operations measured at the same normalized query boundary."""

    def entity_lookup(self, entity_id: str, *, timeout_ms: int) -> EntityLookup | None:
        """Look up one entity by stable identifier."""
        ...

    def relationships_as_of(
        self,
        subject_id: str,
        instant: datetime,
        *,
        predicate_iri: str,
        limit: int,
        timeout_ms: int,
    ) -> tuple[tuple[str, str], ...]:
        """Return active entity-valued assertions at a half-open instant."""
        ...

    def assertion_provenance(
        self, assertion_id: str, *, timeout_ms: int
    ) -> AssertionProvenance | None:
        """Return sources and exact evidence for one assertion."""
        ...


def _evidence_rows(assertion: dict) -> tuple[tuple[str | None, ...], ...]:
    rows = []
    for evidence in assertion.get("evidence", []):
        span = evidence.get("span") or {}
        rows.append(
            (
                evidence.get("source_id"),
                evidence.get("locator"),
                str(span["start"]) if "start" in span else None,
                str(span["end"]) if "end" in span else None,
                span.get("unit"),
                span.get("exact_text"),
            )
        )
    return tuple(sorted(rows, key=repr))


class PostgresQueryBackend:
    """Expose PostgreSQL through the production ``QueryService`` contract."""

    def __init__(self, query_service: QueryService) -> None:
        """Wrap the production query facade."""
        self._queries = query_service

    def entity_lookup(self, entity_id: str, *, timeout_ms: int) -> EntityLookup | None:
        """Look up and normalize one PostgreSQL entity result."""
        entity = self._queries.entity_lookup(entity_id, timeout_ms=timeout_ms)
        return _entity_lookup(entity) if entity else None

    def relationships_as_of(
        self,
        subject_id: str,
        instant: datetime,
        *,
        predicate_iri: str,
        limit: int,
        timeout_ms: int,
    ) -> tuple[tuple[str, str], ...]:
        """Run the production as-of relationship query and normalize its page."""
        page = self._queries.relationships_as_of(
            subject_id,
            instant,
            predicate_iri=predicate_iri,
            limit=limit,
            timeout_ms=timeout_ms,
        )
        return tuple((item["assertion_id"], item["object"]["value"]) for item in page.items)

    def assertion_provenance(
        self, assertion_id: str, *, timeout_ms: int
    ) -> AssertionProvenance | None:
        """Read provenance through the production query facade."""
        assertion = self._queries.assertion_provenance(assertion_id, timeout_ms=timeout_ms)
        if assertion is None:
            return None
        return AssertionProvenance(
            assertion_id=assertion_id,
            status=assertion["status"],
            source_ids=tuple(sorted(assertion["source_ids"])),
            evidence=_evidence_rows(assertion),
        )


class OxigraphQueryBackend:
    """Expose an Oxigraph snapshot through the same normalized operations."""

    def __init__(self, store) -> None:
        """Bind a read-only Oxigraph snapshot."""
        self._store = store

    def _query(self, query: str, timeout_ms: int):
        started = time.monotonic()
        result = tuple(self._store.query(query))
        if (time.monotonic() - started) * 1000 > timeout_ms:
            # pyoxigraph 0.5 has no query cancellation option. Report an
            # overrun after consuming its lazy result iterator; the in-process
            # query itself cannot be interrupted.
            raise QueryTimeoutError(f"Oxigraph query exceeded {timeout_ms} ms")
        return result

    def entity_lookup(self, entity_id: str, *, timeout_ms: int) -> EntityLookup | None:
        """Look up one entity and normalize it to the shared result shape."""
        from rdflib import URIRef

        subject = URIRef(entity_id).n3()
        entity_class = URIRef(f"{CORE_ONTOLOGY_NS}Entity").n3()
        query = f"""SELECT ?name ?type WHERE {{
          {subject} <{CORE_ONTOLOGY_NS}name> ?name ; a ?type .
          FILTER(STRSTARTS(STR(?type), {self._sparql_string(CORE_ONTOLOGY_NS)}))
        }} ORDER BY (?type = {entity_class}) ?type ?name LIMIT 1"""
        result = self._query(query, timeout_ms)
        row = next(iter(result), None)
        if row is None:
            return None
        entity_type_iri = row["type"].value
        entity_type = entity_type_iri.removeprefix(CORE_ONTOLOGY_NS) or "Entity"
        return EntityLookup(entity_id, entity_type, row["name"].value)

    def relationships_as_of(
        self,
        subject_id: str,
        instant: datetime,
        *,
        predicate_iri: str,
        limit: int,
        timeout_ms: int,
    ) -> tuple[tuple[str, str], ...]:
        """Return the same bounded, half-open relationship page as PostgreSQL."""
        from rdflib import Literal, URIRef
        from rdflib.namespace import XSD

        subject = URIRef(subject_id).n3()
        predicate = URIRef(predicate_iri).n3()
        instant_literal = Literal(instant.isoformat(), datatype=XSD.dateTime).n3()
        status = URIRef(f"{ASSERTION_MIDDLE_NS}status").n3()
        describes = URIRef(f"{CORE_ONTOLOGY_NS}describes").n3()
        valid_from = URIRef(f"{CORE_ONTOLOGY_NS}validFrom").n3()
        valid_until = URIRef(f"{CORE_ONTOLOGY_NS}validUntil").n3()
        assertion_predicate = URIRef(f"{ASSERTION_MIDDLE_NS}predicate").n3()
        has_object = URIRef(f"{ASSERTION_MIDDLE_NS}hasObject").n3()
        supersedes = URIRef(f"{ASSERTION_MIDDLE_NS}supersedes").n3()
        retracts = URIRef(f"{ASSERTION_MIDDLE_NS}retracts").n3()
        location_type = URIRef(f"{CORE_ONTOLOGY_NS}Location").n3()
        query = f"""SELECT ?assertion ?object ?from WHERE {{
          ?assertion {describes} {subject} ; {assertion_predicate} {predicate} ;
                     {status} "asserted" ; {valid_from} ?from ; {has_object} ?object .
          FILTER (?from <= {instant_literal})
          OPTIONAL {{ ?assertion {valid_until} ?until . }}
          FILTER (!BOUND(?until) || {instant_literal} < ?until)
          FILTER NOT EXISTS {{ ?replacement {supersedes} ?assertion . }}
          FILTER NOT EXISTS {{ ?retraction {retracts} ?assertion . }}
          FILTER NOT EXISTS {{ ?object a {location_type} . }}
        }} ORDER BY ?from ?assertion ?object LIMIT {limit}"""
        result = self._query(query, timeout_ms)
        return tuple((row["assertion"].value, row["object"].value) for row in result)

    def assertion_provenance(
        self, assertion_id: str, *, timeout_ms: int
    ) -> AssertionProvenance | None:
        """Read and normalize source and evidence triples for one assertion."""
        from rdflib import URIRef

        assertion = URIRef(assertion_id).n3()
        status_iri = URIRef(f"{ASSERTION_MIDDLE_NS}status").n3()
        source_iri = URIRef(f"{CORE_ONTOLOGY_NS}hasSource").n3()
        evidence_iri = URIRef(f"{ASSERTION_MIDDLE_NS}evidence").n3()
        evidence_source_iri = URIRef(f"{ASSERTION_MIDDLE_NS}evidenceSource").n3()
        locator_iri = URIRef(f"{ASSERTION_MIDDLE_NS}locator").n3()
        start_iri = URIRef(f"{ASSERTION_MIDDLE_NS}spanStart").n3()
        end_iri = URIRef(f"{ASSERTION_MIDDLE_NS}spanEnd").n3()
        unit_iri = URIRef(f"{ASSERTION_MIDDLE_NS}spanUnit").n3()
        text_iri = URIRef(f"{ASSERTION_MIDDLE_NS}exactText").n3()
        status_query = f"SELECT ?status WHERE {{ {assertion} {status_iri} ?status }}"
        status_rows = list(self._query(status_query, timeout_ms))
        if not status_rows:
            return None
        source_query = (
            f"SELECT ?source WHERE {{ {assertion} {source_iri} ?source }} ORDER BY ?source"
        )
        source_rows = list(self._query(source_query, timeout_ms))
        evidence_query = f"""SELECT ?source ?locator ?start ?end ?unit ?text WHERE {{
          {assertion} {evidence_iri} ?node .
          ?node {evidence_source_iri} ?source .
          OPTIONAL {{ ?node {locator_iri} ?locator . }}
          OPTIONAL {{ ?node {start_iri} ?start . }}
          OPTIONAL {{ ?node {end_iri} ?end . }}
          OPTIONAL {{ ?node {unit_iri} ?unit . }}
          OPTIONAL {{ ?node {text_iri} ?text . }}
        }} ORDER BY ?source ?locator ?start ?end"""
        evidence_rows = list(self._query(evidence_query, timeout_ms))
        evidence = tuple(
            sorted(
                [
                    (
                        row["source"].value,
                        _term_value(row, "locator"),
                        _term_value(row, "start"),
                        _term_value(row, "end"),
                        _term_value(row, "unit"),
                        _term_value(row, "text"),
                    )
                    for row in evidence_rows
                ],
                key=repr,
            )
        )
        return AssertionProvenance(
            assertion_id=assertion_id,
            status=status_rows[0]["status"].value,
            source_ids=tuple(row["source"].value for row in source_rows),
            evidence=evidence,
        )

    @staticmethod
    def _sparql_string(value: str) -> str:
        from rdflib import Literal

        return Literal(value).n3()


def _entity_lookup(entity: EntityView) -> EntityLookup:
    return EntityLookup(entity.entity_id, entity.entity_type, entity.name)


def _term_value(row, name: str) -> str | None:
    term = row[name]
    return term.value if term is not None else None


class OperationalQueryBoundary:
    """One stable call surface over interchangeable operational backends."""

    def __init__(self, backend: OperationalQueryBackend) -> None:
        """Bind one backend implementation to the shared operation contract."""
        self._backend = backend

    def entity_lookup(self, entity_id: str, *, timeout_ms: int) -> EntityLookup | None:
        """Validate and look up one canonical entity."""
        require_absolute_iri(entity_id, "entity_id")
        _validate_timeout(timeout_ms)
        return self._backend.entity_lookup(entity_id, timeout_ms=timeout_ms)

    def relationships_as_of(
        self,
        subject_id: str,
        instant: datetime,
        *,
        predicate_iri: str,
        limit: int,
        timeout_ms: int,
    ) -> tuple[tuple[str, str], ...]:
        """Validate and return one page of relationships active at an instant."""
        require_absolute_iri(subject_id, "subject_id")
        require_absolute_iri(predicate_iri, "predicate_iri")
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("instant must be timezone-aware")
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError(f"limit must be from 1 to {MAX_PAGE_SIZE}")
        _validate_timeout(timeout_ms)
        return self._backend.relationships_as_of(
            subject_id,
            instant,
            predicate_iri=predicate_iri,
            limit=limit,
            timeout_ms=timeout_ms,
        )

    def assertion_provenance(
        self, assertion_id: str, *, timeout_ms: int
    ) -> AssertionProvenance | None:
        """Validate and retrieve source and evidence lineage."""
        require_absolute_iri(assertion_id, "assertion_id")
        _validate_timeout(timeout_ms)
        return self._backend.assertion_provenance(assertion_id, timeout_ms=timeout_ms)


def postgres_boundary(query_service: QueryService) -> OperationalQueryBoundary:
    """Create the shared query boundary over the production service."""
    return OperationalQueryBoundary(PostgresQueryBackend(query_service))


def oxigraph_boundary(store) -> OperationalQueryBoundary:
    """Create the shared query boundary over an Oxigraph snapshot."""
    return OperationalQueryBoundary(OxigraphQueryBackend(store))


def _validate_timeout(timeout_ms: int) -> None:
    """Reject timeouts outside the production query contract."""
    if (
        not isinstance(timeout_ms, int)
        or isinstance(timeout_ms, bool)
        or not MIN_TIMEOUT_MS <= timeout_ms <= MAX_TIMEOUT_MS
    ):
        raise ValueError(f"timeout_ms must be from {MIN_TIMEOUT_MS} to {MAX_TIMEOUT_MS}")
