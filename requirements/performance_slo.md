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

- fail nếu bất kỳ query nào có p95 > 1.2× baseline;
- fail nếu bất kỳ query nào có p95 > target p95 trong bảng trên.

Harness: `tools/benchmark.py` — deterministic synthetic log (fixed seed),
đo raw append, identity hot path, full projection replay, và warm query latency
(p50/p95/p99, ≥ 100 runs). Kết quả xuất ra `build/benchmark-report.json`.

## Quy tắc đo

- Đo trên dataset có kích thước khai báo rõ (hiện tại: seed dataset nhỏ cho correctness;
  scale benchmark dùng synthetic 1K–100K entities tùy mục đích).
- Latency đo từ API boundary / read-model method, không tính network client-side.
- p95 tính trên ≥ 100 lần chạy warm cache, ghi nhận cả cold-start riêng.
- Baseline được ghi trên máy cố định; CI sẽ so sánh với baseline đã commit.
