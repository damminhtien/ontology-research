# Data contract changelog

Nhật ký phiên bản của **hợp đồng dữ liệu** — độc lập với SemVer của ontology
modules (xem `docs/CHANGELOG.md`, sinh tự động từ registry). Số phiên bản hiện
hành nằm trong file `VERSION` ở gốc repo; source code đọc qua
`foundry/versioning.py` và **không bao giờ hardcode** số phiên bản.

## event_schema

### 3 — 2026-09-17

- `AssertionMade` payload: `predicate` (tên quan hệ dạng chuỗi, vd `locatedAt`)
  → **`predicate_iri`** (IRI tuyệt đối, vd
  `https://…/ontology/core#locatedAt`). Root cause: RDF mapping không đưa quan
  hệ vào graph nên hai assertion chỉ khác predicate sinh ra **cùng một** RDF
  form. Nay `assertion:predicate` là một node trong graph
  (`ontology/middle/assertion.ttl`).
- Cùng thay đổi: `assertion:Assertion` không còn ⊑ `core:Event` mà ⊑
  `core:InformationObject` (event ghi thay đổi mệnh đề là `AssertionMade`);
  object dạng literal map sang `assertion:literalValue` thay vì `core:name`
  trên assertion node.
- Tên quan hệ dạng trần (`locatedAt`) resolve về vocabulary `core`; quy tắc nằm
  một chỗ — `foundry.namespaces.resolve_predicate_iri` — dùng chung bởi write
  path (`foundry/assertions.py`) và upcaster.
- Upcaster v2→v3 đăng ký trong `UPCASTERS` (`foundry/events.py`): record v2 vẫn
  replay được — ADR-0002/0009; regression test đọc một record v2 `AssertionMade`
  trong `tests/test_events.py`. Log trong repo hiện không có record
  `AssertionMade` v2 nào, nhưng producer đã tồn tại từ Phase 2 nên bump kèm
  upcaster thay vì amend im lặng.

### 2 — amendment 2026-09-02

- `AffiliationAssessed` **xoá khỏi `EVENT_TYPES`** — superseded bởi generic
  `AssertionMade` (predicate `memberOf` qua assertion lane). Không producer
  từng tồn tại và 0 record trong mọi log, nên không cần upcaster; type này
  trở thành "unknown event type" ở các consumer cũ nếu log lịch sử có
  (thực tế: không có).

### 2 — 2026-08-31

- Thêm `sequence` (offset 1-based do `EventLog` gán khi append; replay phát hiện
  discontinuity) và `valid_at` (valid-time, tách khỏi record-time `occurred_at`).
- Upcaster v1→v2 đăng ký trong `UPCASTERS` (`foundry/events.py`); log v1 vẫn
  replay được — ADR-0009.

### 1 — 2026-08-26

- Hợp đồng đầu tiên: `event_id`, `event_type`, `occurred_at`, `payload` — ADR-0002.

## lake

### 1 — 2026-08-30

- Parquet + zstd, partition theo `event_date` (Hive-style), manifest v1 với
  `lake_version` trong `manifest.json`.

## Quy tắc bump phiên bản

1. Sửa file `VERSION` (key tương ứng).
2. Thêm entry vào chính file này: ngày, nội dung thay đổi, ADR tham chiếu.
3. Riêng `event_schema`: đăng ký upcaster từ phiên trước trong `UPCASTERS`
   (`foundry/events.py`) + regression test đọc log phiên cũ — log đã ghi không
   bao giờ bị rewrite (ADR-0002).
4. `make check` phải xanh trong cùng commit.

## reference_lane

### 1 — 2026-09-11

- Lane snapshot đầu tiên: typed Parquet theo `class_qid` (cột `qid`, `name_vi`,
  `name_en`, `type_qids`, `class_qid`, `fetched_at`), manifest-authoritative
  (`snapshots[]` + `latest`), publish atomic qua tmp+rename — ADR-0002/
  architecture §4.6. Lane là reference data, rebuild được độc lập, không bao giờ
  là nguồn sự thật.
