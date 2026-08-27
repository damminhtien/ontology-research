"""Data-plane monitoring endpoints: event log, SHACL gate, competency queries."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from cq_runner import run_competency_queries
from fastapi import APIRouter, Query
from ontology_utils import find_module_files, materialize_type_closure
from pyshacl import validate as shacl_validate
from rdflib import RDF, Graph
from rdflib.namespace import SH

from foundry.events import EventLog

REPO_ROOT = Path(__file__).resolve().parents[3]

EVENT_LOG_ENV = "FOUNDRY_EVENT_LOG"

router = APIRouter(prefix="/monitor", tags=["monitoring"])


def event_log_path() -> Path:
    """Resolve the event log path (FOUNDRY_EVENT_LOG or data/events.jsonl)."""
    configured = os.environ.get(EVENT_LOG_ENV)
    return Path(configured) if configured else REPO_ROOT / "data" / "events.jsonl"


def _validation_graphs() -> tuple[Graph, Graph, list[str], list[str]]:
    """Build the full data graph and shapes graph the SHACL gate runs on."""
    data = Graph()
    data_files: list[str] = []
    for path in find_module_files():
        data.parse(path.as_posix(), format="turtle")
    for path in sorted((REPO_ROOT / "benchmarks" / "datasets").glob("*.ttl")):
        data.parse(path.as_posix(), format="turtle")
        data_files.append(path.name)
    materialize_type_closure(data)

    shapes = Graph()
    shapes_files: list[str] = []
    for path in sorted((REPO_ROOT / "shapes").glob("*.ttl")):
        shapes.parse(path.as_posix(), format="turtle")
        shapes_files.append(path.name)
    return data, shapes, data_files, shapes_files


@router.get("/events/stats")
def event_stats() -> dict:
    """Aggregate the event log: total count and counts per event type."""
    path = event_log_path()
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "total": 0,
            "by_type": {},
            "message": "no event log yet; run tools/seed_console_data.py",
        }
    events = EventLog(path).read_all()
    by_type: dict[str, int] = {}
    for event in events:
        by_type[event.event_type] = by_type.get(event.event_type, 0) + 1
    return {"exists": True, "path": str(path), "total": len(events), "by_type": by_type}


@router.get("/events/recent")
def recent_events(limit: Annotated[int, Query(le=500)]) -> dict:
    """Return the most recent events, newest first."""
    limit_value = min(limit, 500)
    path = event_log_path()
    events = EventLog(path).read_all() if path.exists() else []
    recent = [
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at,
            "payload": event.payload,
        }
        for event in reversed(events[-limit_value:])
    ]
    return {"total": len(events), "returned": len(recent), "events": recent}


@router.get("/validation")
def validation() -> dict:
    """Run the SHACL gate (all shapes) over all benchmark datasets."""
    data, shapes, data_files, shapes_files = _validation_graphs()

    conforms, results_graph, _results_text = shacl_validate(
        data_graph=data,
        shacl_graph=shapes,
        inference="none",
        advanced=True,
    )

    violations = []
    if not conforms:
        for report in results_graph.subjects(RDF.type, SH.ValidationReport):
            for result_node in results_graph.objects(report, SH.result):
                violations.append(
                    {
                        "focus_node": str(results_graph.value(result_node, SH.focusNode) or ""),
                        "path": str(results_graph.value(result_node, SH.resultPath) or ""),
                        "message": str(results_graph.value(result_node, SH.resultMessage) or ""),
                    }
                )

    return {
        "data": data_files,
        "shapes": shapes_files,
        "conforms": conforms,
        "violation_count": len(violations),
        "violations": violations,
    }


@router.get("/cq")
def competency_queries() -> dict:
    """Run every competency query against the seed dataset and compare ground truth."""
    results = run_competency_queries()
    passed = sum(1 for r in results if r["passed"])
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "queries": results,
    }
