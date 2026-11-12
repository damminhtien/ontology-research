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

Entity identifiers are generated for each fresh seed run, and the accepted
records determine the event count. Read the receipts instead of relying on
fixed UUIDs or a copied count.

## Bước 2 — Đọc từng kết quả

| Tình huống | Kết quả | Vì sao |
|-----------|---------|--------|
| Entity mới có danh tính rõ | accepted | Tạo `EntityCreated` sau khi qua validation |
| Tên mơ hồ, khớp nhiều candidate | review | Không tự merge; ghi `ResolutionReviewQueued` |
| Observation của entity đã resolve | accepted nếu conform | Đi qua SHACL gate |
| Observation mơ hồ hoặc sai contract | pending/rejected theo trường hợp | Identity receipt và SHACL report nêu nguyên nhân |

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

Mỗi dòng là một JSON object: `event_id`, `event_type` (ví dụ `EntityCreated`,
`AssertionMade`, `ObservationRecorded`, `ResolutionReviewQueued`), `schema_version`, `occurred_at`
(record-time), `valid_at` (valid-time, `null` = trùng record-time), `sequence`
(vị trí 1-based trong log), `payload`. Đây là **write model** — canonical truth
của hệ thống. Contract chi
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

Khi hai canonical entity hoá ra là một thực thể thật (duplicate của chính nguồn,
hoặc hai lượt chạy ghi vào hai log khác nhau), repair bằng merge tool — không
sửa log, chỉ append `EntityMerged`:

```bash
.venv/bin/python tools/merge_entities.py \
    --survivor urn:world:entity:<hex> --duplicate urn:world:entity:<hex> \
    --reason "duplicate QID pair confirmed"
```

Tool rebuild registry từ log trước, từ chối merge sai (unknown id, type
conflict, self-merge) mà không ghi gì. Undo merge sai: `tools/split_entity.py`
(same flags) — restore set đọc từ `EntityMerged`, split không có merge record
bị reject. Chi tiết: [ADR-0007](../adr/ADR-0007-under-merge-repair-via-append-only-merge-events.md).

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
| Corrections | Human decision | merge/split/backfill/import CLI | `EntityMerged`, `EntitySplit`, `ExternalIdBound`, `ResolutionReviewQueued`, `AssertionSuperseded` |

Tracking writes the observation act as `core:Observation` and its reported
position, typed measurement values, units and evidence as a separate
`tracking:ObservationResult`. Position is sensor output; it does not itself
assert `core:locatedAt`. Source IDs and inline evidence use the same normalized
provenance model as assertions.

Document lane theo nguyên tắc *"LLM chỉ đề xuất; semantic system quyết định"*:
`PatternExtractor` (deterministic VI/EN, không cần LLM) làm mặc định,
`LlmExtractor` pluggable qua completion callable; candidate không bao giờ
auto-mint — subject unresolved thì vào review queue, confidence cap 0.7,
provenance là citing document.

## Bước 8 — Quan sát trong Console

```bash
make console
```

Mở tab **Data Monitor** tại <http://127.0.0.1:8787/#/monitor>:

- Event log stats: số lượng và event types được seed vào `data/events.jsonl`
- SHACL validation gate: kết quả trên các ontology modules và benchmark datasets
  so với toàn bộ shape files
- Competency queries: 41 executable queries, trạng thái pass/fail từng câu

Tab **Dashboard** có card "Events logged" hiển thị cùng số liệu.

## Bài tập

1. Thêm một location assertion hợp lệ cho Patrol Vessel 01 qua pipeline, chạy lại
   `--force`, rồi mở Console xem location projection được cập nhật. Event mới
   phải là `AssertionMade` với predicate `core:locatedAt`.
2. Thử ingest một observation với `confidence=1.5`. Điều gì xảy ra và ở lớp nào
   (pipeline hay SHACL)?

## Điều gì vừa xảy ra

Bạn đã chạy ingestion pipeline thật và thấy cả ba lớp bảo vệ hoạt động: identity
review gate, entity-đã-biết check, SHACL validation. Đây chính là "data plane"
mà mọi source dữ liệu sau này sẽ đi qua — kể cả LLM extraction trong tương lai.

## Tiếp theo

- [Tutorial 04 — Read model & benchmark](04-read-model.md): dữ liệu trong log trở
  thành read model phục vụ query nhanh thế nào.
