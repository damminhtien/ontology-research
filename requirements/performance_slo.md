# Performance SLOs

Các SLO này là **engineering contract**, đo bằng benchmark, không phải giả định.
Vi phạm SLO trong regression test → reject release hoặc yêu cầu review.

## Query latency (p95, trên benchmark dataset chuẩn)

| Loại query | Target p95 | Ghi chú |
|------------|-----------:|---------|
| Entity lookup (Q1) | 50 ms | point lookup theo canonical_id |
| Current location (Q2-style) | 100 ms | latest location của một entity |
| 2–3 hop traversal (Q3) | 300 ms | 80% operational queries ≤ 3 hops |
| Temporal as-of (Q4) | 500 ms | newest observation với valid_from ≤ instant |
| Provenance trace (Q5) | 500 ms | assertion → sources |
| Analytical (Q6) | n/a | chạy OLAP, không trên graph store |

Read-model hiện tại implement 3 hot query path chính (Q1, current location, Q4);
các query còn lại sẽ đi theo store backend ở Phase 6.

## Throughput

| Metric | Target |
|--------|-------:|
| Ingestion (synthetic benchmark) | 10^4 – 10^5 events/s |
| Projection replay (synthetic benchmark) | 10^4 events/s |
| Events/day sustained | 10^7 |

## Operational quality gates

```text
EntityResolutionErrorRate < 1%
ShapeViolationRate        < 0.1%
ProjectionLag             < 5s
ValidFacts                > 99.9%
```

## Regression policy

Mỗi release chạy lại toàn bộ competency queries và so sánh:

- answer correctness (so với `benchmarks/expected_results/`);
- p50 / p95 / p99.

Ontology/schema change làm query p95 tăng **> 20% so với baseline** → reject hoặc review bắt buộc.
Baseline được lưu tại `benchmarks/baseline.json` và tạo bởi `tools/check_slo.py --generate-baseline`.

### Read-model SLO gate

`make check` chạy `tools/check_slo.py` với cùng scale/seed như baseline:

- fail nếu bất kỳ query nào có p95 > 1.2× comparison baseline;
- fail nếu bất kỳ query nào có p95 > target p95 trong bảng trên.

Harness: `tools/benchmark.py` — sinh payload tổng hợp với seed cố định rồi đo
synthetic ingest (tạo event + append bền vững vào JSONL, chưa chạy SHACL), exact
alias identity lookup, streaming full replay và warm query latency (p50/p95/p99).
Mỗi query sample là trung bình của 100 lần gọi đã warm; p95 lấy trên ≥ 100 probe
entity để giảm nhiễu timer ở query dưới microsecond. Report
`build/benchmark-report.json` còn ghi peak RSS, dung lượng
log, số segment, thời gian build read-model index và thời gian full-replay.
Recovery ở đây là replay vào read model mới; chưa gồm restore snapshot.

## Quy tắc đo

- Đo trên dataset có kích thước khai báo rõ (hiện tại: seed dataset nhỏ cho correctness;
  scale benchmark tới 100K entities tùy mục đích).
- Latency đo từ API boundary / read-model method, không tính network client-side.
- p95 tính trên 200 probe warm; mỗi probe trung bình 100 lần gọi. Harness này
  chưa đo cold-start.
- So sánh peak RSS chỉ từ lần chạy benchmark độc lập; trong process test dùng chung,
  high-water mark có thể gồm các phép đo chạy trước đó.
- Baseline được ghi trên máy cố định; CI sẽ so sánh với baseline đã commit.
- Baseline Q1 dưới microsecond, trong khi runner khác nhau có thể lệch vài
  microsecond. Relative gate dùng comparison floor 0.0038 ms cho Q1; absolute
  target 50 ms vẫn áp dụng. Các query còn lại so sánh trực tiếp với p95
  baseline đã commit.

Số đo thử scale 100K hiện có cùng giới hạn và ngày đo được ghi tại
[`scale_targets.md`](scale_targets.md); nó chưa hoàn thành rung 10M/100M.
