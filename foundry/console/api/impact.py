"""Blast-radius analysis endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from manage_ontology import blast_radius_for_terms, local_name

router = APIRouter(tags=["impact"])


@router.get("/impact")
def blast_radius(
    term: Annotated[str, Query(min_length=1, description="term local name or full IRI")],
) -> dict:
    """Count and list consumers of a term across modules/queries/applications."""
    name = local_name(term) if "://" in term else term
    radius = blast_radius_for_terms([name])
    score = sum(len(files) for files in radius.values())
    return {"term": name, "score": score, **radius}
