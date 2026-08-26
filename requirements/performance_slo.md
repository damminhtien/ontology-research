# Performance SLOs

Các SLO này là **engineering contract**, đo bằng benchmark, không phải giả định.
Vi phạm SLO trong regression test → reject release hoặc yêu cầu review.

## Query latency (p95, trên benchmark dataset chuẩn)

| Loại query | Target p95 | Ghi chú |
|------------|-----------:|---------|
| Entity lookup (Q1) | 50 ms | point lookup theo canonical_id |
| 1-hop relationship (Q2) | 100 ms | index hit trực tiếp |
| 2–3 hop traversal (Q3) | 300 ms | 80% operational queries ≤ 3 hops |
| Temporal as-of (Q4) | 500 ms | dùng read model temporal-indexed |
| Provenance trace (Q5) | 500 ms | assertion → sources |
| Analytical (Q6) | n/a | chạy OLAP, không trên graph store |

## Throughput

| Metric | Target |
|--------|-------:|
| Ingestion (synthetic benchmark) | 10^4 – 10^5 events/s |
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

Ontology/schema change làm query p95 tăng **> 20%** → reject hoặc review bắt buộc.

## Quy tắc đo

- Đo trên dataset có kích thước khai báo rõ (hiện tại: seed dataset nhỏ cho correctness;
  scale benchmark sẽ dùng synthetic 10M/100M rồi 100M/1B).
- Latency đo từ API boundary, không tính network client-side.
- p95 tính trên ≥ 100 lần chạy warm cache, ghi nhận cả cold-start riêng.
- Lưu ý hiện trạng: correctness harness (pytest) chỉ verify kết quả đúng;
  harness đo latency thật sẽ gắn vào Phase 3 khi có read store + dashboard.
