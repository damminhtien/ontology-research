"""Release registry endpoints: releases, version check, stability."""

from __future__ import annotations

from pathlib import Path

import manage_ontology as mgmt
from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["governance"])


def _registry() -> Path:
    """Resolve the release registry directory."""
    return mgmt.registry_dir()


def _module_file(module: str) -> Path | None:
    """Locate the module file whose ontology local name matches ``module``."""
    for path in mgmt.find_module_files():
        try:
            iri, _declared = mgmt.module_identity(mgmt.load_graph(path))
        except ValueError:
            continue
        if mgmt.local_name(iri) == module:
            return path
    return None


def _change_dicts(changes: list) -> list[dict]:
    """Serialize Change objects for JSON responses."""
    return [
        {
            "kind": change.kind,
            "target": change.target,
            "name": mgmt.local_name(change.target),
            "detail": change.detail,
            "severity": change.severity,
        }
        for change in changes
    ]


@router.get("/versions/check")
def versions_check() -> dict:
    """Run the version-consistency matrix and return per-module statuses."""
    statuses = mgmt.collect_version_status()
    failures = sum(1 for entry in statuses if entry["status"] == "error")
    return {"passed": failures == 0, "failures": failures, "modules": statuses}


@router.get("/stability")
def stability() -> dict:
    """Stability per module with roadmap threshold verdicts."""
    entries = mgmt.stability_report(mgmt.load_releases(_registry()))
    modules = []
    for module_iri, score, breaking, total in entries:
        threshold = (
            mgmt.STABILITY_THRESHOLD_CORE
            if module_iri.endswith("/core")
            else mgmt.STABILITY_THRESHOLD_DEFAULT
        )
        modules.append(
            {
                "module": mgmt.local_name(module_iri),
                "stability": score,
                "breaking": breaking,
                "releases": total,
                "threshold": threshold,
                "ok": score >= threshold,
            }
        )
    return {"modules": modules}


# Static routes are declared before the dynamic /{module}/... routes so that
# FastAPI matches "/versions/check" before treating "versions" as a module.


@router.get("/releases")
def all_releases() -> dict:
    """All releases grouped by module, newest version first."""
    grouped: dict[str, list] = {}
    for release in mgmt.load_releases(_registry()):
        grouped.setdefault(release["module_iri"], []).append(release)
    return {
        "total": sum(len(entries) for entries in grouped.values()),
        "modules": {
            module_iri: sorted(
                entries, key=lambda r: mgmt.parse_version(r["version"]), reverse=True
            )
            for module_iri, entries in sorted(grouped.items())
        },
    }


@router.get("/releases/{module}/diff")
def diff_versions(module: str, from_version: str, to_version: str) -> dict:
    """Semantic diff between two released snapshot versions of a module."""
    registry = _registry()
    try:
        old_terms = mgmt.load_snapshot_terms(registry, module, from_version)
        new_terms = mgmt.load_snapshot_terms(registry, module, to_version)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    changes = mgmt.diff_snapshots(old_terms, new_terms)
    return {
        "module": module,
        "from_version": from_version,
        "to_version": to_version,
        "suggested_bump": mgmt.highest_severity(changes),
        "changes": _change_dicts(changes),
    }


@router.get("/releases/{module}/pending")
def pending_release(module: str) -> dict:
    """What a release of this module would contain right now.

    Compares the current module file against the latest released snapshot and
    reports changes, suggested bump level and blast radius - the UI equivalent
    of `release --dry-run`.
    """
    path = _module_file(module)
    if path is None:
        raise HTTPException(status_code=404, detail=f"unknown module '{module}'")

    registry = _registry()
    graph = mgmt.load_graph(path)
    iri, declared = mgmt.module_identity(graph)
    latest = mgmt.latest_release(mgmt.load_releases(registry), iri)
    if latest is None:
        return {
            "module": module,
            "has_baseline": False,
            "message": f"never released; 'release {module}' would record {declared} as baseline",
        }

    latest_terms = mgmt.load_snapshot_terms(registry, module, latest["version"])
    current_terms = mgmt.snapshot(graph)
    changes = mgmt.diff_snapshots(latest_terms, current_terms)
    severity = mgmt.highest_severity(changes) if changes else mgmt.SEVERITY_NONE
    names = mgmt.changed_term_names(changes)
    radius = mgmt.blast_radius_for_terms(names)

    status, message = mgmt.evaluate_module_version(latest, latest_terms, declared, current_terms)
    return {
        "module": module,
        "has_baseline": True,
        "latest_version": latest["version"],
        "declared_version": declared,
        "severity": severity,
        "suggested_version": (mgmt.next_version(latest["version"], severity) if changes else None),
        "status": status,
        "status_message": message,
        "changes": _change_dicts(changes),
        "blast_radius": radius,
    }
