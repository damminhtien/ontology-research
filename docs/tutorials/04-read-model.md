# Tutorial 04 — Read model & benchmark

!!! info "Thời lượng ước tính"
    20 phút. Cần hoàn thành [Tutorial 03](03-ingestion.md).

## CQRS: write model ≠ read model

```text
Event log (write) ──► Projector ──► ReadModel (read)
   correctness            fold          speed
```

- **Write model** (`~/Data/ontology-research/data/events.jsonl` cho demo;
  `~/Data/ontology-research/data/production.jsonl` cho production): append-only,
  đầy đủ provenance.
- **Projector** (`foundry/projector.py`): fold log thành read model; replay
  theo `sequence`, checkpointed và idempotent theo từng event.
- **ReadModelStore**: PostgreSQL indexes trả lời query nóng trong operational
  mode; `ReadModel` in-memory vẫn dùng cho tests và offline demos. Cả hai không
  gọi ontology/SPARQL ([ADR-0004](../adr/ADR-0004-cqrs-read-model.md)).

`QueryService` gọi read model qua `OperationalQueryAdapter`; API dùng service
này thay vì truy cập trực tiếp cấu trúc dữ liệu của adapter. SPARQL CQ chạy trên
đường semantic riêng. Danh sách 10 operational OSINT queries và giới hạn từng
query có trong [query contract](https://github.com/damminhtien/ontology-research/blob/main/requirements/osint_queries.md).

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

Lệnh này đo synthetic ingest, exact alias lookup, streaming projection, query
latency, peak process RSS và event-log bytes, rồi ghi
`build/benchmark-report.json`. Kết quả phụ thuộc máy và lượt chạy; query SLO
baseline đã commit nằm ở `benchmarks/baseline.json`.

!!! note "Đọc số liệu đúng cách"
    Benchmark tổng hợp giữ in-memory model để so sánh với baseline lịch sử;
    đây không phải phép đo PostgreSQL. Synthetic ingest tính cả tạo event và
    ghi log (không qua SHACL). Recovery trong report là full replay vào model
    mới. Chỉ so RSS giữa các lần chạy riêng process. Query p95 là percentile
    của các probe trung bình 100 lần gọi warm; baseline SLO dùng cùng phép đo.

Operational setup dùng PostgreSQL worker và data root bên ngoài worktree. Xem
[hướng dẫn PostgreSQL](../guides/postgres-readmodel.md) để migrate, catch up,
rebuild và kiểm tra sau restart.

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

`make check` cũng chạy `tools/benchmark_e2e.py --check` để kiểm tra floor tuyệt
đối và giữ throughput của ingest, lake persist, projector ở mức ít nhất 1/1.5
baseline. Lake query được gate theo latency SLO riêng.

```bash
.venv/bin/python tools/check_slo.py --generate-baseline
```

## Bước 3 — Xem trong Console

Read model tạo location history từ `AssertionMade` có predicate
`core:locatedAt`. Validity dùng khoảng nửa mở `[valid_from, valid_until)`. Khi
replacement assertion được áp dụng, assertion cũ chuyển sang trạng thái
superseded; `AssertionSuperseded` rút lại claim. Cả hai cập nhật location index.
Nếu nhiều location assertion còn hiệu lực cùng lúc, model trả conflict thay vì
chọn theo thứ tự event.

Assertion ledger cũng hỗ trợ quan hệ thay đổi theo thời gian.
`assertions_as_of(subject_id, instant, predicate_iri)` lọc mọi predicate theo
cùng khoảng thời gian. Ví dụ, membership cũ có thể kết thúc đúng lúc membership
mới bắt đầu mà không ghi đè history. Projection statistics chỉ đếm entity có
một location duy nhất đang hiệu lực; claim hết hạn hoặc conflict không được tính
là current location.

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

## Kết quả

Benchmark này đo event log, projection replay và query latency cho đường
in-memory. PostgreSQL và Oxigraph được đánh giá riêng trên workload chung; thời
gian đo ở mỗi adapter bao gồm các tầng xử lý khác nhau.

## Tiếp theo

- [Tutorial 05 — Versioning & governance](05-versioning.md): kiểm soát tiến hóa của
  ontology bằng SemVer.
