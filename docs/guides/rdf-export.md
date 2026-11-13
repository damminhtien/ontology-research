# Offline RDF export

The event log remains the authoritative source. `tools/export_rdf.py` reads it
offline with `EventLog.iter_all()` and writes a TriG dataset using the existing
RDF mappers. The command does not add an export endpoint to FastAPI.

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
  --event-log data/events.jsonl \
  --profile interop-v1 \
  --output build/events.trig \
  --geometry data/location-geometry.csv
```

Use `--profile canonical-v1` and omit `--geometry` for only the repository
default graph. The output file is replaced only after projection and
serialization succeed.

## Verify

```bash
.venv/bin/pytest -q tests/test_rdf_export.py
make check
```

The tests parse the TriG again and check resolved and pending subjects,
predicates, IRI and literal objects, datatype and language tags, half-open
validity timestamps, sources, evidence locators/spans, observation-result
links, the interop SHACL shapes, geometry source checks, and CRS84 axis order.
