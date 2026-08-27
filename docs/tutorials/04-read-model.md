# Tutorial 04 — Read model & benchmark

!!! info "Thời lượng ước tính"
    20 phút. Cần hoàn thành [Tutorial 03](03-ingestion.md).

## CQRS: write model ≠ read model

```text
Event log (write) ──► Projector ──► ReadModel (read)
   correctness            fold          speed
```

- **Write model** (`data/events.jsonl`): append-only, đầy đủ provenance.
- **Projector** (`foundry/projector.py`): fold log thành read model; replay
  deterministic theo `(occurred_at, event_id)`, idempotent — test chặn vi phạm.
- **ReadModel** (`foundry/readmodel.py`): in-memory indexes trả lời query nóng
  không đụng ontology/SPARQL ([ADR-0004](../adr/ADR-0004-cqrs-read-model.md)).

Ba hot query của read model:

| Query | Method | SLO p95 |
|-------|--------|--------:|
| Q1 entity lookup | `get_entity(id)` | 50 ms |
| Current location | `current_location(id)` | 100 ms |
| Q4 temporal as-of | `location_as_of(id, instant)` | 500 ms |

## Bước 1 — Chạy benchmark

```bash
make benchmark
```

```text
Benchmark: 4000 events (1000 entities x 3 observations)

  raw append   :     64,136 events/s
  identity hit :    523,743 lookups/s
  projection   :     55,159 events/s

  query latency (warm, p50/p95/p99 ms):
    q1_entity_lookup_ms       0.0026 /  0.0055 /  0.0292  (SLO 50ms, within SLO)
    q_current_location_ms     0.0054 /  0.0268 /  0.0394  (SLO 100ms, within SLO)
    q4_temporal_ms            0.0051 /  0.0056 /  0.0062  (SLO 500ms, within SLO)

Wrote .../build/benchmark-report.json
```

!!! note "Đọc số liệu đúng cách"
    Đây là số đo trên in-memory scaffold — là **floor** để so sánh regression,
    không phải con số production. Raw append là tốc độ ghi log thuần (không qua
    SHACL gate); production path có thêm validation chi phí cao hơn.

Tăng scale để stress test (không commit kết quả — `build/` đã gitignore):

```bash
.venv/bin/python tools/benchmark.py --scale 10000 --observations 3
```

## Bước 2 — SLO regression gate

```bash
.venv/bin/python tools/check_slo.py
```

```text
Query                        Baseline    Current    Ratio      SLO   Status
------------------------------------------------------------------------------
q1_entity_lookup_ms            0.0038     0.0016    0.42x       50       OK
q_current_location_ms          0.0201     0.0052    0.26x      100       OK
q4_temporal_ms                 0.0158     0.0052    0.33x      500       OK

SLO regression gate PASSED
```

Gate này chạy trong `make check`: fail nếu bất kỳ query nào vượt **1.2× baseline**
hoặc **SLO tuyệt đối**. Baseline nằm ở `benchmarks/baseline.json` (median của 5 lần
chạy để giảm nhiễu). Cập nhật baseline sau khi thay đổi hợp lệ:

```bash
.venv/bin/python tools/check_slo.py --generate-baseline
```

## Bước 3 — Xem trong Console

Mở tab **Projection** tại <http://127.0.0.1:8787/#/projection>:

- Cards: Entities / With location / Distinct locations / Projection lag
- Biểu đồ cột p95 latency vs SLO (D3)
- Bảng p50/p95/p99 đầy đủ

Card **Projection** trên Dashboard hiển thị projection lag so với SLO 5s.

## Bài tập

1. Chạy `--scale 10000`, so sánh projection events/s với scale 1000. Latency query
   thay đổi thế nào khi số entity tăng 10×?
2. Đọc `foundry/projector.py`: chuyện gì xảy ra với event type chưa biết? Vì sao
   thiết kế "skip, không fatal" lại quan trọng cho forward compatibility?

## Điều gì vừa xảy ra

Bạn đã đo được ba tầng hiệu năng: ghi log, replay projection, và query latency —
với SLO gate bảo vệ mọi release tương lai khỏi performance regression.

## Tiếp theo

- [Tutorial 05 — Versioning & governance](05-versioning.md): kiểm soát tiến hóa của
  ontology bằng SemVer.
