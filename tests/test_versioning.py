"""Tests for the ontology versioning engine and release registry."""

from __future__ import annotations

import argparse
from pathlib import Path

import manage_ontology as mgmt
import pytest
from conftest import CORE_ONTOLOGY


class TestSemverHelpers:
    def test_parse_version_valid(self):
        assert mgmt.parse_version("1.2.3") == (1, 2, 3)

    def test_parse_version_rejects_non_semver(self):
        with pytest.raises(ValueError, match="not valid SemVer"):
            mgmt.parse_version("1.2")

    def test_bump_level_matrix(self):
        assert mgmt.bump_level("0.1.0", "0.1.0") == mgmt.SEVERITY_NONE
        assert mgmt.bump_level("0.1.0", "0.1.1") == mgmt.SEVERITY_PATCH
        assert mgmt.bump_level("0.1.0", "0.2.0") == mgmt.SEVERITY_MINOR
        assert mgmt.bump_level("0.1.0", "1.0.0") == mgmt.SEVERITY_MAJOR

    def test_bump_level_rejects_downgrade(self):
        with pytest.raises(ValueError, match="downgraded"):
            mgmt.bump_level("1.0.0", "0.9.0")

    def test_next_version_per_severity(self):
        assert mgmt.next_version("0.1.0", mgmt.SEVERITY_PATCH) == "0.1.1"
        assert mgmt.next_version("0.1.0", mgmt.SEVERITY_MINOR) == "0.2.0"
        assert mgmt.next_version("0.1.0", mgmt.SEVERITY_MAJOR) == "1.0.0"


class TestModuleIdentity:
    def test_reads_core_module(self):
        iri, version = mgmt.module_identity(mgmt.load_graph(CORE_ONTOLOGY))
        assert iri == "https://damminhtien.github.io/ontology-research/ontology/core"
        assert version == "0.1.0"

    def test_rejects_missing_version_header(self, tmp_path):
        module = tmp_path / "broken.ttl"
        module.write_text(
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "<https://example.org/m> a owl:Ontology .\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="dcterms:version"):
            mgmt.module_identity(mgmt.load_graph(module))


def _make_registry(tmp_path):
    registry = tmp_path / "registry"
    registry.mkdir()
    return registry


def _baseline_terms() -> dict:
    return {
        "https://example.org/ex#Widget": {
            "kind": "class",
            "label": "Widget",
            "comment": "",
            "parents": set(),
            "properties": [],
        }
    }


def _changed_label_terms() -> dict:
    terms = _baseline_terms()
    terms["https://example.org/ex#Widget"]["label"] = "Widget v2"
    return terms


def _added_class_terms() -> dict:
    terms = _baseline_terms()
    terms["https://example.org/ex#Sprocket"] = {
        "kind": "class",
        "label": "Sprocket",
        "comment": "",
        "parents": set(),
        "properties": [],
    }
    return terms


def _latest_entry(version: str) -> dict:
    return {"module_iri": "https://example.org/ex", "version": version}


class TestEvaluateModuleVersion:
    def test_no_changes_same_version_is_ok(self):
        status, _msg = mgmt.evaluate_module_version(
            _latest_entry("0.1.0"), _baseline_terms(), "0.1.0", _baseline_terms()
        )
        assert status == "ok"

    def test_empty_bump_is_error(self):
        status, msg = mgmt.evaluate_module_version(
            _latest_entry("0.1.0"), _baseline_terms(), "0.1.1", _baseline_terms()
        )
        assert status == "error"
        assert "no semantic changes" in msg

    def test_change_without_bump_is_error(self):
        status, msg = mgmt.evaluate_module_version(
            _latest_entry("0.1.0"), _baseline_terms(), "0.1.0", _changed_label_terms()
        )
        assert status == "error"
        assert "bump dcterms:version" in msg

    def test_breaking_change_hidden_in_patch_bump_is_error(self):
        status, _ = mgmt.evaluate_module_version(
            _latest_entry("0.1.0"), _baseline_terms(), "0.1.1", _added_class_terms()
        )
        # added class is MINOR > PATCH bump -> must fail
        assert status == "error"

    def test_matching_minor_bump_is_ok(self):
        status, msg = mgmt.evaluate_module_version(
            _latest_entry("0.1.0"), _baseline_terms(), "0.2.0", _added_class_terms()
        )
        assert status == "ok"
        assert "MINOR" in msg

    def test_conservative_major_bump_warns(self):
        status, msg = mgmt.evaluate_module_version(
            _latest_entry("0.1.0"), _baseline_terms(), "1.0.0", _changed_label_terms()
        )
        assert status == "warn"
        assert "conservative" in msg


class TestRegistryPersistence:
    def test_release_round_trip(self, tmp_path):
        registry = _make_registry(tmp_path)
        entries = [
            {
                "module_iri": "https://example.org/ex",
                "version": "0.1.0",
                "date": "2026-01-01T00:00:00Z",
                "severity": mgmt.SEVERITY_NONE,
                "changes": [],
                "migration": None,
                "commit": None,
            }
        ]
        mgmt.save_releases(registry, entries)
        loaded = mgmt.load_releases(registry)
        assert loaded == entries
        assert mgmt.latest_release(loaded, "https://example.org/ex") == entries[0]
        assert mgmt.latest_release(loaded, "https://example.org/other") is None

    def test_snapshot_jsonable_round_trip_preserves_diff(self, tmp_path):
        registry = _make_registry(tmp_path)
        terms = _baseline_terms()
        terms["https://example.org/ex#Child"] = dict(
            terms["https://example.org/ex#Widget"], parents={"https://example.org/ex#Widget"}
        )
        mgmt.save_snapshot(registry, "ex", "0.1.0", "https://example.org/ex", "ex.ttl", terms)
        restored = mgmt.load_snapshot_terms(registry, "ex", "0.1.0")

        assert restored["https://example.org/ex#Child"]["parents"] == {
            "https://example.org/ex#Widget"
        }
        assert mgmt.diff_snapshots(terms, restored) == []

    def test_load_missing_registry_returns_empty(self, tmp_path):
        assert mgmt.load_releases(_make_registry(tmp_path)) == []


class TestReleaseFlow:
    """End-to-end release flow against a fake repo rooted in tmp_path."""

    V1 = (
        'demo:Widget a owl:Class ; rdfs:label "Widget"@en .\n'
        "demo:gadget a owl:ObjectProperty ; rdfs:domain demo:Widget ; rdfs:range demo:Gizmo .\n"
    )

    @staticmethod
    def _write_demo_module(repo: Path, version: str, body: str) -> Path:
        module = repo / "ontology" / "demo.ttl"
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            "@prefix dcterms: <http://purl.org/dc/terms/> .\n"
            "@prefix demo: <https://example.org/demo#> .\n\n"
            "<https://example.org/demo> a owl:Ontology ;\n"
            f'    dcterms:title "Demo"@en ;\n    dcterms:version "{version}" .\n\n' + body,
            encoding="utf-8",
        )
        return module

    @pytest.fixture()
    def fake_repo(self, tmp_path, monkeypatch):
        """A tmp repo with one released demo 0.1.0 module."""
        repo = tmp_path
        self._write_demo_module(repo, "0.1.0", self.V1)
        monkeypatch.setattr(mgmt, "REPO_ROOT", repo)
        monkeypatch.setattr(
            mgmt,
            "find_module_files",
            lambda roots=("ontology",): sorted((repo / "ontology").rglob("*.ttl")),
        )
        return repo

    @staticmethod
    def _release_args(migration=None, dry_run=False) -> argparse.Namespace:
        return argparse.Namespace(module="demo", migration=migration, dry_run=dry_run)

    def test_initial_baseline_release(self, fake_repo):
        assert mgmt.cmd_release(self._release_args()) == 0

        entries = mgmt.load_releases(fake_repo / "registry")
        assert len(entries) == 1
        assert entries[0]["version"] == "0.1.0"
        assert entries[0]["severity"] == mgmt.SEVERITY_NONE

        snapshot = mgmt.load_snapshot_terms(fake_repo / "registry", "demo", "0.1.0")
        assert "https://example.org/demo#Widget" in snapshot

        changelog = (fake_repo / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "demo" in changelog and "0.1.0" in changelog

    def test_rerelease_without_change_fails(self, fake_repo):
        mgmt.cmd_release(self._release_args())
        assert mgmt.cmd_release(self._release_args()) == 1

    def test_check_versions_ok_between_releases(self, fake_repo):
        mgmt.cmd_release(self._release_args())
        assert mgmt.cmd_check_versions(argparse.Namespace()) == 0

    def test_minor_flow_adds_second_release(self, fake_repo):
        mgmt.cmd_release(self._release_args())
        self._write_demo_module(
            fake_repo,
            "0.2.0",
            self.V1 + 'demo:Sprocket a owl:Class ; rdfs:label "Sprocket"@en .\n',
        )
        assert mgmt.cmd_check_versions(argparse.Namespace()) == 0

        assert mgmt.cmd_release(self._release_args()) == 0
        entries = mgmt.load_releases(fake_repo / "registry")
        assert [e["version"] for e in entries] == ["0.1.0", "0.2.0"]
        assert entries[1]["severity"] == mgmt.SEVERITY_MINOR

    def test_major_requires_migration_note(self, fake_repo):
        mgmt.cmd_release(self._release_args())
        self._write_demo_module(
            fake_repo,
            "1.0.0",
            self.V1.replace("rdfs:range demo:Gizmo", "rdfs:range demo:Thing"),
        )

        assert mgmt.cmd_release(self._release_args()) == 1

        with_note = argparse.Namespace(
            module="demo",
            migration="gadget range widened Gizmo -> Thing; re-map downstream data.",
            dry_run=False,
        )
        assert mgmt.cmd_release(with_note) == 0
        changelog = (fake_repo / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "#### Migration" in changelog
        assert "re-map downstream data" in changelog

    def test_dry_run_records_nothing(self, fake_repo):
        self._write_demo_module(
            fake_repo,
            "0.2.0",
            self.V1 + 'demo:Sprocket a owl:Class ; rdfs:label "Sprocket"@en .\n',
        )
        assert mgmt.cmd_release(self._release_args(dry_run=True)) == 0
        assert mgmt.load_releases(fake_repo / "registry") == []


class TestBlastRadius:
    def test_finds_consumers_across_categories(self, tmp_path):
        (tmp_path / "ontology").mkdir(parents=True)
        (tmp_path / "ontology" / "other.ttl").write_text(
            "# references :Widget for alignment\n", encoding="utf-8"
        )
        queries = tmp_path / "benchmarks" / "queries"
        queries.mkdir(parents=True)
        (queries / "q.rq").write_text("SELECT * WHERE { ?x a ex:Widget }", encoding="utf-8")
        foundry = tmp_path / "foundry"
        foundry.mkdir()
        (foundry / "app.py").write_text('ALLOWED = {"Widget"}\n', encoding="utf-8")

        radius = mgmt.blast_radius_for_terms(["Widget"], repo_root=tmp_path)
        score = sum(len(files) for files in radius.values())

        assert score == 3
        assert radius["modules"] == ["ontology/other.ttl"]
        assert radius["queries"] == ["benchmarks/queries/q.rq"]
        assert radius["applications"] == ["foundry/app.py"]


class TestStability:
    def test_stability_report_values(self):
        releases = [
            {"module_iri": "https://example.org/core", "severity": mgmt.SEVERITY_NONE},
            {"module_iri": "https://example.org/core", "severity": mgmt.SEVERITY_MAJOR},
        ]
        assert mgmt.stability_report(releases) == [("https://example.org/core", 0.5, 1, 2)]

    def test_cmd_stability_enforces_core_threshold(self, tmp_path, monkeypatch):
        registry = _make_registry(tmp_path)
        core_iri = "https://damminhtien.github.io/ontology-research/ontology/core"
        mgmt.save_releases(
            registry,
            [
                {"module_iri": core_iri, "severity": mgmt.SEVERITY_NONE},
                {"module_iri": core_iri, "severity": mgmt.SEVERITY_MAJOR},
            ],
        )
        monkeypatch.setattr(mgmt, "REPO_ROOT", tmp_path)
        # core stability = 0.5 < 0.99 threshold -> enforced failure
        assert mgmt.cmd_stability(argparse.Namespace()) == 1
