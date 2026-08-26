"""Ontology model endpoints: model, hierarchy tree, search, term detail."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from manage_ontology import blast_radius_for_terms
from ontology_utils import find_module_files, local_name
from visualize_ontology import build_model, build_tree

router = APIRouter(prefix="/ontology", tags=["ontology"])


def _model() -> dict:
    """Load the full class/property model of all registered modules."""
    return build_model(find_module_files())


@router.get("/model")
def get_model() -> dict:
    """Return every class and property declared across registered modules."""
    return _model()


@router.get("/tree")
def get_tree() -> dict:
    """Return the class hierarchy as a nested tree for D3 rendering."""
    return {"tree": build_tree(_model())}


@router.get("/search")
def search(q: Annotated[str, Query(min_length=1)]) -> dict:
    """Filter classes and properties by name substring (case-insensitive)."""
    needle = q.casefold().strip()
    model = _model()
    classes = [
        {
            "iri": iri,
            "name": local_name(iri),
            "label": desc["label"],
            "comment": desc["comment"],
        }
        for iri, desc in sorted(model["classes"].items())
        if needle in local_name(iri).casefold() or needle in (desc["label"] or "").casefold()
    ]
    properties = [
        {
            "iri": prop["iri"],
            "name": local_name(prop["iri"]),
            "kind": prop["kind"],
            "domain": local_name(prop["domain"]) or "-",
            "range": local_name(prop["range"]) or "-",
            "label": prop["label"],
        }
        for prop in sorted(model["properties"], key=lambda p: p["iri"])
        if needle in local_name(prop["iri"]).casefold()
    ]
    return {"query": q, "classes": classes, "properties": properties}


@router.get("/terms/{local}")
def term_detail(local: str) -> dict:
    """Detail view for one term: descriptors plus its blast radius."""
    model = _model()
    property_iris = [p["iri"] for p in model["properties"]]
    matches = [iri for iri in list(model["classes"]) + property_iris if local_name(iri) == local]
    if not matches:
        raise HTTPException(status_code=404, detail=f"term '{local}' not found")

    iri = matches[0]
    detail: dict = {
        "iri": iri,
        "name": local,
        "kind": "class" if iri in model["classes"] else "property",
        "blast_radius": blast_radius_for_terms([local]),
    }
    if iri in model["classes"]:
        desc = model["classes"][iri]
        detail.update(
            label=desc["label"],
            comment=desc["comment"],
            parents=[local_name(p) for p in desc["parents"]],
            properties=desc["properties"],
        )
    else:
        prop = next(p for p in model["properties"] if p["iri"] == iri)
        detail.update(
            label=prop["label"],
            kind_of_property=prop["kind"],
            domain=local_name(prop["domain"]),
            range=local_name(prop["range"]),
        )
    return detail
