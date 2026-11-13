"""Tests for migration script generation and the alignment registry (Phase 5)."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import manage_ontology as mgmt
import pyarrow as pa
import pyarrow.parquet as pq
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
            target="https://damminhtien.github.io/ontology-research/ontology/core#operates",
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
            target="https://damminhtien.github.io/ontology-research/ontology/core#Facility",
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
            target="https://damminhtien.github.io/ontology-research/ontology/core#operates",
            detail="changed property definition",
            field_changes=["domain: Artifact -> Entity"],
        )
        path = mgmt.generate_migration_script("core", "0.1.0", "1.0.0", [changed], tmp_path)
        assert "Re-run SHACL validation" in path.read_text(encoding="utf-8")


class TestScanDataImpact:
    def test_scans_jsonl_event_records_by_changed_term(self, tmp_path):
        term = "https://example.org/ontology#locatedAt"
        source = tmp_path / "history.jsonl"
        source.write_text(
            json.dumps({"payload": {"predicate_iri": term}})
            + "\n"
            + json.dumps({"payload": {"predicate_iri": "https://example.org/ontology#memberOf"}})
            + "\n",
            encoding="utf-8",
        )

        result = mgmt.scan_data_impact([term], [source])

        assert result["status"] == "scanned"
        assert result["items_scanned"] == 2
        assert result["matched_items"] == 1
        assert result["matches_by_term"] == {term: 1}

    def test_requires_explicit_authoritative_sources(self):
        with pytest.raises(ValueError, match="every authoritative historical data source"):
            mgmt.scan_data_impact(["https://example.org/ontology#locatedAt"], [])

    def test_scans_only_manifest_registered_parquet_files(self, tmp_path):
        term = "https://example.org/ontology#locatedAt"
        lake_root = tmp_path / "lake"
        registered = lake_root / "event_date=2026-11-12" / "registered.parquet"
        orphan = lake_root / "event_date=2026-11-12" / "orphan.parquet"
        registered.parent.mkdir(parents=True)
        pq.write_table(
            pa.table(
                {
                    "event_type": ["AssertionMade"],
                    "payload_json": [json.dumps({"predicate_iri": term})],
                }
            ),
            registered,
        )
        pq.write_table(
            pa.table(
                {
                    "event_type": ["AssertionMade"],
                    "payload_json": [json.dumps({"predicate_iri": term})],
                }
            ),
            orphan,
        )
        (lake_root / "manifest.json").write_text(
            json.dumps({"files": [{"path": "event_date=2026-11-12/registered.parquet"}]}),
            encoding="utf-8",
        )

        result = mgmt.scan_data_impact([term], [lake_root])

        assert result["files_scanned"] == 1
        assert result["items_scanned"] == 1
        assert result["matched_items"] == 1

    def test_rejects_missing_parquet_file_registered_by_lake_manifest(self, tmp_path):
        lake_root = tmp_path / "lake"
        lake_root.mkdir()
        (lake_root / "manifest.json").write_text(
            json.dumps({"files": [{"path": "missing.parquet"}]}), encoding="utf-8"
        )

        with pytest.raises(ValueError, match="manifest Parquet file is missing"):
            mgmt.scan_data_impact(["https://example.org/ontology#locatedAt"], [lake_root])

    def test_rejects_malformed_lake_manifest(self, tmp_path):
        lake_root = tmp_path / "lake"
        lake_root.mkdir()
        (lake_root / "manifest.json").write_text("[]", encoding="utf-8")

        with pytest.raises(ValueError, match="invalid lake manifest files list"):
            mgmt.scan_data_impact(["https://example.org/ontology#locatedAt"], [lake_root])


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
                            "module_iri": "https://damminhtien.github.io/ontology-research/ontology/core",
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
        history = tmp_path / "production.jsonl"
        history.write_text(
            json.dumps(
                {
                    "event_type": "AssertionMade",
                    "payload": {
                        "predicate_iri": (
                            "https://damminhtien.github.io/ontology-research/ontology/core#operates"
                        )
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        code = mgmt.cmd_migrate(argparse_namespace(module="core", data=[history]))
        out = capsys.readouterr().out
        assert code == 0
        assert "Generated" in out
        script = Path(out.split("Generated ")[1].splitlines()[0].strip())
        assert "DELETE" in script.read_text(encoding="utf-8")
        assert (tmp_path / "migrations" / "core").is_dir()
        report_path = script.with_suffix(".impact.json")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["compatibility"] == "BREAKING"
        assert report["historical_data"]["matched_items"] == 1
        assert report["historical_data"]["sources"] == [str(history.resolve())]
        assert report["migration_script"] == mgmt._display_path(script.resolve())
        assert report["migration_script_sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()

    def test_refuses_breaking_migration_without_data_scan(self, tmp_path, monkeypatch, capsys):
        self._released_repo(tmp_path, monkeypatch)
        broken = tmp_path / "core_broken.ttl"
        text = CORE_ONTOLOGY.read_text(encoding="utf-8")
        start = text.index("core:operates a owl:ObjectProperty")
        end = text.index(" .", start) + 2
        broken.write_text(text[:start] + text[end:], encoding="utf-8")
        monkeypatch.setattr(mgmt, "find_module_files", lambda: [broken])

        code = mgmt.cmd_migrate(argparse_namespace(module="core"))

        assert code == 1
        assert "requires every authoritative historical data source" in capsys.readouterr().out
        assert not (tmp_path / "migrations" / "core").exists()


class TestAlignmentRegistry:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "alignments.json"
        mgmt.save_alignments(
            path,
            [
                {
                    "source": "s",
                    "target": "t",
                    "relation": "exactMatch",
                    "mode": "reference",
                    "source_type": "class",
                    "target_type": "class",
                    "note": "",
                    "added": "2026-01-01T00:00:00Z",
                }
            ],
        )
        assert len(mgmt.load_alignments(path)) == 1
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["version"] == 2

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
                    "source": "https://damminhtien.github.io/ontology-research/ontology/core#Ghost",
                    "target": "https://schema.org/Thing",
                    "relation": "closeMatch",
                    "mode": "reference",
                    "source_type": "class",
                    "target_type": "class",
                    "note": "",
                    "added": "2026-01-01T00:00:00Z",
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
            source="https://damminhtien.github.io/ontology-research/ontology/core#NoSuchTerm",
            target="https://schema.org/Thing",
            relation="closeMatch",
            mode="reference",
            source_type="class",
            target_type="class",
            predicate=None,
            evidence=None,
            verified_identity=False,
            note="",
        )
        assert mgmt.cmd_align_add(args) == 1
        assert "no longer defined" in capsys.readouterr().out

    def test_align_add_rejects_duplicate(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "alignments.json"
        monkeypatch.setattr(mgmt, "alignments_file", lambda: path)
        monkeypatch.setattr(mgmt, "find_module_files", lambda: [CORE_ONTOLOGY])
        args = argparse_namespace(
            source="https://damminhtien.github.io/ontology-research/ontology/core#Person",
            target="https://schema.org/Person",
            relation="exactMatch",
            mode="reference",
            source_type="class",
            target_type="class",
            predicate=None,
            evidence=None,
            verified_identity=False,
            note="dup",
        )
        assert mgmt.cmd_align_add(args) == 0
        assert mgmt.cmd_align_add(args) == 1
        assert "duplicates an earlier" in capsys.readouterr().out

    def test_export_mapping_requires_matching_semantics(self):
        person = "https://damminhtien.github.io/ontology-research/ontology/core#Person"
        mapping = {
            "source": person,
            "target": "https://schema.org/Person",
            "relation": "exactMatch",
            "mode": "export",
            "source_type": "class",
            "target_type": "class",
            "predicate": "http://www.w3.org/2004/02/skos/core#exactMatch",
            "evidence": "https://example.org/mapping-evidence",
            "note": "Incorrect class-to-concept export.",
            "added": "2026-01-01T00:00:00Z",
        }
        errors = mgmt.validate_alignments(
            [mapping], mgmt.collect_defined_term_kinds([CORE_ONTOLOGY])
        )
        assert any("SKOS matches require SKOS concept endpoints" in error for error in errors)

    def test_same_as_requires_verified_individuals_and_evidence(self):
        mapping = {
            "source": "https://damminhtien.github.io/ontology-research/ontology/core#Person",
            "target": "https://example.org/person/1",
            "relation": "sameAs",
            "mode": "export",
            "source_type": "class",
            "target_type": "individual",
            "predicate": "http://www.w3.org/2002/07/owl#sameAs",
            "evidence": "https://example.org/mapping-evidence",
            "note": "",
            "added": "2026-01-01T00:00:00Z",
        }
        errors = mgmt.validate_alignments(
            [mapping], mgmt.collect_defined_term_kinds([CORE_ONTOLOGY])
        )
        assert any("sameAs requires individual endpoints" in error for error in errors)
        assert any("sameAs requires verified_identity=true" in error for error in errors)

    def test_export_alignment_rejects_invalid_iri_and_timestamp(self):
        mapping = {
            "source": "not an iri",
            "target": "https://schema.org/Person",
            "relation": "subClassOf",
            "mode": "export",
            "source_type": "class",
            "target_type": "class",
            "predicate": "http://www.w3.org/2004/02/skos/core#exactMatch",
            "evidence": "not-an-iri",
            "note": "",
            "added": "yesterday",
        }
        errors = mgmt.validate_alignments([mapping], {})
        assert any("source must be an absolute IRI" in error for error in errors)
        assert any("ISO-8601" in error for error in errors)
        assert any("predicate does not match relation" in error for error in errors)
        assert any("absolute evidence IRI" in error for error in errors)
