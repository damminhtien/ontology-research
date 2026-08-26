"""SHACL conformance tests for the semantic contracts (Phase 1).

Contract under test (`shapes/core_shapes.ttl`):
  * Observation   : exactly one atTime, >= 1 source, >= 1 observed entity,
                    confidence in [0,1] when present;
  * LocationAssertion : exactly one subject entity, exactly one location,
                    exactly one validFrom, >= 1 source;
  * Named things (agents/artifacts/locations/sources) : >= 1 name.
"""

from __future__ import annotations

import pytest
from conftest import CORE, CORE_ONTOLOGY, SAMPLE_DATA, SHAPES_FILE
from ontology_utils import materialize_type_closure
from pyshacl import validate
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

ex = Namespace("https://data.example/entity/")


def _shapes() -> Graph:
    g = Graph()
    g.parse(SHAPES_FILE.as_posix(), format="turtle")
    return g


def _run_shacl(data: Graph) -> tuple[bool, str]:
    # Expand rdf:type over rdfs:subClassOf so sh:class constraints accept
    # subclass instances (same contract as tools/validate.py).
    materialize_type_closure(data)
    conforms, _, results_text = validate(
        data_graph=data,
        shacl_graph=_shapes(),
        inference="none",
        advanced=True,
    )
    return conforms, results_text


@pytest.fixture(scope="module")
def sample_data() -> Graph:
    g = Graph()
    g.parse(CORE_ONTOLOGY.as_posix(), format="turtle")
    g.parse(SAMPLE_DATA.as_posix(), format="turtle")
    return g


class TestShaclConformance:
    def test_seed_dataset_conforms(self, sample_data):
        conforms, report = _run_shacl(sample_data)
        assert conforms, f"seed dataset must conform to core shapes:\n{report}"

    def test_observation_missing_time_and_source_fails(self):
        g = Graph()
        g.parse(CORE_ONTOLOGY.as_posix(), format="turtle")
        bad = ex["obs-bad"]
        g.add((bad, RDF.type, URIRef(CORE + "Observation")))
        g.add((bad, URIRef(CORE + "observes"), ex["patrol-01"]))
        # missing atTime, missing hasSource
        g.add(
            (
                bad,
                URIRef(CORE + "hasConfidence"),
                Literal("1.50", datatype=XSD.decimal),  # out of [0,1]
            )
        )

        conforms, report = _run_shacl(g)
        assert not conforms, "observation without atTime/source must be rejected"

        for path_iri in ("atTime", "hasSource", "hasConfidence"):
            assert path_iri in report, f"expected violation on core:{path_iri}"

    def test_location_assertion_requires_provenance(self):
        g = Graph()
        g.parse(CORE_ONTOLOGY.as_posix(), format="turtle")
        bad = ex["la-bad"]
        g.add((bad, RDF.type, URIRef(CORE + "LocationAssertion")))
        g.add((bad, URIRef(CORE + "describes"), ex["patrol-01"]))
        g.add((bad, URIRef(CORE + "locatedAt"), ex["loc-cam-ranh"]))
        g.add(
            (
                bad,
                URIRef(CORE + "validFrom"),
                Literal("2026-01-01T00:00:00Z", datatype=XSD.dateTime),
            )
        )
        # hasSource intentionally absent

        conforms, _ = _run_shacl(g)
        assert not conforms, "location assertion without a source must be rejected"
