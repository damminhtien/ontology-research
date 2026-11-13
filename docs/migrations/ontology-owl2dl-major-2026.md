# OWL 2 DL Major Update: Revalidation and Recovery

The assertion module is now version `2.0.0`, and tracking is version `1.0.0`.
These major versions replace generic `rdfs:Resource` domain/range declarations
with explicit evidence-reference and measurement classes. The assertion
predicate range is `owl:Thing`; SHACL still requires the recorded predicate to
be an IRI, and ingestion still checks that it names a declared, assertable
property.

The event payloads and their identifiers have not changed. Historical inline
evidence and measurement blank nodes remain valid RDF: their property use entails
the new class membership under the declared domain and range axioms. Consumers
that do not apply RDFS or OWL entailment may keep seeing those resources as
untyped blank nodes. Consumers that need the explicit types should rebuild their
derived RDF projection from the append-only event log with the new ontology.

## Preflight

Before activating either new ontology version against a complete historical
source set:

1. Preserve the old read-store snapshot and record its event-log sequence and
   ontology versions.
2. Rebuild a candidate projection from the authoritative event log with the
   new ontology. Do not edit or rewrite events.
3. Run `make check` and `make conformance` on the seed fixtures, then run the
   same SHACL and competency-query checks against the candidate projection.
4. Compare source/evidence links, measurement values, predicate IRIs and CQ
   results. Publish the candidate only after the full source scan passes.
5. Record the complete preflight inputs and result before recording releases in
   `registry/releases.json`.

The release registry and historical snapshots are intentionally unchanged by
this source update. The seed fixture run is a compatibility check, not a
historical-data preflight.

## Recovery

If validation or queries regress before activation, discard the candidate
projection and keep serving the previous snapshot. If regression appears after
activation, switch readers back to the preserved snapshot and its prior
ontology version, then rebuild a corrected candidate from the unchanged event
log. Never roll back by deleting or rewriting events. Once a major release is
recorded, use the migration workflow and its full-source impact report before
publishing a subsequent ontology version.
