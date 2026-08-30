"""Tests for the ontology management CLI (diff, scaffold) and visualization."""

from __future__ import annotations

import argparse
from pathlib import Path

import manage_ontology as mgmt
import pytest
from conftest import CORE_ONTOLOGY
from visualize_ontology import build_model, build_tree, render_mermaid


def _write_module(path: Path, body: str) -> Path:
    path.write_text(
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "@prefix ex: <https://example.org/ex#> .\n\n" + body,
        encoding="utf-8",
    )
    return path


V1_BODY = (
    'ex:Widget a owl:Class ; rdfs:label "Widget"@en ; rdfs:comment "A widget."@en .\n'
    "ex:gadget a owl:ObjectProperty ; rdfs:domain ex:Widget ; rdfs:range ex:Gizmo .\n"
)

V2_LABEL_ONLY = V1_BODY.replace('"Widget"@en', '"Widget v2"@en')
V2_RANGE_CHANGE = V1_BODY.replace("rdfs:range ex:Gizmo", "rdfs:range ex:Thing")
V2_REMOVAL = "ex:gadget a owl:ObjectProperty ; rdfs:domain ex:Widget ; rdfs:range ex:Gizmo .\n"


class TestSemanticDiff:
    def test_label_edit_is_patch_level(self, tmp_path):
        old = _write_module(tmp_path / "old.ttl", V1_BODY)
        new = _write_module(tmp_path / "new.ttl", V2_LABEL_ONLY)
        changes = mgmt.diff_snapshots(
            mgmt.snapshot(mgmt.load_graph(old)), mgmt.snapshot(mgmt.load_graph(new))
        )
        assert len(changes) == 1
        assert changes[0].severity == mgmt.SEVERITY_PATCH
        assert mgmt.highest_severity(changes) == mgmt.SEVERITY_PATCH

    def test_range_change_is_breaking(self, tmp_path):
        old = _write_module(tmp_path / "old.ttl", V1_BODY)
        new = _write_module(tmp_path / "new.ttl", V2_RANGE_CHANGE)
        changes = mgmt.diff_snapshots(
            mgmt.snapshot(mgmt.load_graph(old)), mgmt.snapshot(mgmt.load_graph(new))
        )
        assert mgmt.highest_severity(changes) == mgmt.SEVERITY_MAJOR
        gadget = next(c for c in changes if c.target.endswith("gadget"))
        assert any("range" in d for d in gadget.field_changes)

    def test_removed_term_is_breaking(self, tmp_path):
        old = _write_module(tmp_path / "old.ttl", V1_BODY)
        new = _write_module(tmp_path / "new.ttl", V2_REMOVAL)
        changes = mgmt.diff_snapshots(
            mgmt.snapshot(mgmt.load_graph(old)), mgmt.snapshot(mgmt.load_graph(new))
        )
        removals = [c for c in changes if c.kind == "removed"]
        assert len(removals) == 1
        assert removals[0].severity == mgmt.SEVERITY_MAJOR
        assert removals[0].target.endswith("Widget")

    def test_added_term_is_minor(self, tmp_path):
        old = _write_module(tmp_path / "old.ttl", V1_BODY)
        new = _write_module(
            tmp_path / "new.ttl", V1_BODY + 'ex:Sprocket a owl:Class ; rdfs:label "Sprocket".\n'
        )
        changes = mgmt.diff_snapshots(
            mgmt.snapshot(mgmt.load_graph(old)), mgmt.snapshot(mgmt.load_graph(new))
        )
        assert mgmt.highest_severity(changes) == mgmt.SEVERITY_MINOR

    def test_no_changes_yields_empty(self, tmp_path):
        old = _write_module(tmp_path / "old.ttl", V1_BODY)
        new = _write_module(tmp_path / "new.ttl", V1_BODY)
        assert (
            mgmt.diff_snapshots(
                mgmt.snapshot(mgmt.load_graph(old)), mgmt.snapshot(mgmt.load_graph(new))
            )
            == []
        )


class TestNewModuleScaffold:
    def test_scaffold_creates_importing_module(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mgmt, "REPO_ROOT", tmp_path)
        args = argparse.Namespace(name="organization", layer="middle")
        assert mgmt.cmd_new_module(args) == 0

        scaffolded = tmp_path / "ontology" / "middle" / "organization.ttl"
        content = scaffolded.read_text(encoding="utf-8")
        core_iri = "https://damminhtien.github.io/ontology-research/ontology/core"
        module_iri = "https://damminhtien.github.io/ontology-research/ontology/middle/organization"
        assert f"owl:imports <{core_iri}>" in content
        assert f"<{module_iri}>" in content

    def test_scaffold_refuses_existing_and_bad_layer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mgmt, "REPO_ROOT", tmp_path)
        args = argparse.Namespace(name="organization", layer="middle")
        assert mgmt.cmd_new_module(args) == 0
        assert mgmt.cmd_new_module(args) == 1
        bad = argparse.Namespace(name="core", layer="core")
        assert mgmt.cmd_new_module(bad) == 1


@pytest.fixture(scope="module")
def core_model() -> dict:
    """Parsed model of the semantic-core kernel, shared across viz tests."""
    return build_model([CORE_ONTOLOGY])


class TestVisualizationModel:
    def test_model_extracts_kernel(self, core_model):
        assert len(core_model["classes"]) == 20
        assert len(core_model["properties"]) == 33

    def test_hierarchy_roots_at_entity(self, core_model):
        tree = build_tree(core_model)
        root_names = {node["name"] for node in tree}
        assert "Entity" in root_names

        entity = next(n for n in tree if n["name"] == "Entity")
        children = {c["name"] for c in entity["children"]}
        assert {"Agent", "PhysicalObject", "Event"} <= children

    def test_properties_grouped_under_domain(self, core_model):
        core = "https://damminhtien.github.io/ontology-research/ontology/core#"
        org_props = {p["name"] for p in core_model["classes"][core + "Organization"]["properties"]}
        agent_props = {p["name"] for p in core_model["classes"][core + "Agent"]["properties"]}
        assert {"operates"} <= org_props
        assert {"uses", "controls", "owns", "memberOf"} <= agent_props

    def test_mermaid_contains_subclass_edges(self, core_model):
        mermaid = render_mermaid(core_model)
        assert mermaid.startswith("graph TD")
        assert "Agent -->|subClassOf| Person" in mermaid
        assert "|operates|" in mermaid
