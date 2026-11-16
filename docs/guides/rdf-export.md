# Offline RDF export

The event log remains the authoritative source. `tools/export_rdf.py` reads it
offline with `EventLog.iter_all()` and writes a TriG dataset using the existing
RDF mappers. The command does not add an export endpoint to FastAPI.

The CLI streams one event's RDF at a time. It first builds a SQLite index for
cross-event document, observation, and location references, then scans the
unchanged event log again while writing TriG. SQLite keeps an 8 MiB page cache;
the reference IDs stay on disk. By default the index is temporary and removed
after export. `--context-index PATH` retains it and reuses it while the event
log segment fingerprint matches. If a segment changes, the index is rebuilt
beside the old one and replaced atomically. `project_event_log()` remains the
separate full-materialization API for callers that need an RDFLib `Dataset`.
Each event is mapped in a fresh RDFLib graph and that graph is discarded after
its quads are written, avoiding retained empty store indexes as the log grows.

## Profiles

`canonical-v1` writes the repository's current RDF mappings to the default
graph. `interop-v1` keeps that graph and adds standards mappings in the named
graph `urn:ontology-research:graph:interop-v1`.

PROV-O marks assertions, documents and tracks as entities and uses the stored
event IRI for an activity that generated them. A recorded assertion cites every
source in `core:hasSource`; the PROV activity uses a source only when that IRI
was registered by a `DocumentRegistered` event in the same log. The exporter
does not infer agents from source-system names. A track is linked with
`prov:wasDerivedFrom` only for observation IDs both listed by `TrackObserved`
and present in the event log.

SOSA uses the existing observation, sensor, feature-of-interest and result
IRIs. `sosa:phenomenonTime` carries `ObservationRecorded.at_time` through an
OWL-Time instant. The record timestamp `occurred_at` is not substituted for
`sosa:resultTime`.

An optional CSV adds GeoSPARQL points for locations in the projection. Its
header must be exactly `location_iri,longitude,latitude,source_iri`. Locations
must be present in an assertion, observation, or typed entity event. Each row
has one unique absolute location IRI and a non-empty absolute source IRI;
longitude and latitude must be finite and within their respective ranges. WKT
uses CRS84 with longitude first. A location without a CSV row remains in the
export without a geometry. The source stays as `dcterms:source`; it also becomes
`prov:wasDerivedFrom` when it names a registered document.

Every mapping and its conditions are recorded in
`registry/rdf-crosswalks.json`. Events
without an existing RDF mapper are skipped and listed by the CLI.

## Run

```bash
.venv/bin/python tools/export_rdf.py \
  --event-log "$HOME/Data/ontology-research/data/events.jsonl" \
  --profile interop-v1 \
  --output build/events.trig \
  --geometry data/location-geometry.csv \
  --context-index build/events-rdf-index.sqlite
```

Use `--profile canonical-v1` and omit `--geometry` for only the repository
default graph. `--context-index` is optional. The output file is replaced only
after projection succeeds and the event-log fingerprint is unchanged across
both scans. Output triple lines are emitted by graph blocks; repeated graph
blocks merge when parsed as an RDF dataset.

## Verify

```bash
.venv/bin/pytest -q tests/test_rdf_export.py
make check
```

The tests parse the TriG again and check resolved and pending subjects,
predicates, IRI and literal objects, datatype and language tags, half-open
validity timestamps, sources, evidence locators/spans, observation-result
links, the interop SHACL shapes, geometry source checks, and CRS84 axis order.

## Scale evaluation

`make rdf-export-eval` builds deterministic logs with 1M and 10M events. Each
group contains one registered document, observation, location assertion, and
track that references the observation. Every group has a distinct document,
observation, and location ID, plus one matching geometry CSV row. This forces
the export through all three cross-event index lookups while writing the
`interop-v1` profile.

The command stores generated logs, the reusable SQLite index, geometry CSV,
TriG output, and a raw report under a new ignored `build/rdf-export-evaluation-*`
directory. It also writes the compact, versioned report to
`benchmarks/evaluations/reports/rdf-export-v1.json`. Run a smaller smoke scale
with `.venv/bin/python tools/benchmark_rdf_export.py --scales 1000`.

Log generation, index building, and export run in fresh processes. The report
records input and output SHA-256 hashes, bytes, index size, phase times, peak
RSS, event throughput, runtime versions, and host details. Hashing is outside
the measured phases. The full RDF graph is not parsed at scale because doing so
would construct another in-memory dataset; RDF dataset isomorphism is checked
by the small fixture tests in `tests/test_rdf_export.py`.
