# ADR-0004: CQRS projector + materialized read model for operational queries

- Status: accepted
- Date: 2026-08-27

## Context

Operational queries ("where is platform X now?", "where was it at 06:00?") must
be fast and stable even while the semantic model keeps evolving. Putting every
query through the OWL/SHACL ontology or a SPARQL endpoint would make the
operational path pay for semantic complexity it does not need, violating the
project's central architectural rule.

We therefore separate the write model (append-only canonical events) from the
read model (a materialized, query-optimized projection).

## Decision

Adopt CQRS:

- **Write model**: immutable `SemanticEvent` stream in `foundry/events.py`.
  Every change is an event; corrections are new events that supersede old ones.
- **Projector** (`foundry/projector.py`): folds the event stream into a
  `ReadModel`. Replay is deterministic: events are sorted by `(occurred_at,
  event_id)` and each `apply()` is idempotent. Replaying the same log twice must
  produce structurally identical output.
- **ReadModel** (`foundry/readmodel.py`): in-memory indexes that answer the hot
  operational queries directly: entity lookup, current location, temporal
  `as-of`, reverse `entities_at`, and projection stats. No ontology, no SPARQL.

Forward-compatibility rules (so newer writers do not break older projections):

1. Unknown event types are counted and skipped, never fatal.
2. The projector only reads the fields it knows; extra payload fields are
   ignored.
3. Every event carries `schema_version`; the projector validates only the
   subset it understands.
4. Renaming or removing a field that the projector consumes is a breaking
   change and requires a major-version bump + migration.

Phase 3 uses in-memory structures; the `ReadModel` interface is stable so a
real store (graph DB, relational projection tables, search index) can replace
it in Phase 6 without changing callers.

## Consequences

- (+) Operational queries are O(1)-ish and independent of ontology complexity.
- (+) Read models can be rebuilt, branched, or A/B-tested by replaying different
  event subsets.
- (+) New event types can be emitted before the projector understands them;
  skips keep the model safe.
- (-) Read model lags behind the log; `ProjectionLag < 5s` is an operational SLO.
- (-) Projector changes require careful replay regression tests because they
  re-interpret history.
- The current console exposes projection stats and benchmark latency so lag
  and SLO health are visible in production.
