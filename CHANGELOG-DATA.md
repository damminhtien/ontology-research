# Data contract changelog

Nhật ký phiên bản của **hợp đồng dữ liệu** — độc lập với SemVer của ontology
modules (xem `docs/CHANGELOG.md`, sinh tự động từ registry). Số phiên bản hiện
hành nằm trong file `VERSION` ở gốc repo; source code đọc qua
`foundry/versioning.py` và **không bao giờ hardcode** số phiên bản.

## event_schema

### 4 — 2026-11-12

- Location is now recorded through the generic `AssertionMade` contract. The
  v3→v4 upcaster converts historical `LocationObserved` records to
  `AssertionMade(predicate_iri=core:locatedAt)` while preserving event ID,
  sequence and source references. Its deterministic legacy assertion ID makes
  replay stable; unresolved subjects remain typed references, never entities.
  `LocationObserved` is no longer an accepted event type for new writes.
- `ObservationRecorded` projects a `core:Observation` event plus a distinct
  `tracking:ObservationResult` information object. The upcaster preserves the
  legacy `location_uri` as `position_uri` and supplies an empty measurement list
  when old records have no typed measurements. Sensor positions do not become
  `core:locatedAt` assertions.
- Assertions use the half-open valid-time interval
  `[valid_from, valid_until)`; old `valid_to` payload values are read as
  `valid_until`. Corrections and retractions remain append-only events and now
  refresh the location projection. Conflicting active claims are exposed as a
  conflict, not silently resolved by event order.
- `source_id` and `source_ids` are normalized to stable source IRIs. Inline
  evidence carries the normalized source, locator and optional span through the
  event and RDF mappings.
- Predicate compatibility checks the registered domain/range, object kind and
  literal datatype. `core:alias` explicitly accepts both `xsd:string` and
  language-tagged `rdf:langString`; first-class Identifiers and unresolved
  references remain separate from world entities.
- The event contract now contains 11 write event types. Legacy
  `LocationObserved` is retained only as an upcast input; it is not in
  `EVENT_TYPES`.

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
- Write path **chỉ nhận IRI tuyệt đối**: `predicate_iri` là tên trần bị từ chối
  (`foundry.namespaces.require_absolute_iri`), không resolve ngầm — resolve một
  cái tên trần là tự chế ra một quan hệ mà ontology có thể chưa từng khai báo.
  Chỉ log cũ v2 mới được map, qua bảng tường minh
  `foundry.namespaces.LEGACY_PREDICATE_IRIS` (`locatedAt`, `memberOf`); tên
  legacy ngoài bảng ⇒ `ValueError` (fail loudly, không đoán IRI).
- Object literal mang **type tường minh**: `datatype_iri` XOR `language`, mặc
  định `xsd:string`. Trước đây `{"kind": "literal", "value": "250"}` không còn
  cho biết `"250"` là string hay number; nay datatype/language tag đi tới RDF
  nguyên vẹn (`assertion:literalValue "12000"^^xsd:decimal`, `"Việt Nam"@vi`).
- Upcaster v2→v3 đăng ký trong `UPCASTERS` (`foundry/events.py`): record v2 vẫn
  replay được — ADR-0002/0009; regression test đọc một record v2 `AssertionMade`
  trong `tests/test_events.py`. Log trong repo hiện không có record
  `AssertionMade` v2 nào, nhưng producer đã tồn tại từ Phase 2 nên bump kèm
  upcaster thay vì amend im lặng. Log đã ghi **không bao giờ bị rewrite**: raw v2
  trên đĩa giữ nguyên bytes, upcast chỉ diễn ra trong bộ nhớ khi đọc.
- Đổi theo governance (kèm migration note trong `registry/releases.json`):
  `assertion:literalValue` range `xsd:string` → `rdfs:Literal` (một datatype cố
  định không chở được literal có type); `core:validFrom`/`core:validUntil`
  domain → `core:InformationObject`; `core:hasSource`/`core:hasConfidence`
  domain → `core:Entity`; `core:LocationAssertion` subclass của
  `core:InformationObject`; `core:InformationObject owl:disjointWith
  core:Event`. Domain `core:Event` cũ suy ra sai rằng một mệnh đề là một event.
  Core `0.1.0 → 1.0.0`, assertion `0.1.0 → 1.0.0` (MAJOR).
- Hai tầng validation tách bạch: `build_assertion()` chỉ kiểm *syntax* (IRI
  tuyệt đối, object hợp lệ, timestamp, confidence); `IngestionPipeline` kiểm
  *semantic* — `foundry.assertions.is_known_predicate` đối chiếu registry
  ontology, predicate không khai báo bị từ chối với structured receipt (LLM hay
  extractor không mint được vocabulary); SHACL giữ ràng buộc cấu trúc
  (`assertion:predicate` đúng 1 IRI, `assertion:hasObject` ⊕
  `assertion:literalValue`).
- Lịch sử: tại thời điểm phát hành v3, location lane chưa được migrate. Việc
  đó được hoàn tất bằng migration v3→v4 ở trên.

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
