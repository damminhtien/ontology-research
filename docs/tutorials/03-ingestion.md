# Tutorial 03 — Ingestion pipeline

!!! info "Thời lượng ước tính"
    25 phút. Cần hoàn thành [Tutorial 02](02-ontology-shacl.md).

## Pipeline chuẩn

```text
Source → Schema normalization → Identity resolution → Ontology mapping
      → SHACL gate → Append-only event log
```

Ba nguyên tắc thiết kế:

- **Append-only**: history không bao giờ bị overwrite; corrections là event mới
  ([ADR-0002](../adr/ADR-0002-append-only-event-log.md)).
- **Identity precision-first**: fuzzy match KHÔNG tự merge — chuyển sang hàng đợi
  review ([ADR-0003](../adr/ADR-0003-identity-resolution.md)).
- **SHACL gate**: dữ liệu vi phạm contract bị reject với structured receipt,
  không drop im lặng.

## Bước 1 — Chạy pipeline end-to-end

```bash
.venv/bin/python tools/seed_console_data.py --force
```

```text
accepted Coast Guard Region 4 -> urn:world:entity:f9ce072e2d82486ba5909a9c3c13e605
accepted Maritime Headquarters -> urn:world:entity:52c53fa93efa4660bac315d2f3f64bea
accepted Patrol Vessel 01 -> urn:world:entity:f073683a41e646ccb51998ab68eaa9bb
rejected Patrol Vessel 02 ->
accepted observation of Patrol Vessel 01
rejected observation of Patrol Vessel 02
rejected (expected) unresolved-entity reference: entity reference matches candidates
('urn:world:entity:f073683a41e646ccb51998ab68eaa9bb',); resolve via exact alias or
external id first

Seeded 4 events into .../data/events.jsonl
```

## Bước 2 — Đọc từng kết quả

| Record | Kết quả | Vì sao |
|--------|---------|--------|
| Coast Guard Region 4 | ✅ accepted | Entity mới, tạo `urn:world:entity:<uuid>` |
| Patrol Vessel 01 | ✅ accepted | Entity mới |
| **Patrol Vessel 02** | ❌ rejected | Overlap token với "Patrol Vessel 01" → phương án an toàn là **review**, không merge bừa |
| Observation của Vessel 01 | ✅ accepted | Entity đã biết, qua SHACL gate |
| Observation của Vessel 02 | ❌ rejected | Entity chưa được resolve |

!!! tip "Đây là tính năng, không phải bug"
    "Vessel 02" rất có thể là đơn vị khác "Vessel 01". Merge nhầm hai entity là lỗi
    đắt nhất trong hệ thống knowledge — pipeline thà chuyển sang review queue.

## Bước 3 — Xem event log

```bash
cat data/events.jsonl | .venv/bin/python -m json.tool --json-lines | head -20
```

Mỗi dòng là một JSON object: `event_id`, `event_type` (`EntityCreated` /
`LocationObserved`), `schema_version`, `occurred_at`, `payload`. Đây là **write
model** — canonical truth của hệ thống.

## Bước 4 — Quan sát trong Console

```bash
make console
```

Mở tab **Data Monitor** tại <http://127.0.0.1:8787/#/monitor>:

- Event log stats: 4 events (3 EntityCreated, 1 LocationObserved)
- SHACL validation gate: CONFORMS
- Competency queries: ALL 6 PASS

Tab **Dashboard** có card "Events logged" hiển thị cùng số liệu.

## Bài tập

1. Thêm một `LocationObserved` hợp lệ cho Patrol Vessel 01 vào code seed, chạy lại
   `--force`, rồi mở Console xem `with_location` tăng lên.
2. Thử ingest một observation với `confidence=1.5`. Điều gì xảy ra và ở lớp nào
   (pipeline hay SHACL)?

## Điều gì vừa xảy ra

Bạn đã chạy ingestion pipeline thật và thấy cả ba lớp bảo vệ hoạt động: identity
review gate, entity-đã-biết check, SHACL validation. Đây chính là "data plane"
mà mọi source dữ liệu sau này sẽ đi qua — kể cả LLM extraction trong tương lai.

## Tiếp theo

- [Tutorial 04 — Read model & benchmark](04-read-model.md): dữ liệu trong log trở
  thành read model phục vụ query nhanh thế nào.
