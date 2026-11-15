# Milestone 1 identity baseline

This report captures the pre-change state at commit `b6ba61fbe64eb0d13b89e2bf57aeddae85e33958`, before identity-resolution edits. `baseline.json` indexes results, environment, commands, and hashes. `input-manifest.json` pins identity source/tests, benchmark tools, fixtures, all query contracts, and expected results. Reports and logs are captured run outputs. Large datasets, event logs, RDF stores, and tool archives remain in ignored `build/`.

## Reproduction

Use the existing Python 3.13 `.venv` and Temurin 25.0.5+8 for conformance and Oxigraph. `make setup` was skipped because the existing environment passed `pip check`, while the setup target would recreate it with system Python 3.14.

```sh
git checkout b6ba61fbe64eb0d13b89e2bf57aeddae85e33958
make check
make conformance
make benchmark
.venv/bin/python tools/benchmark_e2e.py --entities 300
make oxigraph-test
make oxigraph-eval
make splink-fetch
make splink-test
make splink-eval
```

The captured evaluation commands used explicit output directories beneath ignored `build/milestone1-baseline-b6ba61f/` to preserve previous build artifacts. The exact commands and dependency pins are recorded in `baseline.json` and logs. FEBRL4 sources are pinned by SHA-256 in its report and `benchmarks/evaluations/febrl4_sources.json`.

## Interpretation

`benchmark-inmemory.json` measures an in-process synthetic workload. `benchmark-e2e.json` measures ingestion and lake paths at 300 entities. Oxigraph reports direct engine and Fuseki loopback HTTP timings separately at 10K and 100K entities. Splink separates candidate recall from review precision/recall under its five-candidate review budget. Percentiles are per-call samples, not comparable to historical batch-average measurements.
