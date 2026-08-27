# ADR-0005: Tracks are InformationObjects; domain modules are query-only

- Status: Accepted
- Date: 2026-08-27
- Deciders: Ontology / platform engineering
- Tags: ontology, domain-model, tracking

## Context

Phase 4 introduces the first domain vertical:
`Observation → Track → Entity → Organization → Location`.
The pivotal modeling question is what a **Track** fundamentally *is*:

1. `core:Event` — something that happens in the world;
2. `core:InformationObject` — derived data produced by a system;
3. `core:PhysicalObject` — a physical thing.

A track is the output of a correlation process over observations (a
hypothesis about one entity's movements). It can be created, revised and
retracted by software without anything in the world changing. Modeling it as
an Event would corrupt event-sourcing semantics; modeling it as a physical
object is plainly wrong.

A second decision is needed simultaneously: should the new sensor/tracking
terms be wired into the `foundry` ingestion pipeline now?

## Decision

1. **`tracking:Track ⊑ core:InformationObject`.** A track is a derived,
   revisable information product. Identity/temporal semantics still apply to
   *the entity it hypothesizes about*, not to the track itself.
2. **Extension predicates may specialize core classes downward**
   (`sensor:detectedBy : Observation → Sensor`). This is sound because domain
   modules import core — the DAG invariant makes the direction impossible to
   violate.
3. **Domain modules are query-only for now.** No ingestion mapping is added
   until a real data source exists (`benchmarks/datasets/domain_tracking.ttl`
   is benchmark scaffolding, deliberately never fed through
   `foundry.ingestion`). Wiring premature mappings would put fake producers
   on the critical path.
4. Sensor provenance is split from source provenance: `core:hasSource`
   keeps its canonical meaning; `sensor:detectedBy` adds *physical
   detection* without overloading source semantics.

## Consequences

- Reasoning tests assert the negative invariant explicitly: asserting
  `trackOf` must never re-type the target entity, and `Track` closures must
  exclude `Event`.
- When real tracking feeds arrive (Phase 6+), the mapping contract goes into
  `ontology/mappings/` with its own review; the ontology layer itself does
  not change.
- SHACL contracts for tracks/sensors land in `shapes/domain_shapes.ttl`
  (exactly-one `trackOf`, at-least-one `derivedFrom`, stable `hasTrackId`)
  so that any future producer is validated before entering the graph.

## References

- Roadmap Phase 4 (first domain module)
- Definition of Done for ontology modules (`CODING_CONVENTIONS.md`)
