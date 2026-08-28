"""Tests for migration script generation and the alignment registry (Phase 5)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import manage_ontology as mgmt
import pytest
from conftest import CORE_ONTOLOGY


def argparse_namespace(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _terms(iri: str) -> dict:
    """Minimal snapshot containing one object property and one class."""
    prop = f"{iri}#prop"
    cls = f"{iri}#Class"
    return {
        "snapshot_version": 1,
        "classes": [cls],
        "object_properties": [prop],
        "data_properties": [],
        "annotation_properties": [],
        "individuals": [],
        "property_domains": {prop: [f"{iri}#Class"]},
        "property_ranges": {prop: [f"{iri}#Class"]},
        "sub_class_of": {},
    }


class TestGenerateMigrationScript:
    def test_rejects_non_breaking_changes(self):
        with pytest.raises(ValueError, match="nothing to migrate"):
            mgmt.generate_migration_script("core", "0.1.0", "0.1.1", [])

    def test_removed_property_becomes_delete(self, tmp_path):
        removed = mgmt.Change(
            kind="removed",
            severity=mgmt.SEVERITY_MAJOR,
            target="https://ontology.example/core#operates",
            detail="removed object property",
            field_changes=[],
        )
        path = mgmt.generate_migration_script("core", "0.1.0", "1.0.0", [removed], tmp_path)
        text = path.read_text(encoding="utf-8")
        assert f"DELETE {{ ?s <{removed.target}> ?o }}" in text
        assert "REVIEW BEFORE APPLYING" in text

    def test_removed_class_becomes_investigation_not_delete(self, tmp_path):
        removed = mgmt.Change(
            kind="removed",
            severity=mgmt.SEVERITY_MAJOR,
            target="https://ontology.example/core#Facility",
            detail="removed class",
            field_changes=[],
        )
        path = mgmt.generate_migration_script("core", "0.1.0", "1.0.0", [removed], tmp_path)
        text = path.read_text(encoding="utf-8")
        assert text.count("DELETE {") == 0  # no destructive statement
        assert f"# SELECT ?instance WHERE {{ ?instance a <{removed.target}> }}" in text

    def test_domain_change_requires_revalidation_note(self, tmp_path):
        changed = mgmt.Change(
            kind="changed",
            severity=mgmt.SEVERITY_MAJOR,
            target="https://ontology.example/core#operates",
            detail="changed property definition",
            field_changes=["domain: Artifact -> Entity"],
        )
        path = mgmt.generate_migration_script("core", "0.1.0", "1.0.0", [changed], tmp_path)
        assert "Re-run SHACL validation" in path.read_text(encoding="utf-8")


class TestCmdMigrate:
    def _released_repo(self, tmp_path, monkeypatch):
        """Fake repo root with core released at 0.1.0 (baseline = real core terms)."""
        registry = tmp_path / "registry"
        (registry / "snapshots" / "core").mkdir(parents=True)
        (registry / "releases.json").write_text(
            json.dumps(
                {
                    "registry_version": 1,
                    "releases": [
                        {
                            "module_iri": "https://ontology.example/core",
                            "version": "0.1.0",
                            "severity": "NONE",
                            "changes": [],
                        }
                    ],
                }
            ),
            "utf-8",
        )
        graph = mgmt.load_graph(CORE_ONTOLOGY)
        baseline = {
            "snapshot_version": mgmt.SNAPSHOT_VERSION,
            "terms": mgmt.terms_to_jsonable(mgmt.snapshot(graph)),
        }
        (registry / "snapshots" / "core" / "0.1.0.json").write_text(json.dumps(baseline), "utf-8")
        monkeypatch.setattr(mgmt, "registry_dir", lambda: registry)
        monkeypatch.setattr(mgmt, "REPO_ROOT", tmp_path)
        return registry

    def test_fails_when_module_never_released(self, tmp_path, monkeypatch):
        registry = tmp_path / "registry"
        registry.mkdir()
        (registry / "releases.json").write_text(
            json.dumps({"registry_version": 1, "releases": []}), "utf-8"
        )
        monkeypatch.setattr(mgmt, "registry_dir", lambda: registry)
        assert mgmt.cmd_migrate(argparse_namespace(module="core")) == 1

    def test_no_breaking_changes_reports_and_fails(self, tmp_path, monkeypatch, capsys):
        self._released_repo(tmp_path, monkeypatch)
        monkeypatch.setattr(mgmt, "find_module_files", lambda: [CORE_ONTOLOGY])
        code = mgmt.cmd_migrate(argparse_namespace(module="core"))
        out = capsys.readouterr().out
        assert code == 1
        assert "no MAJOR changes" in out

    def test_end_to_end_major_break_generates_script(self, tmp_path, monkeypatch, capsys):
        """Baseline keeps `operates`; broken ttl drops it -> script with DELETE."""
        self._released_repo(tmp_path, monkeypatch)
        broken = tmp_path / "core_broken.ttl"
        text = CORE_ONTOLOGY.read_text(encoding="utf-8")
        start = text.index("core:operates a owl:ObjectProperty")
        end = text.index(" .", start) + 2
        broken.write_text(text[:start] + text[end:], encoding="utf-8")
        monkeypatch.setattr(mgmt, "find_module_files", lambda: [broken])

        code = mgmt.cmd_migrate(argparse_namespace(module="core"))
        out = capsys.readouterr().out
        assert code == 0
        assert "Generated" in out
        script = Path(out.split("Generated ")[1].splitlines()[0].strip())
        assert "DELETE" in script.read_text(encoding="utf-8")
        assert (tmp_path / "migrations" / "core").is_dir()


class TestAlignmentRegistry:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "alignments.json"
        mgmt.save_alignments(
            path,
            [{"source": "s", "target": "t", "relation": "exactMatch", "note": "", "added": "x"}],
        )
        assert len(mgmt.load_alignments(path)) == 1

    def test_repo_alignments_file_exists_and_valid(self):
        alignments = mgmt.load_alignments(mgmt.alignments_file())
        assert len(alignments) >= 3
        assert all(a["relation"] in mgmt.ALIGNMENT_RELATIONS for a in alignments)

    def test_align_check_passes_on_repo(self, capsys):
        assert mgmt.cmd_align_check(argparse_namespace()) == 0
        assert "Alignment check passed" in capsys.readouterr().out

    def test_align_check_fails_on_dangling_source(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "alignments.json"
        mgmt.save_alignments(
            path,
            [
                {
                    "source": "https://ontology.example/core#Ghost",
                    "target": "https://schema.org/Thing",
                    "relation": "closeMatch",
                    "note": "",
                    "added": "x",
                }
            ],
        )
        monkeypatch.setattr(mgmt, "alignments_file", lambda: path)
        monkeypatch.setattr(mgmt, "find_module_files", lambda: [CORE_ONTOLOGY])
        assert mgmt.cmd_align_check(argparse_namespace()) == 1
        assert "no longer defined" in capsys.readouterr().out

    def test_align_add_rejects_unknown_term(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "alignments.json"
        monkeypatch.setattr(mgmt, "alignments_file", lambda: path)
        monkeypatch.setattr(mgmt, "find_module_files", lambda: [CORE_ONTOLOGY])
        args = argparse_namespace(
            source="https://ontology.example/core#NoSuchTerm",
            target="https://schema.org/Thing",
            relation="closeMatch",
            note="",
        )
        assert mgmt.cmd_align_add(args) == 1
        assert "not defined" in capsys.readouterr().out

    def test_align_add_rejects_duplicate(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "alignments.json"
        monkeypatch.setattr(mgmt, "alignments_file", lambda: path)
        monkeypatch.setattr(mgmt, "find_module_files", lambda: [CORE_ONTOLOGY])
        args = argparse_namespace(
            source="https://ontology.example/core#Person",
            target="https://schema.org/Person",
            relation="exactMatch",
            note="dup",
        )
        assert mgmt.cmd_align_add(args) == 0
        assert mgmt.cmd_align_add(args) == 1
        assert "already recorded" in capsys.readouterr().out
