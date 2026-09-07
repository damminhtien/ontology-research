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

!!! note "Ngoại lệ: nguồn có external id có thẩm quyền (ADR-0006)"
    Khi record mang `external_id` toàn cục từ nguồn có thẩm quyền (ví dụ Wikidata
    QID) và external id đó miss, danh tính đã được nguồn khẳng định: fuzzy match
    **không** ép review nữa — pipeline tạo entity mới và bind external id. Hai
    record QID khác nhau luôn là hai entity, dù tên gần giống nhau. Luồng review
    chỉ còn áp dụng cho nguồn không có external id (như seed console phía trên).

## Bước 3 — Xem event log

```bash
cat data/events.jsonl | .venv/bin/python -m json.tool --json-lines | head -20
```

Mỗi dòng là một JSON object: `event_id`, `event_type` (`EntityCreated` /
`LocationObserved`), `schema_version`, `occurred_at` (record-time), `valid_at`
(valid-time, `null` = trùng record-time), `sequence` (vị trí 1-based trong log),
`payload`. Đây là **write model** — canonical truth của hệ thống. Contract chi
tiết: [ADR-0009](../adr/ADR-0009-event-contract-v2-sequence-valid-time-upcasters.md).

Với `EntityCreated`, payload chứa `name` (tên chính), `name_aliases` (các tên
phụ — ví dụ nhãn tiếng Anh khi ingesting dữ liệu Wikidata song ngữ Việt–Anh) và
`external_ids` (định danh nguồn, ví dụ Wikidata QID). Alias và external id được
bind vào canonical identity để các tham chiếu sau này resolve chính xác, và được
persist cùng event nên read model/lake đều phục vụ được dữ liệu đa ngôn ngữ.

## Bước 4 — Nạp dữ liệu thật từ Wikidata

Nguồn production chính hiện nay là Wikidata SPARQL (song ngữ Việt–Anh), ghi vào
log riêng `data/production.jsonl`:

```bash
# nạp tổ chức (mặc định Q43229); các lớp khác: Q3918 university,
# Q16917 hospital, Q33506 museum, Q23691 airport, Q176799 military unit
.venv/bin/python tools/ingest_wikidata.py --class Q3918 --limit 3000
```

Điểm chính của luồng này:

- **Registry derive từ log**: CLI rebuild toàn bộ identity registry từ event log
  trước khi nạp, nên chạy tiếp bao nhiêu lần cũng dedupe với dữ liệu đã có —
  re-fetch là external-id hit, không tạo entity mới
  ([ADR-0010](../adr/ADR-0010-registry-as-log-projection.md)).
- **QID là external id đáng tin** (ADR-0006): fuzzy match không ép review;
  `unresolved_rate` thực tế = 0%.
- **Binding là sự kiện**: external id bind sau khi entity đã tồn tại (alias-hit)
  được ghi thành `ExternalIdBound` event — restart không mất định danh.
- **SHACL gate**: record vi phạm contract vẫn bị reject như luồng thường.

Hiện trạng lake sau các lượt nạp thật: **24.217 canonical entities** (23.039 tổ
chức/đơn vị quân đội + 1.178 university), 47.628 events, mirror 1:1 sang lake
Parquet.

## Bước 5 — Sửa under-merge khi phát hiện

Khi hai canonical entity hoá ra là một thực thể thật (duplicate của chính nguồn,
hoặc hai lượt chạy ghi vào hai log khác nhau), repair bằng merge tool — không
sửa log, chỉ append `EntityMerged`:

```bash
.venv/bin/python tools/merge_entities.py \
    --survivor urn:world:entity:<hex> --duplicate urn:world:entity:<hex> \
    --reason "duplicate QID pair confirmed"
```

Tool rebuild registry từ log trước, từ chối merge sai (unknown id, type
conflict, self-merge) mà không ghi gì. Chi tiết:
[ADR-0007](../adr/ADR-0007-under-merge-repair-via-append-only-merge-events.md).

## Bước 6 — Quan sát trong Console

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
