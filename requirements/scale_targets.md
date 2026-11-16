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

Synthetic ingest gồm tạo event, JSON serialization, ghi segmented log và fsync.
Query p95 lấy từ 200 warm entity probes, mỗi probe tính trung bình 100 lần gọi;
đây không phải percentile của từng lần gọi. RSS là high-water mark của process,
nên mỗi lượt scale cần chạy trong process riêng.

Benchmark này dùng in-memory identity và read-model adapters; chưa đo
PostgreSQL. Quy mô 100K entity và 1M location observation còn dưới target 10M
entity / 100M edge, và chưa bao gồm distributed recovery. PostgreSQL operational
backend đã được triển khai. Bước tiếp theo là đo durable projection, index
build, restart và recovery ở 1M rồi 10M entity.

## Quy tắc kiến trúc liên quan scale

- Duy trì store theo workload: event log làm nguồn sự thật; PostgreSQL phục vụ
  identity và operational queries; Parquet/DuckDB phục vụ history/analytics.
  Đánh giá thêm Oxigraph cho SPARQL trước khi chọn làm semantic read store.
- Federation là hướng mở rộng: mỗi domain KG tuân thủ `core + middle + semantic
  contracts`; query federation cần có workload cụ thể trước khi triển khai.
- PostgreSQL candidate/search indexes là bước hiện tại. Vector candidate
  generation chỉ thêm khi đo được lợi ích cho review workload.
- Global ID strategy ngay từ đầu: `urn:world:entity:<uuid>`;
  không dùng database auto-increment làm semantic identity.
  Mỗi entity có `canonical_id`, `external_ids[]`, `aliases[]`, `source_ids[]`.
- Event log append-only, immutable — history không bị overwrite.
