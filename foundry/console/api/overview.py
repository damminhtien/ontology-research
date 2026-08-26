"""Aggregated dashboard endpoint for the console landing view."""

from __future__ import annotations

import subprocess
import sys

import manage_ontology as mgmt
from cq_runner import run_competency_queries
from fastapi import APIRouter

from foundry.console.api.monitor import event_stats

router = APIRouter(tags=["overview"])


def _dag_status() -> dict:
    """Run the dependency DAG checker; returns exit status only."""
    tool = mgmt.REPO_ROOT / "tools" / "check_dependency_dag.py"
    try:
        result = subprocess.run(
            [sys.executable, tool.as_posix()],
            capture_output=True,
            text=True,
            check=False,
            cwd=mgmt.REPO_ROOT.as_posix(),
        )
    except OSError as exc:
        return {"passed": False, "error": str(exc)}
    return {
        "passed": result.returncode == 0,
        "output": result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "",
    }


@router.get("/overview")
def overview() -> dict:
    """One-shot aggregate powering the console dashboard view."""
    statuses = mgmt.collect_version_status()
    releases = mgmt.load_releases(mgmt.registry_dir())
    stability = {
        entry[0]: {"stability": entry[1], "breaking": entry[2], "releases": entry[3]}
        for entry in mgmt.stability_report(releases)
    }

    from ontology_utils import describe_classes, describe_properties

    modules = []
    for status in statuses:
        module_file = mgmt.REPO_ROOT / status["file"]
        graph = mgmt.load_graph(module_file)
        classes = describe_classes(graph)
        props = describe_properties(graph)
        module_releases = [r for r in releases if r.get("module_iri") == status["module_iri"]]
        modules.append(
            {
                "name": status["module"],
                "file": status["file"],
                "version": status["declared_version"],
                "latest_version": status["latest_version"],
                "status": status["status"],
                "classes": len(classes),
                "properties": len(props),
                "last_release": max((r["date"] for r in module_releases), default=None),
            }
        )

    cq_results = run_competency_queries()
    failed_cq = [r["id"] for r in cq_results if not r["passed"]]

    return {
        "modules": modules,
        "version_check": {
            "passed": all(s["status"] != "error" for s in statuses),
            "statuses": statuses,
        },
        "dag_check": _dag_status(),
        "stability": {
            "modules": [
                {
                    "module": mgmt.local_name(m),
                    **values,
                    "ok": values["stability"]
                    >= (
                        mgmt.STABILITY_THRESHOLD_CORE
                        if m.endswith("/core")
                        else mgmt.STABILITY_THRESHOLD_DEFAULT
                    ),
                }
                for m, values in stability.items()
            ]
        },
        "events": event_stats(),
        "cq": {
            "total": len(cq_results),
            "failed": len(failed_cq),
            "failed_ids": failed_cq,
        },
    }
