# Operational OSINT Query Contract

Operational queries use `foundry.query_service.QueryService` over an
`OperationalQueryAdapter`. PostgreSQL executes these operations against indexed
read projections. The in-memory `ReadModel` implements the same adapter surface
for tests and offline development; API handlers call the service rather than
reading implementation dictionaries.

Semantic competency questions remain a separate query path. RDFLib, Jena and
Oxigraph execute SPARQL against the same immutable ontology and dataset
fixtures. `benchmarks/expected_results/*.json` remains the answer contract for
all 41 competency queries. Query adapters return projected variable names and
rows so that the same expected-result check can run independently of FastAPI.

## OSINT Workload

| ID | Question | Service path | Adapter operation | Bounds |
|---|---|---|---|---|
| OSINT-01 | Which entity has this canonical identifier? | `entity_lookup` | `get_entity` | One row; 10–5000 ms timeout |
| OSINT-02 | Which entities have this exact primary name or alias? | `entity_search` | `find_entities_page` | Stable sort; page size 1–100; normalized exact match |
| OSINT-03 | What entity relationships are active from this subject? | `relationships_as_of` | `assertions_as_of_page` | Half-open valid time; optional absolute predicate IRI |
| OSINT-04 | Which entities are reachable within one to three relationship hops? | `traverse_relationships` | `assertions_as_of_page` per frontier entity | 1–3 hops; 500 nodes; 2000 edges; page size 1–100 |
| OSINT-05 | What was the unique location of an entity at time T? | `location_as_of` | `location_as_of` | Half-open valid time; conflicts return no unique value |
| OSINT-06 | Which entities were uniquely at a location at time T? | `entities_at_location` | `entities_at_page` | Stable entity-ID order; page size 1–100 |
| OSINT-07 | What sources, locators and text spans support an assertion? | `assertion_provenance` | `get_assertion` | One assertion by stable ID; API applies source scope |
| OSINT-08 | Which different values conflict for a subject and predicate at time T? | `contradictions` | `assertions_as_of_page` | Absolute predicate IRI; scans at most 1000 assertions |
| OSINT-09 | What observation and source lineage belongs to this track? | `track_provenance` | `get_track` | One track by stable ID; API applies source scope |
| OSINT-10 | Which track hypotheses are associated with this entity? | `tracks_for_entity` | `tracks_page` | Stable track-ID order; page size 1–100 |

## Limits and Ordering

- Default page size is 50; maximum page size is 100. Offsets are limited to
  100,000. Entity search accepts only `entity_id`, `name`, `entity_type`,
  `first_seen` or `last_event_time` as sort keys, followed by entity ID as a
  stable tie breaker.
- PostgreSQL operations use a transaction-local `statement_timeout`; the
  accepted range is 10–5000 ms and the default is 250 ms. The in-memory adapter
  accepts the same contract but cannot interrupt a Python call. PostgreSQL
  cancellation is returned by the FastAPI layer as HTTP 504.
- Relationship traversal is bounded to three hops, 500 distinct visited nodes
  and 2000 examined assertions. Responses expose `truncated` and `has_more` when
  a safety ceiling clips the result.
- Contradictions compare distinct typed object values at the requested
  valid-time instant. Multiple sources supporting the same value are not a
  contradiction. Recorded time does not replace valid time.
- Source visibility is enforced at the API boundary with the existing
  `FOUNDRY_PROVENANCE_TOKENS` scope. Query results from a semantic store are
  optional and never become a write path or an implicit fallback for operational
  reads.

## Semantic Query Evaluation

The 41 CQ expected-result files are run through a minimal SELECT adapter
contract. `make oxigraph-eval` verifies those same expected rows on RDFLib,
Oxigraph and Jena. `foundry.oxigraph_snapshot` supplies immutable, rebuildable
Oxigraph snapshots from the event log. The operational comparison command
`make query-adapter-eval QUERY_ADAPTER_ARGS="--event-log PATH --snapshot-root PATH"`
builds or refreshes that snapshot, reads the existing PostgreSQL projection
without resetting it, and runs matched entity lookup, as-of relationship and
assertion-provenance probes. Set `FOUNDRY_DATABASE_URL` for the operational
database. PostgreSQL must have projected the event-log sequence first. The
report checks result equivalence and records latency boundaries; PostgreSQL
timings include `QueryService` and psycopg, while Oxigraph timings use direct
`Store.query`, so those latency figures are not engine-equivalent.

This contract does not define SPARQL federation, query rewriting across stores,
or a production traffic switch to Oxigraph.
