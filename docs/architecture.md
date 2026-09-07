# Kiến trúc Semantic Foundry

- Status: accepted
- Date: 2026-09-02
- Tổng hợp từ: ADR-0001…ADR-0010, review kiến trúc trên dữ liệu thật (47.628 events, 24.217 entities)
- Tài liệu này là **bản đồ kiến trúc hiện tại + thiết kế lại các phần chưa tốt**. Các quyết định riêng lẻ vẫn nằm trong ADR; tài liệu này tổng hợp và chỉ ra hướng đi.

## 1. Nguyên tắc bất diệt

Mọi thiết kế dưới đây phục tùng bốn nguyên tắc này — chúng không phải bàn cãi:

1. **Log là sự thật, append-only** (ADR-0002): corrections là event mới, không bao giờ rewrite.
2. **Never make the operational query path pay for semantic complexity it does not need** — read model là materialized, đơn giản, nhanh.
3. **Precision-first identity** (ADR-0003/0006): false-merge đắt hơn under-merge; lexical similarity không bao giờ tự merge.
4. **Namespace frozen** (ADR-0008): URI/URN đã mint là vĩnh viễn.

## 2. Kiến trúc hiện tại

```text
                    ┌──────────────────────────────────────────────┐
  Nguồn dữ liệu     │              FOUNDRY (write path)            │
  ─────────────     │                                              │
  Wikidata SPARQL ─►│ fetch/normalize ─► IdentityService ─► SHACL ─┼─► EventLog (JSONL, schema v2)
  (tools/ingest_    │ (foundry/wikidata.py)  (foundry/identity.py) │    data/production.jsonl
   wikidata.py)     │                        pure lookup + mint    │    = WRITE MODEL (sự thật duy nhất)
  Console seed ────►│ seed_console_data.py ──► IngestionPipeline ──┤
  (data/events.jsonl│                                              │
   = demo)          └──────────────┬───────────────────────────────┘
                                    │ persist_events()
                                    ▼
                    ┌──────────────────────────────┐   ┌───────────────────────────────┐
   READ PATH        │ Lake (Parquet+zstd, manifest)│   │ Projector → ReadModel (CQRS)  │
                    │ OLAP: DuckDB lake_query      │   │ foundry/projector.py (batch)  │
                    └──────────────────────────────┘   └───────────────────────────────┘
                                    │                          │
                                    ▼                          ▼
                    ┌──────────────────────────────┐   ┌───────────────────────────────┐
   REPAIR           │ merge_entities.py            │   │ Console (FastAPI + SPA)       │
                    │ backfill_external_ids.py     │   │ Dashboard/Monitor/Projection  │
                    └──────────────────────────────┘   └───────────────────────────────┘
```

Thành phần đang hoạt động tốt (giữ nguyên thiết kế):

| Thành phần | Vì sao tốt |
|---|---|
| Event contract v2 (sequence, valid_at, upcasters) | Replay phát hiện tamper; log cũ v1 vẫn đọc được; đã chịu 47k events thật |
| Identity precision-first + blocking index | unresolved_rate = 0% trên nguồn có QID; fuzzy không bao giờ tự merge |
| Registry = log projection | Restart không mất định danh; re-ingest là external-id hit (đã verify 298/298) |
| SHACL gate trước log | Không có fact vi phạm contract trong write model |
| Namespace freeze guards | Không ai đổi URI vô tình — CI chặn |
| Merge là event không phải edit | 185 cặp trùng repair được, có audit trail, replay được |

## 3. Nợ kiến trúc — có bằng chứng, có severity

Đánh giá sau khi vận hành trên dữ liệu thật. Mỗi mục kèm bằng chứng cụ thể,
không phải dự đoán.

| # | Nợ | Bằng chứng | Mức độ |
|---|-----|-----------|--------|
| D1 | **Nhiều log song song, không có log "chính thức" duy nhất** | Sự cố thật: một lượt chạy ghi vào `data/events.jsonl` thay vì `data/production.jsonl` → 185 entity trùng, phải repair tay bằng 185 `EntityMerged` | **Cao** |
| D2 | **EventLog không có bảo vệ single-writer, không fsync** | `append()` ghi thẳng vào file; 2 tiến trình chạy cùng lúc sẽ tính trùng `_next_sequence` (đều đếm từ disk) và xen kẽ dòng — log hỏng im lặng | **Cao** |
| D3 | **Projector sắp replay theo `(occurred_at, event_id)` thay vì thứ tự log** | `occurred_at` độ phân giải giây; trong cùng một giây, thứ tự = uuid ngẫu nhiên ≠ thứ tự append nhân quả. Test helper `_sequenced_event` trong chính repo đã phải làm việc quanh điều này | **Cao** |
| D4 | **Assertion không phải first-class** | `LocationObserved` trộn 3 vai trò: event "ta biết điều này", assertion "entity ở X từ T", record provenance. Không có assertion id → không correct/retract được một mệnh đề cụ thể; history chỉ append, "latest wins" là ngầm định | **Cao** |
| D5 | **`lake_query` đọc bằng glob, bỏ qua manifest** | `lake_query` build view từ `**/*.parquet` — file chưa flush xong hoặc file mồ côi (writer crash) sẽ bị query; `verify()` có sẵn nhưng không nằm trên query path | Trung bình |
| D6 | **Reverse index `_by_location` chỉ đúng sau batch replay** | `rebuild_location_index()` chỉ được gọi cuối `replay()`; apply streaming một event mới để index cũ kỹ — invariant ẩn chỉ ai đọc code mới biết | Trung bình |
| D7 | **Review queue không bền vững** | Record bị reject chỉ trả receipt trong RAM của lần chạy đó; không bảng review, không replay được; ambiguous collision chỉ sống trong stdout | Trung bình |
| D8 | **Provenance/time/confidence ad hoc theo từng payload** | `source_ids` ở `LocationObserved`, `source_id` + `confidence` ở `EntityCreated`, không có cấu trúc chung; thêm fact type mới = tự chế lại bộ ba này | Trung bình |
| D9 | **Projector replay toàn phần mỗi lần khởi động** | 47k events → seconds mỗi lần; không checkpoint dù sequence đã có sẵn (ADR-0009 thiết kế đúng nhưng chưa dùng) | Thấp–TB |
| D10 | **Lake không có compaction/dedup** | Mỗi lần persist = 1 file mới (đã 4 files cho 3 lượt ghi); persist lại cùng event → row trùng, `event_id` không unique-enforced | Thấp–TB |
| D11 | **Reference data trộn với event facts** | Wikidata fetch là runtime step của ingestion; không có lane reference tĩnh, không có paging (fetch đạt trần LIMIT 3000) | Trung bình |
| D12 | **SHACL yêu cầu identity đã resolve** | Shape `Observation` bắt buộc tham chiếu entity tồn tại — không biểu diễn được unresolved reference từ nguồn ngoài | Trung bình |

## 4. Thiết kế lại

### 4.1 Một log production duy nhất + transport bền vững (sửa D1, D2)

**Target**: đúng một production log (`data/production.jsonl`). Seed/demo dùng
log riêng và **không bao giờ** là default của tool production. Writer an toàn:

```text
EventLog (v3 transport, payload contract giữ nguyên v2)
├── writer lock file (flock) — writer thứ hai fail nhanh, không xen kẽ
├── fsync mỗi batch (mỗi lần extend) — crash không mất batch đã báo thành công
├── segment files: production-000001.jsonl, … (roll theo size) — repair một
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

### 4.2 Document → Assertion model (sửa D4, D8)

Tách "sự kiện ta biết" khỏi "mệnh đề về thế giới". Đây là thay đổi mô hình lớn
nhất, làm nền cho mọi fact type sau này:

```text
Document                          Assertion
  document_id (urn:doc:)            assertion_id (urn:assert:)
  uri / title / fetched_at          subject_id   (canonical entity)
  source_system                     predicate    (locatedAt / memberOf / …)
  content_ref                       object       (location_uri / entity_id / literal)
                                    ── generic provenance, mọi assertion như nhau:
                                    valid_from / valid_to   (valid-time)
                                    asserted_at             (transaction-time)
                                    source_ids[]            (Document refs)
                                    confidence
                                    status: asserted | corrected | retracted
                                    supersedes: assertion_id | null
```

Quy tắc:

- **Event chỉ còn vai trò ghi thay đổi của assertion**: `AssertionMade` (tạo),
  `AssertionSuperseded` (correct/retract trỏ assertion cũ). `LocationObserved`
  hiện tại map 1:1 sang `AssertionMade(predicate=locatedAt)` khi migrate.
- Read model giữ history theo **assertion_id** — correct/retract một mệnh đề cụ
  thể được, không còn "latest wins" ngầm định.
- Entity creation giữ nguyên `EntityCreated` (đã đúng vai trò event).

Đây là workstream lớn nhất; làm sau khi 4.1 ổn vì nó thêm event type lên cùng log.

### 4.3 Projector: sequence-ordered, checkpointed, per-event idempotent (sửa D3, D6, D9)

- **Thứ tự replay = thứ tự log**: sort theo `sequence` (fallback vị trí dòng cho
  record v1), bỏ `(occurred_at, event_id)`. Nhân quả không còn phụ thuộc độ
  phân giải giây của timestamp.
- **Per-event idempotency**: read model giữ watermark các event đã apply
  (checkpoint); apply lại event cũ = no-op thật, không chỉ "replay toàn phần cho
  cùng kết quả".
- **Checkpoint**: lưu `last_sequence` cạnh read model; khởi động lại chỉ replay
  phần tăng thêm.
- **Streaming-safe**: `rebuild_location_index()` biến thành cập nhật tăng dần
  bên trong từng apply — bỏ invariant "chỉ đúng sau batch replay".
- Read model vẫn in-memory theo ADR-0004; interface không đổi với Console.

### 4.4 Lake: manifest-authoritative + compaction + dedup (sửa D5, D10)

- `lake_query` build view **từ manifest entries** (danh sách file đóng), không
  glob filesystem. File chưa flush không tồn tại với query.
- `event_id` là unique key: persist lại event cũ → skip (idempotent), không sinh
  row trùng.
- **Compaction**: tool gộp các file nhỏ theo partition, ghi file mới + cập nhật
  manifest atomically (write manifest.tmp → rename), xoá file cũ sau khi manifest
  mới đã rename.
- Writer lấy cùng flock discipline với log — một writer tại một thời điểm.

### 4.5 Identity: persistent boundary + durable review queue + un-merge (sửa D7)

- Registry tiếp tục là projection của log (ADR-0010) nhưng có **store boundary**:
  interface `IdentityStore` với backend mặc định in-memory-rebuilt-from-log;
  backend SQLite/graph-db thay vào sau không đổi `IdentityService`.
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

- Thêm node kind cho tham chiếu chưa resolve: observation về một entity chưa
  mint vẫn qua gate, được ghi vào log, và tự nối vào entity khi identity resolve
  sau (xử lý ở projector, không cần sửa log).

### 4.8 Benchmark end-to-end (đo cái thật)

Microbenchmark hiện tại chỉ đo read model trong bộ nhớ. Bổ sung e2e suite:
reference → ingest (identity + SHACL + log) → persist lake → replay projector →
đo throughput/latency từng stage trên phân bố dữ liệu thật (song ngữ, tên trùng,
alias nhiều). SLO gate mở rộng theo stage thay vì một con số tổng.

## 5. Thứ tự chuyển đổi

Phụ thuộc quyết định thứ tự — không làm song song các bước liền kề:

```text
B1. 4.1 Log duy nhất + transport bền vững       ← nền cho mọi thứ; dọn D1/D2
B2. 4.3 Projector sequence-ordered + checkpoint ← cần sequence ổn định từ B1
B3. 4.4 Lake manifest-authoritative + dedup     ← độc lập B2, song song được
B4. 4.5 Identity store + review queue + un-merge
B5. 4.2 Document/Assertion model                ← lớn nhất, làm khi B1–B4 ổn định
B6. 4.6 Reference lane + paging; 4.7 SHACL unresolved; 4.8 e2e benchmark
```

Mỗi bước giữ nguyên bất biến: log không rewrite; mọi migration là upcaster mới
hoặc event mới. Tiến trình track ở `roadmap.md`, mục "Nền móng production facts".

## 6. Rủi ro của chính bản thiết kế lại này

- **4.2 (Assertion) là breaking change ở tầng khái niệm**: mọi consumer đọc
  `LocationObserved` phải map sang `AssertionMade`. Giảm rủi ro bằng upcaster
  v2→v3 để log cũ đọc được mãi, và dual-write một thời gian nếu Console cần.
- **4.3 đổi thứ tự replay** có thể đổi `first_seen`/`last_event_time` trên dữ
  liệu cùng giây — chấp nhận vì đó mới là thứ tự nhân quả đúng; ghi rõ trong
  release note.
- **B1 khoá single-writer** là hạn chế throughput có chủ đích: 10^5 events/s của
  Phase 6 sẽ cần multi-writer — thiết kế segment ở 4.1 để sẵn đường này, nhưng
  không làm trước khi có nhu cầu đo được.