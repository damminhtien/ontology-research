# Oxigraph snapshot evaluation

This experiment evaluates Oxigraph as a rebuildable read store. The event log
remains authoritative, and the snapshot is disposable derived data. The tool
does not connect to FastAPI or the production identity store.

## Snapshot contract

`foundry.oxigraph_snapshot.build_snapshot()` streams the event log through the
canonical RDF mapper, writes N-Quads into a candidate directory, loads and
reopens it with `Store.read_only`, then atomically replaces the `CURRENT`
pointer. The manifest records the content fingerprint and segment digests,
last sequence, event and quad counts, ontology module versions, profile,
Oxigraph version, reopen time, and build time. If projection, load, validation,
or source-stability checks fail, the candidate is discarded and `CURRENT`
continues to name the previous snapshot.

The experiment performs full rebuilds. It does not add an incremental writer
or an import path into the event log. Snapshot directories and benchmark
outputs belong under ignored `build/`.

## Reproduce

```bash
make oxigraph-test
make oxigraph-eval
```

The Make targets create an isolated Python 3.13 environment with
`pyoxigraph==0.5.13`; the project environment and `requirements.txt` stay
unchanged. The evaluation downloads and checksum-verifies Apache Jena 6.2.0,
checks all 41 seed competency queries against RDFLib, Oxigraph, and Jena ARQ,
then builds 10K and 100K entity logs with ten assertions per entity using
`tools.benchmark.generate_events(seed=42)`.

For a shorter development run:

```bash
uv run --no-project --python 3.13 \
  --with-requirements requirements/evaluation-oxigraph.txt \
  python tools/evaluate_oxigraph.py --scales 100 --runs 10
```

## Measurements

The runner performs a full snapshot rebuild twice per scale and verifies that
the first query after reopening returns the same rows as the first published
snapshot. Both stores execute the same SPARQL strings for point lookup, temporal
lookup, and provenance.

Engine-level timing is measured separately: Oxigraph uses direct
`Store.query` calls, while a persistent Java harness runs `QueryExecution`
directly against TDB2 and measures transaction start, execution, and result
materialization inside the JVM. Both interfaces receive the same query text and
include parsing and result materialization in the timed section. A separate
service-level section reports Fuseki loopback HTTP latency;
those values include HTTP and SPARQL JSON overhead and must not be compared with
engine-level percentiles. Fuseki rejects updates. Cold latency means the first
workload query after opening each store; Fuseki readiness and read-only checks
run before its first measured workload query. The operating-system page cache is
not cleared. Warm p50/p95/p99 use 100 calls per query after five warmups by default.

The report records first-query and warm latency, sample counts, Oxigraph/TDB2
build and reopen time, peak RSS by process, disk footprint, software versions,
hardware, event-log manifest and source hash, generator/harness hashes, and the
reproduction command. The measured reference is
`benchmarks/evaluations/reports/oxigraph-v2.json`; generated datasets, stores,
logs and the full machine-specific report stay under ignored
`build/oxigraph-evaluation/`. These percentiles are not directly comparable
with historical batch-average measurements.

On the machine recorded in that report, the 100K scale produced 1.1M events
and 10.2M triples. A full Oxigraph rebuild took 525 seconds and peaked at 23.1
GB process RSS; TDB2 loaded the same projection in 29.9 seconds and occupied
1.71 GB. These figures describe this implementation and host. They show that
snapshot construction and storage need improvement before considering a larger
read-model trial; the query timings alone do not justify selecting a backend.
