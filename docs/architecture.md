# Kiến trúc Semantic Foundry

- Status: accepted
- Last synchronized with source: 2026-11-16
- Historical audit baseline: ADR-0001…ADR-0010 and an operational snapshot of 47.628 events / 24.217 entities (2026-09-02)
- Tài liệu mô tả kiến trúc hiện tại và giữ lại các vấn đề trong audit ban đầu cùng cách xử lý. Backlog còn mở nằm trong `roadmap.md`; quyết định kiến trúc được ghi trong ADR.

## 1. Nguyên tắc kiến trúc

Kiến trúc hiện tại tuân theo bốn nguyên tắc:

1. **Event log là nguồn sự thật và chỉ ghi nối tiếp** (ADR-0002): correction là event mới.
2. **Tách đường truy vấn theo nhu cầu**: query vận hành dùng read model có index; query ngữ nghĩa dùng ontology và SPARQL.
3. **Identity ưu tiên precision** (ADR-0003/0006): trùng tên hoặc độ giống từ vựng không đủ làm bằng chứng gộp.
4. **Namespace đã phát hành được giữ ổn định** (ADR-0008): URI/URN đã mint không đổi.

## 2. Kiến trúc hiện tại

```text
                    ┌──────────────────────────────────────────────┐
  Nguồn dữ liệu     │              FOUNDRY (write path)            │
  ─────────────     │                                              │
  Wikidata SPARQL ─►│ fetch/normalize ─► IdentityService ─► SHACL ─┼─► EventLog (JSONL, schema v4)
  (tools/ingest_    │ (foundry/wikidata.py)  (foundry/identity.py) │    data/production.jsonl
   wikidata.py)     │                        pure lookup + mint    │    ~/Data/ontology-research/data/production.jsonl
  Console seed ────►│ seed_console_data.py ──► IngestionPipeline ──┤    = WRITE MODEL (sự thật duy nhất)
  (demo log dưới    │                                              │
   = demo)          └──────────────┬───────────────────────────────┘
                                    │ persist_events()
                                    ▼
                    ┌──────────────────────────────┐   ┌───────────────────────────────┐
   READ PATH        │ Lake (Parquet+zstd, manifest)│   │ Projector worker → PostgreSQL │
                    │ OLAP: DuckDB lake_query      │   │ indexed identity/read model   │
                    └──────────────────────────────┘   └───────────────────────────────┘
                                    │                          │
                                    ▼                          ▼
                    ┌──────────────────────────────┐   ┌───────────────────────────────┐
   REPAIR           │ merge_entities.py            │   │ Console (FastAPI + SPA)       │
                    │ backfill_external_ids.py     │   │ scoped provenance + Admin auth│
                    └──────────────────────────────┘   └───────────────────────────────┘
```

Sơ đồ ghi phiên bản log schema v4; hiện contract là schema v6 với 13 event type
được chấp nhận. Identity dùng in-memory store trong test/offline mode và
PostgreSQL store khi cấu hình `FOUNDRY_DATABASE_URL`. `QueryService` phục vụ
query vận hành; các CQ SPARQL có đường đánh giá riêng qua RDFLib, Jena và
Oxigraph. API không chuyển query vận hành sang RDF store.

Thành phần và invariant đã được triển khai:

| Thành phần | Hợp đồng hiện hành |
|---|---|
| Event log schema v6 | Sequence bền vững, event bất biến, upcaster và idempotency key cho logical batch |
| Identity | Source-record identity tách khỏi canonical identity; tên và alias tạo candidate, còn merge cần same-entity evidence; cannot-link được lưu bền vững |
| Identity persistence | `IdentityStore` có adapter in-memory và PostgreSQL; namespace quyết định cách external ID được index |
| Read model | Projector cập nhật projection, idempotency record và checkpoint trong cùng transaction PostgreSQL |
| SHACL | Dữ liệu qua validation phù hợp trước khi event được commit |
| Namespace | Freeze guards kiểm tra URI/URN đã phát hành |
| Merge và split | Correction là event có thể replay; merge ghi same-entity evidence |
| Core ontology | Core giữ vocabulary dùng chung; term theo domain nằm trong module có version |

## 3. Các vấn đề trong audit ban đầu (đã khắc phục)

Bảng này giữ lại trạng thái tại audit tháng 9/2026 để giải thích thiết kế. D1–D12
đều đã được xử lý bằng các thay đổi ở §4 và được đối chiếu trong §6; đây không
phải backlog hiện tại.

| # | Nợ | Bằng chứng | Mức độ |
|---|-----|-----------|--------|
| D1 | **Nhiều log song song, không có log "chính thức" duy nhất** | Sự cố thật: một lượt chạy ghi vào `data/events.jsonl` thay vì `data/production.jsonl` → 185 entity trùng, phải repair tay bằng 185 `EntityMerged` | **Cao** |
| D2 | **EventLog không có bảo vệ single-writer, không fsync** | `append()` ghi thẳng vào file; 2 tiến trình chạy cùng lúc sẽ tính trùng `_next_sequence` (đều đếm từ disk) và xen kẽ dòng — log hỏng im lặng | **Cao** |
| D3 | **Projector sắp replay theo `(occurred_at, event_id)` thay vì thứ tự log** | `occurred_at` độ phân giải giây; trong cùng một giây, thứ tự = uuid ngẫu nhiên ≠ thứ tự append nhân quả. Test helper `_sequenced_event` trong chính repo đã phải làm việc quanh điều này | **Cao** |
| D4 | **Assertion không phải first-class** | Lịch sử `LocationObserved` trộn 3 vai trò: event "ta biết điều này", assertion "entity ở X từ T", record provenance. Không có assertion id → không correct/retract được một mệnh đề cụ thể; history chỉ append, "latest wins" là ngầm định | **Cao** |
| D5 | **`lake_query` đọc bằng glob, bỏ qua manifest** | `lake_query` build view từ `**/*.parquet` — file chưa flush xong hoặc file mồ côi (writer crash) sẽ bị query; `verify()` có sẵn nhưng không nằm trên query path | Trung bình |
| D6 | **Reverse index `_by_location` chỉ đúng sau batch replay** | `rebuild_location_index()` chỉ được gọi cuối `replay()`; apply streaming một event mới để index cũ kỹ — invariant ẩn chỉ ai đọc code mới biết | Trung bình |
| D7 | **Review queue không bền vững** | Record bị reject chỉ trả receipt trong RAM của lần chạy đó; không bảng review, không replay được; ambiguous collision chỉ sống trong stdout | Trung bình |
| D8 | **Provenance/time/confidence ad hoc theo từng payload** | Lịch sử `source_ids` ở `LocationObserved`, `source_id` + `confidence` ở `EntityCreated`, không có cấu trúc chung; thêm fact type mới = tự chế lại bộ ba này | Trung bình |
| D9 | **Projector replay toàn phần mỗi lần khởi động** | 47k events → seconds mỗi lần; không checkpoint dù sequence đã có sẵn (ADR-0009 thiết kế đúng nhưng chưa dùng) | Thấp–TB |
| D10 | **Lake không có compaction/dedup** | Mỗi lần persist = 1 file mới (đã 4 files cho 3 lượt ghi); persist lại cùng event → row trùng, `event_id` không unique-enforced | Thấp–TB |
| D11 | **Reference data trộn với event facts** | Wikidata fetch là runtime step của ingestion; không có lane reference tĩnh, không có paging (fetch đạt trần LIMIT 3000) | Trung bình |
| D12 | **SHACL yêu cầu identity đã resolve** | Shape `Observation` bắt buộc tham chiếu entity tồn tại — không biểu diễn được unresolved reference từ nguồn ngoài | Trung bình |

## 4. Các invariant đã triển khai

Các tiểu mục sau ghi lại cách repo hiện thực các quyết định trên. Chúng là mô tả
code hiện tại, không phải kế hoạch chuyển đổi còn mở.

### 4.1 Một log production duy nhất + transport bền vững (sửa D1, D2)

**Invariant**: một production log mặc định (`~/Data/ontology-research/data/production.jsonl`,
override bằng `FOUNDRY_ADMIN_LOG`). Seed/demo dùng log riêng và **không bao giờ** là
default của tool production. Thư mục `data/` trong worktree là symlink tới external
data root. Writer an toàn:

```text
EventLog (event schema v6; upcaster reads log v1–v5)
├── writer lock file (flock) — writer thứ hai fail nhanh, không xen kẽ
├── fsync mỗi batch (mỗi lần extend) — crash không mất batch đã báo thành công
├── segment files: production-000001.jsonl, … (roll khi đạt ngưỡng hoặc trước
│   record làm vượt ngưỡng; record đơn lớn hơn ngưỡng được giữ nguyên) — repair một
│   segment hỏng không đụng phần còn lại
├── tail-recovery: dòng JSON cắt cụt ở cuối segment = bỏ + ghi nhận, không làm
│   chết toàn bộ log
└── sequence = offset toàn cục qua các segment (không còn là vị trí dòng)
```

Quy tắc vận hành: CLI production (`ingest_wikidata`, `merge_entities`,
`backfill_external_ids`) mặc định vào production log; log demo chỉ do
`seed_console_data` chạm tới. Import giữa hai log là tool tường minh
(`import-events`) re-stamp sequence theo log nhận — đã được chứng minh cần thiết
bởi sự cố D1.

`sequence` là số thứ tự event tăng dần trên toàn bộ segment, không phải byte
offset. Các số đo hiệu năng trong tài liệu này được ghi riêng với ngày và
workload; chúng không tự cập nhật khi build docs.

### 4.2 Document → Assertion model (sửa D4, D8)

Tách "sự kiện ta biết" khỏi "mệnh đề về thế giới". Đây là thay đổi mô hình lớn
nhất, làm nền cho mọi fact type sau này:

```text
Document                          Assertion
  document_id (urn:doc:)            assertion_id (urn:assert:)
  uri / title / content_ref         subject_id   (canonical entity)
  source_system / source_record_key predicate_iri (IRI tuyệt đối: core:locatedAt)
  content_hash (SHA-256)            object       (location_uri / entity_id / literal)
                                    ── generic provenance, mọi assertion như nhau:
                                    valid_from / valid_until (valid-time, [from, until))
                                    recorded_at              (event record-time)
                                    source_ids[]            (Source refs)
                                    evidence[]              (source locator/span)
                                    model_score / evidence_quality
                                    status: asserted | superseded | retracted (read model)
                                    supersedes: assertion_id | null
```

Quy tắc hiện hành:

- **Event chỉ còn vai trò ghi thay đổi của assertion**: `AssertionMade` (tạo),
  `AssertionSuperseded` (retract assertion). Correction tạo assertion thay thế
  với `supersedes` trỏ assertion trước đó. `LocationObserved` cũ được upcast
  thành `AssertionMade(predicate_iri=core:locatedAt)` khi đọc; producer mới chỉ
  dùng assertion lane (event schema v6, `CHANGELOG-DATA.md`).
- **Assertion là information object, không phải event**: `assertion:Assertion ⊑
  core:InformationObject`; quan hệ là một node trong graph
  (`assertion:predicate` → IRI của property, vd `core:locatedAt`), nên hai mệnh
  đề chỉ khác quan hệ không còn trùng RDF form. Object dạng literal nằm ở
  `assertion:literalValue` (không dùng `core:name` cho giá trị).
- Read model giữ history theo **assertion_id** — correction/retraction cập nhật
  active status và location projection ngay khi apply. Validity là khoảng nửa
  mở `[valid_from, valid_until)`; nếu nhiều location assertion còn active cùng
  lúc thì trả trạng thái conflict, không chọn theo thứ tự event.
- `assertions_as_of(subject, instant, predicate)` trả mọi assertion đã resolve
  và còn hiệu lực tại thời điểm đó cho bất kỳ predicate nào; cách truy vấn này
  cũng hỗ trợ quan hệ thay đổi theo thời gian như `core:memberOf`.
- `predicate_compatibility` kiểm tra property đã khai báo, được phép assert,
  domain/range và kiểu object/literal. `core:currentLocation` cùng các sensor
  measurement predicates là projection/output-only, không được ingest như world
  assertions.
- Source IDs được chuẩn hóa thành `core:Source`; evidence giữ source-local
  locator/span và exact text khi có. `model_score` mô tả điểm của extractor;
  `evidence_quality` đánh giá chất lượng nguồn độc lập. Không trường nào trong
  hai trường này biểu thị xác suất fact đúng. Unresolved subjects dùng
  `identity:UnresolvedReference`, không được gán kiểu `core:Entity`.
- `recorded_at` luôn tách khỏi valid-time. Cùng source record và cùng SHA-256
  content hash tạo lại cùng document event; nội dung khác tạo document version
  mới nhưng giữ nguyên source-record key. Entity/source-record events cũng lưu
  stable key và hash của payload nguồn đã chuẩn hóa.
- Replacement assertion phải trỏ tới assertion chưa được thay thế; một khóa
  trạng thái duy nhất ngăn retry ghi lặp hoặc hai correction cùng thay một
  claim. Replay giữ predecessor với trạng thái `superseded`; retraction không
  có replacement có trạng thái `retracted`. Các giá trị valid-time chồng lấn
  mà không có correction vẫn hiện thành conflict.
- Entity creation giữ nguyên `EntityCreated` (đã đúng vai trò event).

Location facts và các fact khác dùng cùng assertion lane. Upcaster v3→v4 chuyển
legacy location events khi đọc; byte trong log không bị ghi lại.

### 4.3 Projector: sequence-ordered, checkpointed, per-event idempotent (sửa D3, D6, D9)

- **Thứ tự replay = thứ tự log**: sort theo `sequence` (fallback vị trí dòng cho
  record v1), bỏ `(occurred_at, event_id)`. Nhân quả không còn phụ thuộc độ
  phân giải giây của timestamp.
- **Per-event idempotency**: read model giữ watermark các event đã apply
  (checkpoint); apply lại event cũ = no-op thật, không chỉ "replay toàn phần cho
  cùng kết quả".
- **Checkpoint**: PostgreSQL lưu `last_sequence` trong cùng transaction với
  projection writes và event idempotency row; khởi động lại chỉ replay phần tăng thêm.
- **Streaming log replay**: `EventLog.iter_all()` kiểm tra từng dòng mà không
  nạp cả file vào RAM; `replay_log()` dùng `Projector.replay_in_order()`. API
  `read_all()` và `Projector.replay()` vẫn giữ cho caller cần list/sort.
- **Streaming-safe**: `rebuild_location_index()` biến thành cập nhật tăng dần
  bên trong từng apply — bỏ invariant "chỉ đúng sau batch replay".
- **Incremental event source**: `EventLog.iter_after_sequence()` dùng segment
  metadata để bỏ qua segment hoàn tất trước checkpoint; chỉ đọc phần log mới.
- `ReadModel` in-memory vẫn có cho test và demo; operational mode dùng
  `PostgresReadModelStore` để API không nạp toàn graph vào RAM.

### 4.4 Lake: manifest-authoritative + compaction + dedup (sửa D5, D10)

- `lake_query` build view **từ manifest entries** (danh sách file đóng), không
  glob filesystem. File chưa flush không tồn tại với query.
- `event_id` là unique key: persist lại event cũ → skip (idempotent), không sinh
  row trùng.
- **Compaction**: tool gộp các file nhỏ theo partition, ghi file mới + cập nhật
  manifest atomically (write manifest.tmp → rename), xoá file cũ sau khi manifest
  mới đã rename.
- Writer lấy cùng flock discipline với log — một writer tại một thời điểm.

### 4.5 Identity: persistent PostgreSQL indexes + durable review queue + un-merge (sửa D7)

- Registry tiếp tục là projection của log (ADR-0010) nhưng có **store boundary**:
  interface `IdentityStore` có backend in-memory cho test và
  `PostgresIdentityStore` cho operational mode. Alias/token, source-record,
  cannot-link, merge redirect và external-id indexes đều nằm trong PostgreSQL;
  namespace không unique giữ multimap semantics.
- Projector worker cập nhật identity, assertion/relation indexes và checkpoint
  trong cùng transaction. `tools/project_events.py --rebuild` xóa projection
  PostgreSQL rồi replay theo luồng; nó không sửa event log.
- **Review queue thành first-class**: mọi rejection/ambiguous (kèm payload,
  candidates, reason) append vào log bằng event `ResolutionReviewQueued` —
  review được replay, thống kê được, không mất khi process chết. Merge CLI đọc
  queue thay vì phải phát hiện tay.
- **Un-merge**: `EntitySplit` event (ngược `EntityMerged`) — merge sai có đường
  quay lại; dựng trên redirect map hiện có.

### 4.6 Reference lane tách khỏi event facts (sửa D11)

Wikidata/Wikipedia là **reference data** (nhiều version, đổi thường) — không
phải fact vận hành:

```text
reference/
  wikidata/{class_qid}/part-*.parquet   # QID, labels vi/en, aliases, types,
  wikipedia/…                           # timestamps — snapshot tĩnh, versioned
```

- Fetch có **paging theo cursor** (QID tiếp theo thay vì LIMIT window).
- Ingestion đọc reference lane như một nguồn; facts sinh ra vẫn đi qua identity +
  SHACL vào log như thường. Reference lane rebuild được độc lập, không bao giờ
  là nguồn sự thật.

### 4.7 SHACL hỗ trợ unresolved identity (sửa D12)

- Tham chiếu chưa resolve là `identity:UnresolvedReference`, disjoint với
  `core:Entity`. Assertion giữ `subjectReference` cùng surface form; nó chỉ được
  nối vào entity khi đúng một identity phù hợp. Nếu alias trở nên ambiguous,
  read model unlinks projection đó và chuyển reference trở lại pending.
- Unknown object entities are queued for review; they are never fabricated as
  canonical placeholders.

### 4.8 Benchmark và giới hạn số đo

`tools/benchmark.py` đo event synthetic, segmented-log append, exact alias
lookup, streaming replay, in-memory query latency, peak RSS và kích thước log.
`tools/benchmark_e2e.py` đo ingest, lake persist, projector replay và lake query;
`make check` chạy stage gate ở CI scale. Hai benchmark này dùng đường chạy
in-memory và chưa đo PostgreSQL. `make query-adapter-eval` kiểm tra kết quả của
PostgreSQL và Oxigraph trên cùng workload; phép đo query của hai adapter gồm
thành phần xử lý khác nhau nên không phải so sánh engine-only. Chi tiết và phạm
vi nằm trong `requirements/scale_targets.md`. Chưa có bằng chứng đạt target
10M/100M entity.

### 4.9 Schema evolution preflight

Ontology Turtle và SHACL shapes tiếp tục là schema nguồn. Governance đọc các
module thành term snapshots, diff theo SemVer và thêm nhãn tương thích
`SAFE` / `COMPATIBLE` / `BREAKING`: additions là `SAFE`, edit không breaking
là `COMPATIBLE`, còn mọi thay đổi MAJOR là `BREAKING`. Core giữ các khái niệm
cross-domain ổn định; khái niệm domain đi trong các module versioned hiện có.
Thay đổi schema không tự thêm vocabulary hay domain pack.

Trước khi tạo migration MAJOR, chạy `tools/manage_ontology.py migrate MODULE`
với một `--data PATH` cho từng nguồn dữ liệu authoritative. Lệnh xuất migration
review được cùng `.impact.json`, gồm các data item nhắc tới term đổi trong RDF,
JSON/JSONL hoặc Parquet và consumer trong ontology, SHACL, event contract, API,
read model/index, query và migration cũ. Đây là impact leads để review; lexical
match không thay thế SHACL hoặc validation theo application. Thư mục lake có
`manifest.json` chỉ scan các Parquet file đã đăng ký; file mồ côi không tham
gia. Report ghi rõ các nguồn đã scan. SHA-256 trong report gắn kết quả preflight
với nội dung migration script; nếu script đổi thì `release --migration-script`
từ chối report cũ.

Workflow này áp dụng ý tưởng parser → validator → schema diff/codegen trong
[ODL parser](https://github.com/syzygyhack/open-foundry/blob/main/packages/odl/src/parser/index.ts),
[validator](https://github.com/syzygyhack/open-foundry/blob/main/packages/odl/src/validator/index.ts),
[diff](https://github.com/syzygyhack/open-foundry/blob/main/packages/odl/src/diff/index.ts)
và [codegen](https://github.com/syzygyhack/open-foundry/blob/main/packages/odl/src/codegen/index.ts),
giữ nguyên Turtle/SHACL và API contracts của repo.

## 5. Thứ tự triển khai nền tảng

Các thay đổi từ audit ban đầu được triển khai theo thứ tự sau:

```text
B1. 4.1 Log duy nhất + transport bền vững       ← nền cho mọi thứ; dọn D1/D2
B2. 4.3 Projector sequence-ordered + checkpoint ← cần sequence ổn định từ B1
B3. 4.4 Lake manifest-authoritative + dedup     ← độc lập B2, song song được
B4. 4.5 Identity store + review queue + un-merge
B5. 4.2 Document/Assertion model                ← lớn nhất, làm khi B1–B4 ổn định
B6. 4.6 Reference lane + paging; 4.7 SHACL unresolved; 4.8 e2e benchmark
```

Mỗi bước giữ nguyên bất biến: log không rewrite; mọi migration là upcaster mới
hoặc event mới. Sau các bước này, repo bổ sung PostgreSQL identity/read model,
projector checkpoint trong transaction và `QueryService`; xem trạng thái mới ở
`roadmap.md`.

## 6. Trạng thái triển khai (đối chiếu 2026-11-16)

Toàn bộ thiết kế trong tài liệu này **đã triển khai**:

| Thiết kế | Commit |
|---|---|
| §4.1 Log transport (segments, flock, fsync, tail-recovery, import tool) | `23330f4` |
| §4.3 Projector sequence-ordered, checkpoint, per-event idempotent | `072fcf5` |
| §4.4 Lake manifest-authoritative, dedup, compaction | `b3ee596` |
| §4.5 Review queue bền vững (`ResolutionReviewQueued`) + un-merge (`EntitySplit` + CLI) | `d23686b` |
| §4.2 Document/Assertion ledger (`AssertionMade`/`AssertionSuperseded`/`DocumentRegistered`) | `3017719`, `14f80e0`, `d1d0bce` |
| §4.5 IdentityStore boundary | `fd7d218` |
| §4.6 Reference lane + cursor paging | `ba3cd8e` |
| §4.7 SHACL unresolved identity (`identity:UnresolvedReference`) | `a972f8b` |
| §4.8 E2E per-stage benchmark + gate | `b2cac79` |
| Phase 5 Console admin write ops (token auth + audit trail) | `54eab65` |
| Phase 6 provenance access (`FOUNDRY_PROVENANCE_TOKENS`) | `foundry/console/api/provenance_access.py`, `tests/test_console_api.py` |
| Location facts use generic assertions; corrections/retractions refresh the location projection | `foundry/`, `tests/test_events.py`, `tests/test_readmodel.py` |
| `ObservationResult`, typed measurements, source normalization, `Identifier` and compatibility rules | `foundry/`, `tests/test_tracking.py`, `tests/test_semantic_consistency.py` |
| Event and assertion provenance, identity evidence and idempotent commands | `cd4c938` |
| PostgreSQL identity/read model and transactional projection | `5e92123` |
| Bounded operational `QueryService` and semantic adapters | `4875f89` |
| RDF export bounded-memory probe | `b6ba61f` |
| Oxigraph engine and Fuseki service latency measurements separated | `c6440d3` |
| CQ workload: 41 executable queries | `requirements/competency_questions.md`, `benchmarks/queries/`, `benchmarks/expected_results/` |

D1–D12 và các sửa đổi assertion, observation, identity, query và workload đã
được triển khai. Query API giới hạn provenance theo source-ID scope; request
không có token nhận danh sách source rỗng. PostgreSQL backend đã có; scale ladder
và đo recovery ở quy mô lớn vẫn còn trong backlog tại `roadmap.md`.

## 7. Rủi ro đã ghi nhận trong quá trình triển khai

Các mục dưới đây là rủi ro rollout được ghi trong audit thiết kế; chúng không
mô tả backlog còn mở.

- **4.2 (Assertion) is a breaking conceptual change, migrated in v4**:
  `LocationObserved` chỉ được đọc qua upcaster v3→v4; record mới dùng
  `AssertionMade`. Byte của log lịch sử không đổi.
- **4.3 đổi thứ tự replay** có thể đổi `first_seen`/`last_event_time` trên dữ
  liệu cùng giây — chấp nhận vì đó mới là thứ tự nhân quả đúng; ghi rõ trong
  release note.
- **B1 single-writer** giới hạn throughput. Multi-writer chưa được triển khai;
  trước hết cần benchmark workload lớn và xác định mức throughput cần thiết.
