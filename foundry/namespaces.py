"""Pinned namespaces and identifier schemes (frozen before production facts).

Every canonical identifier and ontology namespace is minted from here. The
values are embedded in events, the lake and generated RDF graphs, so once
real production facts exist they must never change: renaming a namespace
orphans every fact minted under the old one. The freeze tests pin each value
and cross-check the OWL headers — change a URI only with an explicit
migration (new namespace + explicit mapping), never in place.
"""

from __future__ import annotations

import uuid

# ---------------------------------------------------------------------------
# Ontology namespaces (mirror the @prefix declarations in ontology/*.ttl).
# ---------------------------------------------------------------------------

ONTOLOGY_BASE = "https://damminhtien.github.io/ontology-research/ontology"
CORE_ONTOLOGY_NS = f"{ONTOLOGY_BASE}/core#"
LOCATION_MIDDLE_NS = f"{ONTOLOGY_BASE}/middle/location#"
TRACKING_DOMAIN_NS = f"{ONTOLOGY_BASE}/domain/tracking#"

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


def new_entity_id() -> str:
    """Mint a fresh canonical entity id."""
    return f"{ENTITY_URN_PREFIX}{uuid.uuid4().hex}"


def new_fact_iri() -> str:
    """Mint a fresh fact subject IRI."""
    return f"{FACT_URN_PREFIX}{uuid.uuid4().hex}"


def new_location_iri() -> str:
    """Mint a fresh location IRI owned by this deployment."""
    return f"{LOCATION_URN_PREFIX}{uuid.uuid4().hex}"
