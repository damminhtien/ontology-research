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
| Location state and conflicts as of an instant | `location_resolution(id, instant)` | 500 ms |

## Bước 1 — Chạy benchmark

```bash
make benchmark
```

The command reports synthetic ingest, exact alias lookup, streaming projection,
query latency, peak process RSS and event-log bytes, then writes
`build/benchmark-report.json`. Measurements depend on the machine and run; the
committed query SLO baseline is in `benchmarks/baseline.json`.

!!! note "Đọc số liệu đúng cách"
    Event log và projector đọc theo luồng, nhưng identity/read-model indexes vẫn
    in-memory. Synthetic ingest tính cả tạo event và ghi log (không qua SHACL);
    đây không phải tốc độ production. Recovery trong report là full replay vào
    model mới, không phải thời gian restore snapshot. Chỉ so RSS giữa các lần
    chạy riêng process. Query p95 là percentile của các probe trung bình 100 lần
    gọi warm; baseline SLO dùng cùng phép đo.

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

The read model builds location history from generic `AssertionMade` records
whose predicate is `core:locatedAt`. Validity uses a half-open interval
`[valid_from, valid_until)`. A replacement assertion marks the referenced
assertion corrected; `AssertionSuperseded` retracts a claim. Both operations
refresh the current location index during event application. If multiple
non-superseded locations are valid at the same instant, the model reports a
conflict and does not pick a winner based on event order.

The general assertion ledger also supports time varying relationships.
`assertions_as_of(subject_id, instant, predicate_iri)` filters any predicate by
the same half-open interval. For example, membership can end at one instant and
a new membership can begin at that exact boundary without overwriting history.
Projection statistics count only entities with one unique active location;
expired or conflicting claims do not count as a current location.

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
