"""Tests for the ontology versioning engine and release registry."""

from __future__ import annotations

import json

import pytest

import manage_ontology as mgmt
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
        assert iri == "https://ontology.example/core"
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
        status, msg = mgmt.evaluate_module_version(
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
