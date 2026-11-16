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

Mỗi lượt seed mới tạo entity ID khác nhau; số record được chấp nhận quyết định
số event. Đọc receipt của lượt chạy để lấy kết quả thay vì dùng UUID hoặc số
lượng chép từ lần trước.

## Bước 2 — Đọc từng kết quả

| Tình huống | Kết quả | Vì sao |
|-----------|---------|--------|
| Entity mới có identity rõ | Được nhận | Tạo `EntityCreated` sau validation |
| Tên mơ hồ, khớp nhiều candidate | Chờ review | Ghi `ResolutionReviewQueued`; không tự merge |
| Observation của entity đã resolve | Được nhận nếu conform | Đi qua SHACL gate |
| Observation mơ hồ hoặc sai contract | Pending hoặc bị từ chối | Identity receipt và SHACL report nêu nguyên nhân |

!!! tip "Tên trùng cần được review"
    "Vessel 02" có thể là một đơn vị khác "Vessel 01". Trùng tên tạo candidate
    để xem xét; cần identity evidence trước khi merge hai canonical entity.

!!! note "Ngoại lệ: nguồn có external id có thẩm quyền (ADR-0006)"
    Khi record mang `external_id` toàn cục từ nguồn có thẩm quyền (ví dụ Wikidata
    QID) và external id đó miss, danh tính đã được nguồn khẳng định: fuzzy match
    **không** ép review nữa — pipeline tạo entity mới và bind external id. Hai
    record QID khác nhau luôn là hai entity, dù tên gần giống nhau. Luồng review
    chỉ còn áp dụng cho nguồn không có external id (như seed console phía trên).

## Bước 3 — Xem event log

```bash
EVENT_DATA_ROOT="${FOUNDRY_DATA_ROOT:-$HOME/Data/ontology-research}/data"
cat "$EVENT_DATA_ROOT/events.jsonl" | .venv/bin/python -m json.tool --json-lines | head -20
```

Mỗi dòng là một JSON object: `event_id`, `event_type` (ví dụ `EntityCreated`,
`AssertionMade`, `ObservationRecorded`, `ResolutionReviewQueued`), `schema_version`, `occurred_at`
(recorded time), `valid_at` (valid-time, `null` = trùng recorded time), `sequence`
(số thứ tự event 1-based tăng dần qua các segment), `payload`. Đây là **write
model**, nguồn sự thật chuẩn của hệ thống. Xem contract tại
[ADR-0009](../adr/ADR-0009-event-contract-v2-sequence-valid-time-upcasters.md).

Với `EntityCreated`, payload chứa `name` (tên chính), `name_aliases` (các tên
phụ — ví dụ nhãn tiếng Anh khi ingesting dữ liệu Wikidata song ngữ Việt–Anh) và
`external_ids` (định danh nguồn, ví dụ Wikidata QID). Alias và external id được
bind vào canonical identity để các tham chiếu sau này resolve chính xác, và được
persist cùng event nên read model/lake đều phục vụ được dữ liệu đa ngôn ngữ.
Source record có stable key và content hash; ingest lại cùng key và nội dung là
idempotent. Document có version mới khi nội dung đổi. Assertion giữ recorded
time, valid-time, nguồn và evidence locator/span. Model score và chất lượng
evidence được lưu riêng, không biểu thị xác suất fact đúng.

## Bước 4 — Nạp dữ liệu thật từ Wikidata

Nguồn production chính hiện nay là Wikidata SPARQL (song ngữ Việt–Anh), ghi vào
log riêng `~/Data/ontology-research/data/production.jsonl`:

Trong cấu hình vận hành local, `data/` là symlink tới
`~/Data/ontology-research/data`; xem [hướng dẫn PostgreSQL](../guides/postgres-readmodel.md)
để cấu hình identity store và projector worker.

```bash
# nạp tổ chức (mặc định Q43229); các lớp khác: Q3918 university,
# Q16917 hospital, Q33506 museum, Q23691 airport, Q176799 military unit
.venv/bin/python tools/ingest_wikidata.py --class Q3918 --limit 3000
```

Điểm chính của luồng này:

- **Registry lấy từ persistent identity index**: khi dùng PostgreSQL, CLI yêu
  cầu projector đã theo kịp event log; nếu chưa, hãy chạy projector trước.
  Ingestion dùng index để dedupe theo source record và external ID — re-fetch
  không tạo entity mới
  ([ADR-0010](../adr/ADR-0010-registry-as-log-projection.md)).
- **QID là external id đáng tin** (ADR-0006): fuzzy match không ép review khi
  nguồn đã xác nhận định danh; rejection và unresolved rate vẫn phụ thuộc dữ liệu
  của từng lượt ingest.
- **Binding là sự kiện**: external id bind sau khi entity đã tồn tại (alias-hit)
  được ghi thành `ExternalIdBound` event — restart không mất định danh.
- **SHACL gate**: record vi phạm contract vẫn bị reject như luồng thường.

Snapshot production trong audit ngày 2026-09-02 có **24.217 canonical
entities** (23.039 tổ chức/đơn vị quân đội + 1.178 university) và 47.628 events,
mirror sang lake Parquet. Đây là snapshot lịch sử; production log không nằm
trong repo nên hãy đo lại trước khi dùng các con số này làm trạng thái hiện tại.

## Bước 5 — Sửa under-merge khi phát hiện

Khi có bằng chứng xác nhận hai canonical entity là cùng một thực thể, dùng merge
tool để append `EntityMerged`. Event ghi same-entity evidence; log hiện có không
bị sửa.

```bash
.venv/bin/python tools/merge_entities.py \
    --survivor urn:world:entity:<hex> --duplicate urn:world:entity:<hex> \
    --reason "duplicate QID pair confirmed"
```

Đoạn lệnh trên chỉ minh họa hai entity cần merge. CLI hiện yêu cầu thêm
`--evidence-id`, `--evidence-source`, `--evidence-basis` và `--recorded-by`; xem
[CLI reference](../guides/cli-reference.md) để biết cú pháp đầy đủ.

Ở chế độ in-memory, tool dựng identity registry từ log. Khi dùng PostgreSQL,
projector phải theo kịp log trước khi chạy lệnh identity; tool dùng persistent
registry và để projector áp dụng correction. Merge bị từ chối nếu ID không tồn
tại, entity type xung đột, self-merge, duplicate đã merge hoặc thiếu bằng chứng
hợp lệ. `tools/split_entity.py` append `EntitySplit` để hoàn tác merge có trong
log. Chi tiết: [ADR-0007](../adr/ADR-0007-under-merge-repair-via-append-only-merge-events.md)
và [hướng dẫn PostgreSQL](../guides/postgres-readmodel.md).

Toàn bộ các luồng sửa dữ liệu + data lanes gom ở
[Workflows guide](../guides/workflows.md).

## Bước 6 — Assertion về entity chưa mint (pending)

Một location assertion từ nguồn nói về entity chưa tồn tại trong registry vẫn
được lưu với `identity:UnresolvedReference` riêng, không mint một entity giả.
Read model chỉ liên kết assertion khi tên hoặc alias resolve tới đúng một
canonical entity; nếu có nhiều ứng viên thì reference vẫn pending/ambiguous:

```python
pending = pipeline.ingest_location_observation(
    ...
)  # subject chưa có → AssertionMade với subject_ref
created = pipeline.ingest_entity(name=...)  # mint sau
# → liên kết khi identity duy nhất; nếu tên mơ hồ thì vẫn pending
```

Ambiguity thật (một tên, hai entity cùng loại) vẫn vào review queue. Chi tiết:
`docs/architecture.md` §4.7.

## Bước 7 — Bốn lane dữ liệu

| Lane | Đầu vào | Công cụ | Event types |
|------|---------|---------|-------------|
| Reference (Wikidata) | SPARQL / Parquet snapshot | `build_reference_lane.py` → `ingest_wikidata.py --from-lane` | `EntityCreated`, `ExternalIdBound` |
| Tracking (AIS feed) | JSONL sensor feed (`position_uri`, typed `measurements`) | `ingest_tracking_feed.py` | `SensorRegistered`, `ObservationRecorded` → `ObservationResult`, `TrackObserved` |
| Documents (văn bản) | Báo cáo/tin — extractor *đề xuất* | `pipeline.ingest_document(...)` | `DocumentRegistered`, `AssertionMade` |
| Corrections | Quyết định đã review | merge/split/backfill/import CLI | `EntityMerged`, `EntitySplit`, `ExternalIdBound`, `ResolutionReviewQueued`, `AssertionSuperseded` |

Tracking ghi hành động quan sát thành `core:Observation`; vị trí, measurement có
kiểu, unit và evidence được lưu trong `tracking:ObservationResult`. Một fact
`core:locatedAt` cần được tạo riêng như assertion. Source ID và inline evidence
dùng cùng provenance model đã chuẩn hóa.

Document lane theo nguyên tắc *"LLM chỉ đề xuất; semantic system quyết định"*:
`PatternExtractor` (deterministic VI/EN, không cần LLM) làm mặc định,
`LlmExtractor` có thể thay thế qua completion callable; candidate không tự mint.
Subject chưa resolve thì vào review queue. Extractor ghi `model_score` tối đa
0.7; chất lượng evidence được lưu riêng, và score này không phải xác suất fact
đúng. Trường `confidence` là alias tương thích cũ.

## Bước 8 — Quan sát trong Console

```bash
make console
```

Mở tab **Data Monitor** tại <http://127.0.0.1:8787/#/monitor>:

- Event log stats: số lượng và event types được seed vào
  `~/Data/ontology-research/data/events.jsonl`
- SHACL validation gate: kết quả trên các ontology modules và benchmark datasets
  so với toàn bộ shape files
- Competency queries: 41 executable queries, trạng thái pass/fail từng câu

Tab **Dashboard** có card "Events logged" hiển thị cùng số liệu.

## Bài tập

1. Nạp lại cùng source record với cùng content hash. Kiểm tra event/document
   không bị nhân đôi; sau đó đổi nội dung và kiểm tra document version mới.
2. Tạo một location assertion hợp lệ cho Patrol Vessel 01. Xác nhận event mới là
   `AssertionMade` với predicate `core:locatedAt` và có evidence lineage.

## Kết quả

Pipeline kiểm tra identity, SHACL và provenance trước khi commit event. Mọi data
lane ghi vào cùng event log làm nguồn sự thật.

## Tiếp theo

- [Tutorial 04 — Read model & benchmark](04-read-model.md): dữ liệu trong log trở
  thành read model phục vụ query nhanh thế nào.
