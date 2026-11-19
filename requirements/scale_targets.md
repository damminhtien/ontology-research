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
giá riêng bằng `tools/benchmark_osint_scale.py`.

### Baseline lịch sử ngày 2026-11-18

L0 append đạt 15,716 events/s, nhưng PostgreSQL projection chỉ đạt 149
events/s. Báo cáo ghi nhận transaction và ledger round trip theo từng event,
cùng tra cứu subject reference thiếu index đầy đủ, là bottleneck cần xử lý.

### Tái đo ngày 2026-11-19

Hai lượt dưới đây dùng seed 42, cùng máy Apple M2 Max / 12 logical CPUs / 64
GiB RAM, Python 3.13.16 và PostgreSQL 18.7. Workload sinh tổng cộng một
`EntityCreated` cho mỗi entity và 10 `AssertionMade` cho mỗi entity. Query path
là `QueryService → PostgreSQL → psycopg → Python`; không đo HTTP. Dữ liệu là
synthetic, không phải crawl thật hay gold set đã adjudicate.

| Level | Events | Projection | Throughput | First/second half | Query p95 | Restart check | Database / event log | Python peak RSS |
|---|---:|---:|---:|---:|---|---:|---:|---:|
| L0: 10K / 100K | 110K | 10.74 s | 10,243 events/s | 11,206 / 9,712 events/s (-13.33%) | Tất cả gate đạt ở concurrency 1/8/32 | 0.145 s | 169.9 / 129.6 MB | 162.0 MB |
| L1 trước COPY ledger: 100K / 1M | 1.1M | 120.63 s | 9,119 events/s | 9,315 / 9,080 events/s (-2.52%) | Tất cả gate đạt ở concurrency 1/8/32 | 0.192 s | 1.611 / 1.296 GB | 406.2 MB |
| L2 trước COPY ledger: 1M / 10M | 11M | 1,881.48 s | 5,846 events/s | 7,420 / 5,213 events/s (-29.74%) | Tất cả gate đạt ở concurrency 1/8/32 | 0.625 s | 16.008 / 12.973 GB | 414.2 MB |
| L1 sau COPY ledger: 100K / 1M | 1.1M | 115.10 s | 9,557 events/s | 9,797 / 9,494 events/s (-3.09%) | Tất cả gate đạt ở concurrency 1/8/32 | 0.180 s | 1.611 / 1.296 GB | 326.6 MB |

L0 vừa qua mức tối thiểu 10,000 events/s với biên 2.4%; nửa sau chậm hơn
13.33%. L1 trước COPY ledger thấp hơn mức tối thiểu 10,000 events/s khoảng
8.8%; sau COPY, throughput tăng 4.8% nhưng vẫn thấp hơn gate 4.4%. Cả bốn lượt
khớp checkpoint và số dòng cuối với event log. Python RSS không gồm PostgreSQL
server hoặc tiến trình hệ thống. Concurrent append/read là probe nhỏ: L0 90,
L1 trước COPY 112 và L1 sau COPY 111 events/s khi 8 reader hoạt động; không đại
diện cho sustained ingest SLO.

L2 mất khoảng 31.4 phút cho projection; generation mất 859.03 giây. Rebuild
index mất 270.57 giây, restart/checkpoint verification mất 0.625 giây, và
concurrent append/read đạt 85 events/s khi 8 reader thực hiện 435 truy vấn.
Database dùng 16.008 GB, trong đó read_assertions chiếm 11.986 GB và
projection_events 1.826 GB. Query p95 cao nhất ở concurrency 32 là 46.35 ms
cho traversal; mọi query p95 vẫn nằm trong gate. Python RSS không gồm
PostgreSQL server hoặc tiến trình hệ thống.

L2 pre-COPY không đạt projection minimum 10,000 events/s; half-rate giảm
29.74%, và batch cuối mất 2.469 giây cho 10K events so với 0.960 giây ở batch
đầu. Instrumented prefix profile ghi nhận assertion batches chiếm 41.3% thời
gian quan sát và ledger/checkpoint chiếm 20.2%. Sau đổi ledger staging sang
COPY, ledger/checkpoint giảm từ 24.05 xuống 15.50 giây trên cùng prefix; L1
retest đạt 9,557 events/s. Assertion batches tiếp tục chiếm 45.1% trong profile
sau thay đổi. Đây là timing ở ranh giới adapter, không phải SQL CPU profile.
L2 chưa được chạy lại với code COPY mới. L3/L4 chưa chạy; cần tiếp tục tối ưu
và đo L2 hiện tại trước khi thử quy mô lớn hơn. Chưa đủ bằng chứng để gọi bất
kỳ level nào là production-qualified.
Chi tiết và giới hạn đo nằm trong
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
