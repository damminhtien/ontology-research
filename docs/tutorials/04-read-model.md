# Tutorial 04 — Read model & benchmark

!!! info "Thời lượng ước tính"
    20 phút. Cần hoàn thành [Tutorial 03](03-ingestion.md).

## CQRS: write model ≠ read model

```text
Event log (write) ──► Projector ──► ReadModel (read)
   correctness            fold          speed
```

- **Write model** (`data/events.jsonl` cho demo; `data/production.jsonl` cho
  production): append-only, đầy đủ provenance.
- **Projector** (`foundry/projector.py`): fold log thành read model; replay
  theo `sequence`, checkpointed và idempotent theo từng event.
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

The command reports append, identity lookup, projection throughput and query
latency, then writes `build/benchmark-report.json`. Measurements depend on the
machine and run; the committed SLO baseline is in `benchmarks/baseline.json`.

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

Kết quả `PASSED` nghĩa là các query nằm trong ngưỡng tuyệt đối và không vượt
1.2× baseline. Thời gian và tỷ lệ cụ thể thay đổi theo máy.

Gate này chạy trong `make check`: fail nếu bất kỳ query nào vượt **1.2× baseline**
hoặc **SLO tuyệt đối**. Baseline nằm ở `benchmarks/baseline.json` (median của 5 lần
chạy để giảm nhiễu). Cập nhật baseline sau khi thay đổi hợp lệ:

`make check` cũng chạy `tools/benchmark_e2e.py --check` để kiểm tra các ngưỡng
ingest, lake persist, projector và lake query theo stage.

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
