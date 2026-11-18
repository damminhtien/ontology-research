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

| Level | Dataset | Mục đích |
|---|---|---|
| L0 | 10K entities / 100K assertions | Correctness và chức năng trên OSINT-shaped workload |
| L1 | 100K entities / 1M assertions | So sánh với baseline và xem throughput theo batch |
| L2 | 1M entities / 10M assertions | Indexing, memory, query, recovery và đồng thời reader/writer |
| L3 | 10M entities / 100M assertions | Production-scale qualification |
| L4 | 100M entities / 1B assertions | Mục tiêu kiến trúc dài hạn |

Workload benchmark dùng 10 assertions/entity để dựng relation graph, source skew,
evidence locator/span và valid-time qua nhiều năm. Bộ dữ liệu 1M location
observations cũ không thay thế 1M assertion OSINT.

Ở mỗi level, benchmark cần đo append và projection throughput theo batch, query
p50/p95/p99 ở concurrency khai báo, memory với phạm vi process/system rõ ràng,
log/database footprint, index rebuild, restart/recovery và reader/writer
consistency. Nếu không đạt SLO, profile bottleneck trước khi đổi storage hoặc
thêm distributed components.

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
PostgreSQL. Quy mô 100K entity và 1M location observation là baseline lịch sử,
không tương đương 1M assertion OSINT. PostgreSQL operational backend được đánh
giá riêng bằng `tools/benchmark_osint_scale.py`. Tính đến **2026-11-18**, L0
(10K entities / 100K assertions) đã chạy: event-log append đạt 15,716 events/s,
nhưng PostgreSQL projection chỉ đạt 149 events/s so với SLO 10,000 events/s.
Query p95 và checkpoint consistency đạt trong phạm vi phép đo; L1–L4 chưa chạy
vì projection chưa qua gate. Chi tiết và giới hạn đo nằm trong
[`docs/evaluations/osint-scale-ladder.md`](../docs/evaluations/osint-scale-ladder.md);
JSON, event log và profile được lưu ngoài Git trong `/Users/macbook/Data`.

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
