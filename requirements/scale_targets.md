# Scale Targets

Architecture phải thiết kế hướng tới các con số dưới đây ngay từ đầu,
dù không nhất thiết đạt trong năm đầu.

## Targets đích

```text
Entities        10^8
Relations       10^9
Events/day      10^7
Sources         10^3
Ontology terms  10^4–10^5
```

## Ladder kiểm chứng

| Bước | Dataset | Mục đích |
|-----:|---------|----------|
| 0 | Seed dataset (repo này) | Correctness của CQ + SHACL |
| 1 | Synthetic 10M entities / 100M edges | Ingestion throughput, index build, query p95 |
| 2 | Synthetic 100M entities / 1B edges | Horizontal scalability, replication, recovery |
| 3 | Federation (nhiều domain KG) | Federated queries, semantic contracts |

Ở mỗi bước đo: ingestion throughput, query p95/p99, memory, storage,
index build time, recovery time, replication lag.

## Bằng chứng đo hiện tại

Lần chạy độc lập ngày **2026-11-12** dùng
`.venv/bin/python tools/benchmark.py --scale 100000 --observations 10 --runs 200`:

| Metric | Kết quả |
|---|---:|
| Synthetic entities / events / location observations | 100K / 1.1M / 1M |
| Synthetic ingest | 13,706 events/s (80.2558 s) |
| Exact alias lookup | 699,215 lookups/s (1M hits) |
| Streaming projection / index build | 139,022 events/s (7.9124 s) |
| Full replay into a fresh in-memory model | 7.9124 s |
| Query p95: Q1 / current location / Q4 | 0.0013 / 0.1293 / 0.1570 ms |
| Process peak RSS / event-log bytes / segments | 805,044,224 / 448,412,434 bytes / 14 |

Synthetic ingest includes event creation, JSON serialization, segmented log writes
and fsync. Query p95 uses 200 warm entity probes, each averaging 100 calls; it is
not an individual-call latency percentile. RSS is a process high-water mark, so
scale runs must use a standalone process. The log reader and projector stream
records, but identity and read-model indexes remain in memory. This probe has
100K entities and 1M location observations; it does not reach the 10M entity /
100M edge target or measure distributed recovery. The scale ladder remains open;
the next requirement is a persistent identity and read-model backend, with index
build and snapshot recovery measured separately.

## Quy tắc kiến trúc liên quan scale

- Không dùng một database cho mọi thứ — polyglot:
  graph (traversal), relational/columnar (history/analytics), search (text/alias),
  vector (candidate generation), stream (live events).
- Federation > one huge KG: các domain KG riêng nhưng conform chung
  `core + middle + semantic contracts`; query federation xử lý cross-domain.
- Global ID strategy ngay từ đầu: `urn:world:entity:<uuid>`;
  không dùng database auto-increment làm semantic identity.
  Mỗi entity có `canonical_id`, `external_ids[]`, `aliases[]`, `source_ids[]`.
- Event log append-only, immutable — history không bị overwrite.
