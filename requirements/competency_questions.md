# Competency Questions

Workload-driven contract for the current semantic model. There are **41 executable
CQs**, each with a fixed dataset, SPARQL template and expected rows. The contract
contains only questions from the current location, identity, provenance,
correction, contradiction and tracking workload.

| ID | Group | Workload question | Query / expected result | Dataset | p95 target | Status |
|----|-------|------------------|------------------------|---------|-----------:|--------|
| CQ-001 | Q1 | Find an entity by canonical identifier. | `q01_entity_lookup.rq` / `q01_entity_lookup.json` | `sample_data.ttl` | < 50ms | Required |
| CQ-003 | Q2 | Which organization operates a platform? | `q02_one_hop.rq` / `q02_one_hop.json` | `sample_data.ttl` | < 100ms | Required |
| CQ-006 | Q3 | Which platforms are operated by organizations located in a region? | `q03_region_platforms.rq` / `q03_region_platforms.json` | `sample_data.ttl` | < 300ms | Required |
| CQ-008 | Q4 | Where was an entity at a given instant? | `q04_temporal_asof.rq` / `q04_temporal_asof.json` | `sample_data.ttl` | < 500ms | Required |
| CQ-010 | Q5 | Which sources support an assertion? | `q05_provenance.rq` / `q05_provenance.json` | `sample_data.ttl` | < 500ms | Required |
| CQ-012 | Q6 | Count entities by their asserted class. | `q06_analytical_count.rq` / `q06_analytical_count.json` | `sample_data.ttl` | n/a (OLAP) | Required |
| CQ-013 | Q3 | Which organizations report transitively to an organization? | `q07_org_subordinates.rq` / `q07_org_subordinates.json` | `sample_data.ttl` | < 300ms | Required |
| CQ-014 | Q2 | Which entity does a track resolve to? | `q08_track_resolution.rq` / `q08_track_resolution.json` | `domain_tracking.ttl` | < 100ms | Required |
| CQ-015 | Q3 | Which tracks belong to entities operated by organizations in a region? | `q09_tracks_in_region.rq` / `q09_tracks_in_region.json` | `domain_tracking.ttl` | < 300ms | Required |
| CQ-016 | Q5 | Which sensors produced observations used by a track? | `q10_track_sensor_provenance.rq` / `q10_track_sensor_provenance.json` | `domain_tracking.ttl` | < 500ms | Required |
| CQ-017 | Q4 | Show the location assertion history with each half-open valid interval. | `q17_temporal_history.rq` / `cq-017.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-018 | Q4 | Where was the tracked entity on 2026-08-05? | `q18_temporal_asof.rq` / `cq-018.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-019 | Q4 | Where was it on 2026-08-15 after correction? | `q18_temporal_asof.rq` / `cq-019.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-020 | Q4 | Which location is active exactly at the previous assertion's exclusive end? | `q18_temporal_asof.rq` / `cq-020.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-021 | Q4 | Which location is active at the next interval boundary? | `q18_temporal_asof.rq` / `cq-021.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-022 | Q4 | How many distinct locations conflict for an entity at a given instant? | `q19_conflict_count.rq` / `cq-022.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-023 | Q4 | Which location values expose that active contradiction? | `q20_conflict_locations.rq` / `cq-023.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-024 | Q4 | Which assertion replaced a corrected assertion? | `q21_assertion_status.rq` / `cq-024.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-025 | Q4 | Is a retracted assertion still active? | `q21_assertion_status.rq` / `cq-025.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-026 | Q4 | Does an unsuperseded assertion remain active? | `q21_assertion_status.rq` / `cq-026.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-027 | Q5 | Which normalized sources support a corrected assertion? | `q22_provenance_sources.rq` / `cq-027.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-028 | Q5 | Which sources support the current replacement assertion? | `q22_provenance_sources.rq` / `cq-028.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-029 | Q5 | What source locator and text span support an assertion? | `q23_inline_evidence.rq` / `cq-029.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-030 | Q5 | Which source-backed facts start within a valid-time interval? | `q24_source_fact_interval.rq` / `cq-030.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-031 | Q1 | How is an unresolved subject represented before identity resolution? | `q25_pending_identity.rq` / `cq-031.json` | `semantic_workload.ttl` | < 50ms | Required |
| CQ-032 | Q1 | Which entity does a source-scoped identifier resolve to? | `q26_identifier_lookup.rq` / `cq-032.json` | `semantic_workload.ttl` | < 50ms | Required |
| CQ-033 | Q1 | Can a second identifier scheme resolve its own exact value? | `q26_identifier_lookup.rq` / `cq-033.json` | `semantic_workload.ttl` | < 50ms | Required |
| CQ-034 | Q1 | Do equal identifier strings from different schemes stay distinct? | `q27_identifier_collisions.rq` / `cq-034.json` | `semantic_workload.ttl` | < 50ms | Required |
| CQ-035 | Q2 | What measured position did an observation result report? | `q28_observation_result_position.rq` / `cq-035.json` | `semantic_workload.ttl` | < 100ms | Required |
| CQ-036 | Q2 | What position did the later observation result report? | `q28_observation_result_position.rq` / `cq-036.json` | `semantic_workload.ttl` | < 100ms | Required |
| CQ-037 | Q2 | What typed speed value and unit did the sensor report? | `q29_measurement_value.rq` / `cq-037.json` | `semantic_workload.ttl` | < 100ms | Required |
| CQ-038 | Q2 | What typed heading value and unit did the sensor report? | `q29_measurement_value.rq` / `cq-038.json` | `semantic_workload.ttl` | < 100ms | Required |
| CQ-039 | Q3 | What ordered positions form a track's observed path? | `q30_track_path.rq` / `cq-039.json` | `semantic_workload.ttl` | < 300ms | Required |
| CQ-040 | Q2 | Which sensor produced a specific observation? | `q31_sensor_provenance.rq` / `cq-040.json` | `semantic_workload.ttl` | < 100ms | Required |
| CQ-041 | Q5 | Which sensors contributed to a tracking product? | `q31_sensor_provenance.rq` / `cq-041.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-042 | Q5 | Which normalized source record supports an observation? | `q32_observation_provenance.rq` / `cq-042.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-043 | Q4 | Can a measured sensor position be retrieved without asserting world location? | `q33_observation_assertion_separation.rq` / `cq-043.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-044 | Q2 | Is an observation an event and its result an information object? | `q34_observation_event_result_types.rq` / `cq-044.json` | `semantic_workload.ttl` | < 100ms | Required |
| CQ-045 | Q6 | How many measurements of each kind were reported for an entity? | `q35_measurement_counts.rq` / `cq-045.json` | `semantic_workload.ttl` | n/a (OLAP) | Required |
| CQ-046 | Q5 | Which source supports a derived track? | `q36_track_provenance.rq` / `cq-046.json` | `semantic_workload.ttl` | < 500ms | Required |
| CQ-047 | Q4 | How does a person's organization membership change over valid time? | `q37_temporal_relationship.rq` / `cq-047.json` | `semantic_workload.ttl` | < 500ms | Required |

`make quality` executes all 41 specs against their committed datasets and
compares the exact projected columns and rows. The cases cover
temporal boundaries, correction/retraction, contradictions, normalized source
and inline evidence provenance, unresolved identity, identifier schemes,
observation-result semantics, measurements, and track lineage. Older unimplemented
questions outside this workload were removed from the current contract.

Query groups: **Q1** identity lookup (< 50 ms), **Q2** direct relationship or
measurement (< 100 ms), **Q3** graph traversal (< 300 ms), **Q4** temporal state,
history and contradictions (< 500 ms), **Q5** provenance (< 500 ms), and **Q6**
analytical aggregation on an OLAP engine (not the operational graph store).
Tracking questions now follow the observation event, separate
`tracking:ObservationResult`, track and source chain.

Performance invariant for operational queries:

```text
80% of operational queries use at most 3 graph hops
```

This is an engineering SLO to measure, not an assumption about ontology quality.
