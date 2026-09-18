"""Pinned namespaces and identifier schemes (frozen before production facts).

Every canonical identifier and ontology namespace is minted from here. The
values are embedded in events, the lake and generated RDF graphs, so once
real production facts exist they must never change: renaming a namespace
orphans every fact minted under the old one. The freeze tests pin each value
and cross-check the OWL headers — change a URI only with an explicit
migration (new namespace + explicit mapping), never in place.
"""

from __future__ import annotations

import hashlib
import uuid
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Ontology namespaces (mirror the @prefix declarations in ontology/*.ttl).
# ---------------------------------------------------------------------------

ONTOLOGY_BASE = "https://damminhtien.github.io/ontology-research/ontology"
CORE_ONTOLOGY_NS = f"{ONTOLOGY_BASE}/core#"
LOCATION_MIDDLE_NS = f"{ONTOLOGY_BASE}/middle/location#"
ASSERTION_MIDDLE_NS = f"{ONTOLOGY_BASE}/middle/assertion#"
TRACKING_DOMAIN_NS = f"{ONTOLOGY_BASE}/domain/tracking#"
IDENTITY_MIDDLE_NS = f"{ONTOLOGY_BASE}/middle/identity#"

# ---------------------------------------------------------------------------
# Semantic vocabulary: the relation IRIs the assertion lane records.
#
# Event payloads carry a property IRI, never a local name, so these constants
# are the only place a relation is spelled out in code. Extractors, the write
# path and the read model all reference them instead of a string literal.
# ---------------------------------------------------------------------------

#: ``core:locatedAt`` — the entity is at the location.
CORE_LOCATED_AT = f"{CORE_ONTOLOGY_NS}locatedAt"

#: ``core:memberOf`` — the entity is part of the referenced entity.
CORE_MEMBER_OF = f"{CORE_ONTOLOGY_NS}memberOf"

#: Datatype of a literal object given without an explicit datatype: the event
#: contract says "datatype XOR language tag, default xsd:string". Spelled out
#: here rather than imported from rdflib so the payload contract (events,
#: upcasters) does not depend on the RDF toolkit.
DEFAULT_LITERAL_DATATYPE = "http://www.w3.org/2001/XMLSchema#string"


def require_absolute_iri(value: str, field: str) -> str:
    """Return ``value`` once it is known to be an absolute IRI.

    Any scheme is accepted (``https:``, ``urn:``, …) — the contract is "an
    IRI, not a local name", not "an HTTP URL". A bare local name such as
    ``locatedAt`` is rejected: resolving one here would mint an IRI for a
    relation the ontology may never have declared, which is exactly how two
    different statements end up sharing one RDF form.

    Raises:
        ValueError: On a blank value or one without a scheme.
    """
    name = value.strip()
    if not name:
        raise ValueError(f"{field} must be non-empty")
    if not urlparse(name).scheme:
        raise ValueError(f"{field} must be an absolute IRI, got {value!r}")
    return name


#: v2 ``AssertionMade`` events carried a bare relation name. Only these
#: mappings exist: a legacy record naming anything else is not upcastable, and
#: inventing ``core#<name>`` for it would fabricate semantics that no release
#: ever declared.
LEGACY_PREDICATE_IRIS = {
    "locatedAt": CORE_LOCATED_AT,
    "memberOf": CORE_MEMBER_OF,
}


def resolve_legacy_predicate(predicate: str) -> str:
    """Map a v2 bare relation name to its IRI (log upcasting only).

    Reads historical records; the write path never accepts a bare name. An
    absolute IRI passes through, since v2 already allowed one.

    Raises:
        ValueError: On a blank name, or on a bare name outside
            :data:`LEGACY_PREDICATE_IRIS`.
    """
    name = predicate.strip()
    if not name:
        raise ValueError("predicate must be non-empty")
    if urlparse(name).scheme:
        return name
    try:
        return LEGACY_PREDICATE_IRIS[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown legacy predicate {predicate!r}; add an explicit mapping to "
            "LEGACY_PREDICATE_IRIS instead of guessing an IRI"
        ) from exc


# ---------------------------------------------------------------------------
# Runtime identifier schemes.
# ---------------------------------------------------------------------------

#: Canonical entity ids: ``urn:world:entity:<uuid4 hex>`` — stable surrogate
#: identifiers that outlive every external registry.
ENTITY_URN_PREFIX = "urn:world:entity:"

#: Subject IRIs for minted facts: ``urn:fact:<uuid4 hex>``.
FACT_URN_PREFIX = "urn:fact:"

#: Location URIs minted by the pipeline itself (distinct from external
#: location references such as Wikidata items).
LOCATION_URN_PREFIX = "urn:world:location:"

#: Subject IRIs for assertions — reified facts with their own identity:
#: ``urn:assert:<uuid4 hex>`` (see docs/architecture.md §4.2).
ASSERTION_URN_PREFIX = "urn:assert:"

#: Document identifiers for provenance sources: ``urn:doc:<uuid4 hex>``.
DOCUMENT_URN_PREFIX = "urn:doc:"

#: Deterministic IRIs for unresolved references — NOT minted, derived from the
#: cited surface form (md5 of the normalized name) so repeated pending
#: observations of the same name address the same placeholder node.
PENDING_URN_PREFIX = "urn:world:pending:"


def new_entity_id() -> str:
    """Mint a fresh canonical entity id."""
    return f"{ENTITY_URN_PREFIX}{uuid.uuid4().hex}"


def new_fact_iri() -> str:
    """Mint a fresh fact subject IRI."""
    return f"{FACT_URN_PREFIX}{uuid.uuid4().hex}"


def new_location_iri() -> str:
    """Mint a fresh location IRI owned by this deployment."""
    return f"{LOCATION_URN_PREFIX}{uuid.uuid4().hex}"


def new_assertion_id() -> str:
    """Mint a fresh assertion id (stable handle for correct/retract)."""
    return f"{ASSERTION_URN_PREFIX}{uuid.uuid4().hex}"


def new_document_id() -> str:
    """Mint a fresh document id for provenance tracking."""
    return f"{DOCUMENT_URN_PREFIX}{uuid.uuid4().hex}"


def pending_reference_iri(surface_name: str) -> str:
    """Deterministic placeholder IRI for an unresolved reference.

    Same cited name → same IRI, so pending observations of one surface form
    accumulate on one node instead of sprouting duplicates.
    """
    digest = hashlib.md5(normalize_surface(surface_name).encode("utf-8")).hexdigest()
    return f"{PENDING_URN_PREFIX}{digest}"


def normalize_surface(text: str) -> str:
    """Casefold + squeeze whitespace (no punctuation stripping: names may differ only so)."""
    return " ".join(text.casefold().split())
