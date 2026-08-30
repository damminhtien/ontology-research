"""Freeze guards for pinned namespaces and identifier schemes.

These URIs are embedded in every event, lake row and RDF graph. Once real
production facts exist, changing any pinned value in place orphans the
existing facts. If one of these tests fails, the change was NOT accidental:
introduce a new namespace and an explicit migration instead.
"""

from __future__ import annotations

from pathlib import Path

from foundry import namespaces

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_TTL = REPO_ROOT / "ontology" / "core" / "core.ttl"


def test_pinned_namespace_values():
    assert namespaces.ONTOLOGY_BASE == "https://damminhtien.github.io/ontology-research/ontology"
    assert namespaces.CORE_ONTOLOGY_NS == "https://damminhtien.github.io/ontology-research/ontology/core#"
    assert namespaces.ENTITY_URN_PREFIX == "urn:world:entity:"
    assert namespaces.FACT_URN_PREFIX == "urn:fact:"


def test_ttl_namespace_matches_runtime_constant():
    text = CORE_TTL.read_text(encoding="utf-8")
    assert f"<{namespaces.CORE_ONTOLOGY_NS.rstrip('#')}>" in text
    assert f'vann:preferredNamespaceUri "{namespaces.CORE_ONTOLOGY_NS}"' in text


def test_minted_ids_use_frozen_schemes():
    entity = namespaces.new_entity_id()
    fact = namespaces.new_fact_iri()
    location = namespaces.new_location_iri()
    assert entity.startswith(namespaces.ENTITY_URN_PREFIX)
    assert len(entity) > len(namespaces.ENTITY_URN_PREFIX)
    assert fact.startswith(namespaces.FACT_URN_PREFIX)
    assert location.startswith(namespaces.LOCATION_URN_PREFIX)
    # uniqueness
    assert namespaces.new_entity_id() != entity
    assert namespaces.new_fact_iri() != fact
