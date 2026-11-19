# OSINT Scale Ladder

## Latest runs (2026-11-19)

All runs use seed 42, PostgreSQL 18.7, Python 3.13.16, macOS 27.2.1 on an
Apple M2 Max (12 logical CPUs, 64 GiB RAM), and the same
`QueryService → PostgreSQL → psycopg → Python` boundary without HTTP. The
workload is deterministic synthetic data, not crawled OSINT or a manually
adjudicated gold set. The pre-COPY runs used commit
`35c5cfbdd10e4318e0b4959d0c8a305b5fafb42b`; the reports record a dirty worktree.

| Level | Projection events | Time | Throughput | First / second half | Query p95 gates | Checkpoint |
|---|---:|---:|---:|---:|---|---|
| L0: 10K entities / 100K assertions | 110K | 10.74 s | 10,243 events/s | 11,206 / 9,712 (-13.33%) | All passed at concurrency 1/8/32 | Consistent through 110,100 after tail append |
| L1 before ledger COPY: 100K / 1M | 1.1M | 120.63 s | 9,119 events/s | 9,315 / 9,080 (-2.52%) | All passed at concurrency 1/8/32 | Consistent through 1,100,100 after tail append |
| L2 before ledger COPY: 1M / 10M | 11M | 1,881.48 s | 5,846 events/s | 7,420 / 5,213 (-29.74%) | All passed at concurrency 1/8/32 | Consistent through 11,000,100 after tail append |
| L1 after ledger COPY: 100K / 1M | 1.1M | 115.10 s | 9,557 events/s | 9,797 / 9,494 (-3.09%) | All passed at concurrency 1/8/32 | Consistent through 1,100,100 after tail append |

L0 cleared the 10,000 events/s projection minimum by 2.4%, with a 13.33%
second-half slowdown. The first L1 run stayed 8.8% under that minimum; the
post-COPY L1 retest improved 4.8% to 9,557 events/s but still missed by 4.4%.
The pre-COPY L2 run reached 5,846 events/s and slowed 29.74% in its second half.
All runs matched event-ledger count, projection count and final checkpoint; all
recorded query p95s were within their configured limits at concurrency 1/8/32.
The Python high-water RSS was 162.0 MB at L0, 406.2 MB at L1, and 414.2 MB at
L2; these values exclude PostgreSQL and other system processes. Database/event-
log footprints were 169.9/129.6 MB at L0, 1.611/1.296 GB at the first L1,
1.611/1.296 GB at the post-COPY L1, and 16.008/12.973 GB at pre-COPY L2.
Restart plus checkpoint verification took 0.145 s, 0.192 s, 0.180 s and
0.625 s respectively. L2 index rebuild took 270.57 s.

At pre-COPY L2, projection batch latency increased from 0.96 s for the first
10K-event batch to 2.47 s for the last. The read assertion table and indexes
occupied 11.99 GB; the projection event ledger occupied 1.83 GB. A separate
instrumented 1.1M-event prefix profile attributed 41.3% of observed time to
assertion batches and 20.2% to ledger/checkpoint batches. Replacing the large
ledger `VALUES` statement with COPY staging reduced the ledger/checkpoint share
to 14.9% and its measured duration from 24.05 s to 15.50 s. The same profile
still attributed 45.1% to assertion batches. These instrumented method timings
include Python, psycopg, and PostgreSQL and are diagnostic, not SLO results.
A 10-second Python sample spent 6,196 of 8,433 samples in psycopg polling; a
concurrent PostgreSQL activity snapshot caught an assertion insert waiting on
`DataFileExtend`, with autovacuum active. This points to database write and file
growth costs, but does not prove that either alone explains the full L2 decline.
The post-COPY code has not been rerun at L2.

The report files and generated logs remain outside Git:

- L0: `/Users/macbook/Data/ontology-research/evaluations/osint-scale-ladder-v1/20261119T075725Z-6862b1d0/l0/scale-report.json`
- L1: `/Users/macbook/Data/ontology-research/evaluations/osint-scale-ladder-v1/20261119T080018Z-0b961589/l1/scale-report.json`
- L2: `/Users/macbook/Data/ontology-research/evaluations/osint-scale-ladder-v1/20261119T081041Z-4f98b094/l2/scale-report.json`
- L1 after ledger COPY: `/Users/macbook/Data/ontology-research/evaluations/osint-scale-ladder-v1/20261119T103315Z-f1ee4318/l1/scale-report.json`
- Diagnostic profiles: `/Users/macbook/Data/ontology-research/evaluations/osint-scale-ladder-v1/20261119T081041Z-4f98b094/l2/profile-prefix-1.1m.json`, `profile-prefix-1.1m-copy-ledger.json`, and `python-profile-prefix-1.1m.txt`

Reproduce either level with the same options and change only `--level`:

```sh
FOUNDRY_DATABASE_URL="<PostgreSQL admin DSN>" \
  .venv/bin/python tools/benchmark_osint_scale.py \
  --level l0 --seed 42 --append-batch-events 10000 \
  --query-runs 100 --concurrency 1,8,32 --tail-appends 100 \
  --data-root /Users/macbook/Data/ontology-research
```

L0 meets the projection rate threshold only narrowly. Both L1 runs and the
pre-COPY L2 run miss it; post-COPY L2 has not been measured. L3–L4 remain
unmeasured, so the current evidence does not qualify production-scale traffic or
multi-writer use.

## Historical baseline (2026-11-18)

**Run:** `20261118T082918Z-1ee7a959`
**Measured level:** L0 only
**Status:** correctness and query gates passed; PostgreSQL projection throughput did not meet its SLO. L1–L4 were not run.

## Purpose and scope

This run measures the PostgreSQL operational projection with a deterministic,
synthetic OSINT-shaped workload. It does not establish performance on real
source data, API requests, multiple writers, or a production host. The benchmark
uses a temporary PostgreSQL database and drops it after the run. The generated
event log and reports are kept outside Git under
`/Users/macbook/Data/ontology-research/evaluations/osint-scale-ladder-v1/`.

The ladder targets are 10K entities / 100K assertions at L0, 100K / 1M at L1,
1M / 10M at L2, 10M / 100M at L3, and 100M / 1B at L4. Each entity receives ten
assertions. The event log therefore contains entity and assertion events, so L0
projects 110K events before its 100-event concurrent append probe.

The synthetic data has six entity types, eight typed predicates, 64 long-tailed
sources, entity and location objects, 1–3 evidence references with locators and
exact text spans, and valid-time windows across 2010–2025. In this L0 sample,
the five most frequent sources contributed 60.4% of source references; 78,899
assertions linked to entities and 21,101 to locations. The realized valid-time
year buckets were 54,989 recent, 25,011 middle-period, and 20,000 older
assertions. Seventy percent of entity targets were sampled from the top one
percent of the entity range. This workload has skew and provenance, but it is
still synthetic and cannot substitute for a domain gold set.

The historical 1M location-observation probe is not counted as 1M assertions.
The earlier in-memory benchmark is recorded separately in
[`requirements/scale_targets.md`](../../requirements/scale_targets.md).

## Reproduction

Set `FOUNDRY_DATABASE_URL` to a PostgreSQL administrative DSN for a local test
instance. The runner creates and drops a uniquely named temporary database.
The DSN is deliberately absent from the report.

```sh
FOUNDRY_DATABASE_URL="<PostgreSQL admin DSN>" \
  .venv/bin/python tools/benchmark_osint_scale.py \
  --level l0 --seed 42 --append-batch-events 10000 \
  --query-runs 100 --concurrency 1,8,32 --tail-appends 100 \
  --data-root /Users/macbook/Data/ontology-research
```

The measurement used commit `2ba5d79a612dda5e31a4dca044610b6df4a6fc84`,
Python 3.13.16, PostgreSQL 18.7 (Homebrew), macOS 27.2.1, Mac14,6 / Apple M2
Max, 12 logical CPUs, and 64 GiB RAM. The worktree was dirty because this
benchmark runner and the ladder documentation were being added. The runner
source SHA-256 at measurement time was
`cd41e76aa9d84122ca9e95cc6ee8dc596c1b38e1b3aa64749e4dd605b45e89f9`.

The machine-readable report is
`/Users/macbook/Data/ontology-research/evaluations/osint-scale-ladder-v1/20261118T082918Z-1ee7a959/l0/scale-report.json`.
The event log uses four segments and is hashed in the report. Its generated
110K-event prefix has SHA-256
`99e05e04f1dc7cb50141350176d385fd6d883f9168e89b5cb3cb56844d4ff2a1`; after the
concurrent 100-assertion append, the final 110,100-event log has SHA-256
`f9285a535814b370bd7f657557a332c38195258f29552d7016eda4435f1b0fad`.

## L0 results

The synthetic durable event-log append reached 15,716 events/s, inside the
10,000–100,000 events/s ingestion SLO. Its second-half rate was 8.27% lower than
the first half. Fresh PostgreSQL projection processed 110,000 events in 736.2
seconds, or 149 events/s. The projection rate was steady across batches (2.59%
second-half increase), but missed the 10,000 events/s projection SLO by 67.1×.

| Measure | L0 result |
|---|---:|
| PostgreSQL migration and initial index setup | 0.034 s |
| `ANALYZE` | 1.976 s |
| Fresh projection | 736.225 s; 149 events/s |
| Projection batch rate, first / second half | 148 / 152 events/s |
| `REINDEX` of measured projection tables | 2.018 s |
| Reopen and checkpoint verification | 0.179 s |
| Final database size | 169,572,031 bytes |
| Final segmented event log size | 129,622,462 bytes |
| Database / event-log size ratio | 1.308× |
| Database plus log, normalized per million assertions | 2,988,955,974 bytes (about 2.99 GB) |
| Benchmark Python process peak RSS | 96,108,544 bytes (about 91.6 MiB) |

The RSS figure covers only the Python benchmark process. It excludes the
PostgreSQL server and other processes, so it is not a whole-system peak-memory
measurement. The normalized storage figure is from L0 and must not be linearly
extrapolated as a measured L1–L4 footprint. No monetary cost was measured.

### Query adapter

The query path was `QueryService → PostgreSQL → psycopg → Python result
materialization`. It did not include FastAPI or HTTP. Each operation ran 100
requests at each concurrency level; all requests succeeded. All recorded p95
values were inside the configured operation limits.

| Operation | p95 / p99, concurrency 1 (ms) | p95 / p99, concurrency 8 (ms) | p95 / p99, concurrency 32 (ms) |
|---|---:|---:|---:|
| Entity lookup | 3.07 / 3.23 | 5.54 / 6.65 | 20.91 / 21.06 |
| Relationships as-of | 4.70 / 4.89 | 6.77 / 8.06 | 21.08 / 21.48 |
| Assertion provenance | 2.64 / 2.80 | 5.60 / 5.99 | 21.30 / 21.44 |
| Two-hop traversal | 6.59 / 7.02 | 9.25 / 10.34 | 60.79 / 71.98 |

### Recovery and concurrent reads

After projection and a 100-assertion tail append, a fresh process verified
checkpoint sequence 110,100 against 110,100 recorded events. The read model held
10,000 entities and 100,100 assertions. The projection event ledger and
checkpoint were consistent.

The single writer projected 100 new assertions while eight reader threads
issued 1,083 relationship queries. There were no query errors. The append plus
projection rate was 55 events/s; reader p50/p95/p99 was 6.37/26.76/183.40 ms.
This is a small concurrency correctness probe, not a multi-writer test or a
crash-injection recovery test.

## Bottleneck evidence and ladder decision

The five-second macOS `sample` artifact captured 3,029 samples; 1,408 samples
were in the `poll` / libpq response-reading stack. The profile is consistent
with the client waiting on PostgreSQL responses. Code inspection identifies a
per-event transaction in `tools/project_events.py`; the event path takes the
projector advisory lock and calls `is_applied`, which issues a `SELECT 1` for
each event in [`foundry/postgres_readmodel.py`](../../foundry/postgres_readmodel.py).
This makes per-event database round trips a strong bottleneck candidate. The
profile does not measure PostgreSQL server CPU, so it does not establish whether
server-side query execution is also a limit.

Since the projection SLO failed already at L0, L1–L4 were deferred. At the L0
projection rate, raw projection time alone would be about 2.1 hours for L1,
20.5 hours for L2, 8.5 days for L3, and 85 days for L4. These are arithmetic
extrapolations, not benchmark results, and exclude query, index, generation,
and recovery work. The next scale step is to improve and remeasure the existing
projection path before extending the ladder; this run provides no evidence for
changing database technology.

## Measurement limits

- L0 is synthetic; no production OSINT corpus or human-labeled identity data was
  used.
- Queries are adapter-level, not API-boundary measurements.
- Peak RSS excludes PostgreSQL and the rest of the system.
- Only one writer was used. Reader concurrency was limited to 8 for the append
  probe and 1, 8, and 32 for query samples.
- Recovery measured fresh-process reopen and checkpoint verification after a
  completed projection. It did not simulate process termination during a
  transaction or rebuild the projection from empty state.
- Storage-per-million and all scale observations are L0-only measurements.
- L3–L4 remain unmeasured. No performance claim is made for those levels.
