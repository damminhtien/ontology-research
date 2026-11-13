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

The runner performs a full snapshot rebuild twice per scale and confirms a
sample query returns the same rows before and after `CURRENT` changes. It also
loads the same N-Quads snapshot into TDB2, starts a query-only Fuseki service
bound to loopback, verifies that its update endpoint is unavailable, and
compares point lookup, temporal, and provenance output before timing calls.

Oxigraph timings are direct in-process `Store.query` calls. Jena timings are
Fuseki loopback HTTP calls and include HTTP overhead. The report gives per-call
p50/p95/p99 after warm-up, sample counts, build/rebuild and reopen time, peak
RSS by process, store size, software versions, and machine metadata. These
percentiles are not directly comparable with historical batch-average
measurements. Reports and generated logs are written beneath
`build/oxigraph-evaluation/` and are not committed.
