"""Tests for the data-contract version file (VERSION + CHANGELOG-DATA.md)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from foundry import versioning
from foundry.versioning import contract_version, load_versions

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestLoadVersions:
    def test_parses_comments_and_whitespace(self, tmp_path):
        path = tmp_path / "VERSION"
        path.write_text(
            "# header comment\n\nevent_schema = 3   # bumped today\nlake=1\nreference_lane = 2\n",
            encoding="utf-8",
        )
        assert load_versions(path) == {"event_schema": 3, "lake": 1, "reference_lane": 2}

    def test_rejects_malformed_line(self, tmp_path):
        path = tmp_path / "VERSION"
        path.write_text("event_schema = 2\noops\n", encoding="utf-8")
        with pytest.raises(ValueError, match="expected 'key = integer'"):
            load_versions(path)

    def test_rejects_non_integer_value(self, tmp_path):
        path = tmp_path / "VERSION"
        path.write_text("event_schema = two\n", encoding="utf-8")
        with pytest.raises(ValueError, match="must be an integer"):
            load_versions(path)

    def test_rejects_non_positive_version(self, tmp_path):
        path = tmp_path / "VERSION"
        path.write_text("event_schema = 0\n", encoding="utf-8")
        with pytest.raises(ValueError, match="must be >= 1"):
            load_versions(path)

    def test_rejects_unknown_contract_key(self, tmp_path):
        path = tmp_path / "VERSION"
        path.write_text("event_schema = 2\nwidget = 4\n", encoding="utf-8")
        with pytest.raises(ValueError, match="unknown contract 'widget'"):
            load_versions(path)

    def test_rejects_missing_contract(self, tmp_path):
        path = tmp_path / "VERSION"
        path.write_text("lake = 1\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing contract"):
            load_versions(path)


class TestRepoVersionFile:
    def test_repo_file_loads_and_matches_module_constants(self):
        versions = load_versions(REPO_ROOT / "VERSION")
        assert versions["event_schema"] == versioning.EVENT_SCHEMA_VERSION
        assert versions["lake"] == versioning.LAKE_VERSION
        assert contract_version("event_schema") == versions["event_schema"]

    def test_event_schema_current_version_matches_upcaster_chain(self):
        from foundry.events import UPCASTERS

        # every version below the current one needs a forward path
        assert versioning.EVENT_SCHEMA_VERSION - 1 in UPCASTERS


class TestNoHardcodedVersions:
    def test_source_modules_do_not_define_version_literals(self):
        """Freeze guard: version numbers live in VERSION, not in source files."""
        pattern = re.compile(r"^(?:[A-Z_]+_)?VERSION\s*=\s*\d", re.MULTILINE)
        offenders = []
        for module in sorted((REPO_ROOT / "foundry").glob("*.py")):
            text = module.read_text(encoding="utf-8")
            if pattern.search(text):
                offenders.append(module.name)
        assert offenders == []
