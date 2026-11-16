# Lộ trình Semantic Foundry

> Truy vấn vận hành dùng read model có index. Truy vấn ngữ nghĩa dùng ontology
> và SPARQL.

Tài liệu này ghi trạng thái đã triển khai và thứ tự công việc còn lại. Kiến trúc
hiện tại được mô tả trong `docs/architecture.md`.

---

## 1. Snapshot ngày 2026-11-16

Snapshot code dựa trên commit `af6a7fb`. `make check` chạy local với PostgreSQL
test database đã cấu hình: 564 passed, 10 skipped. GitHub Actions cũng đã hoàn
thành bốn check `check`, `ontology-conformance`, `build` và `deploy` cho commit
này.

| Thành phần | Trạng thái | Bằng chứng trong repo |
|---|---|---|
| Event log | ✅ | `foundry/events.py` — JSONL phân đoạn, lock, fsync, tail recovery, sequence metadata bền vững, idempotency theo batch và upcaster; schema v6 nhận 13 event type |
| Identity registry | ✅ | `foundry/identity.py`, `foundry/postgres.py` — tách source-record identity khỏi canonical identity; hỗ trợ evidence tường minh, cannot-link và store in-memory/PostgreSQL |
| Ingestion | ✅ | `foundry/ingestion.py` — kiểm tra SHACL cho reference, tracking, document và correction lanes |
| Read model | ✅ | `foundry/projector.py`, `foundry/postgres_readmodel.py` — replay streaming, checkpoint giao dịch, idempotency theo event và projection PostgreSQL có index |
| Lake | ✅ | `foundry/lake.py` — Parquet+zstd, truy vấn theo manifest, khử trùng lặp theo event ID và compaction |
| Assertion | ✅ | `foundry/assertions.py` — valid-time nửa mở, recorded time, lineage source record, evidence span, supersession và retraction |
| Extraction | ✅ | `foundry/extraction.py` — `PatternExtractor` xác định và `LlmExtractor` có thể thay thế; candidate không tự mint entity |
| Reference lane | ✅ | `foundry/reference.py` và `wd.iter_entities` — phân trang theo QID, snapshot có version |
| Tracking | ✅ | `foundry/tracking.py` — sensor, observation event, `ObservationResult` riêng cho vị trí và measurement có kiểu, track; domain SHACL validation |
| Query service | ✅ | `foundry/query_service.py` — truy vấn PostgreSQL cho lookup, thời gian, quan hệ và provenance; có phân trang, timeout và traversal giới hạn |
| Adapter truy vấn ngữ nghĩa | ✅, đường đánh giá | 41 CQ dùng cùng expected results trên RDFLib, Oxigraph và Jena |
| Console | ✅ | `foundry/console/` — FastAPI + SPA; provenance read có scope, write cần token và được audit trong event log |
| Governance | ✅ | `tools/manage_ontology.py` — SemVer registry, semantic diff, blast radius và preflight ảnh hưởng tới data/API/index |
| Competency queries | ✅ | 41 case bắt buộc với dataset và expected rows cố định, bao gồm temporal, provenance, contradiction, correction, identity và tracking |
| Benchmark | ✅ | Có baseline micro/E2E in-memory; `make query-adapter-eval` so sánh kết quả của PostgreSQL và Oxigraph trên cùng workload. Thời gian của hai adapter không phải phép đo engine tương đương |
| Ontology | ✅ | 7 module, 31 lớp và 78 thuộc tính; xem `docs/guides/ontology-status.md` |

Core giữ vocabulary dùng chung giữa các domain. Term đặc thù nằm trong module có
version. Migration MAJOR cần preflight với nguồn dữ liệu authoritative và các
consumer downstream.

**Snapshot production lịch sử (2026-09-02)**: 24.217 canonical entity từ
Wikidata (nhãn Việt–Anh) và 47.628 event trong production log. Log không nằm
trong repo; cần đo lại trước khi dùng các số này làm trạng thái hiện tại.

**Accepted event types (13)**: `EntityCreated`, `EntityMerged`, `EntitySplit`,
`ExternalIdBound`, `SourceRecordMatched`, `EntityCannotLinkAsserted`,
`ResolutionReviewQueued`, `DocumentRegistered`, `AssertionMade`,
`AssertionSuperseded`, `SensorRegistered`, `ObservationRecorded` và
`TrackObserved`.

`LocationObserved` chỉ còn là dạng event lịch sử được upcast v3→v4 thành
`AssertionMade(predicate_iri=core:locatedAt)`; producer mới không phát type này.
`ObservationRecorded` tạo observation event và `tracking:ObservationResult`
riêng biệt để lưu vị trí đo được cùng measurement có kiểu dữ liệu.

`AffiliationAssessed` đã bị loại khỏi contract; affiliation mới dùng
`AssertionMade` với predicate phù hợp (xem `CHANGELOG-DATA.md`).

Mười ADR nằm trong `docs/adr/`. Bảng triển khai theo commit ở
`docs/architecture.md` §6.

---

## 2. Phase đã hoàn thành

| Phase | Phạm vi đã ship |
|-------|-----------------|
| **0** — Engineering contract | requirements, scale targets, SLO baseline, CQ harness |
| **1** — Semantic kernel | `core.ttl` (19 lớp / 34 thuộc tính), SHACL và CI; CQ contract có 41 case chạy được |
| **2** — Production ingestion | Event schema v6, identity precision-first, predicate compatibility, SHACL gate, bốn data lane và document extraction; LLM chỉ tạo đề xuất |
| **3** — Read model (CQRS) | Projector có checkpoint và idempotency theo event, Console, benchmark latency và SLO gate |
| **4** — Domain vertical | Location, organization, sensor và tracking modules; domain SHACL và ingestion mapping |
| **5** | SemVer registry, blast radius, stability, migration/alignment; merge/split/review/backfill/import; Document/Assertion; reference lane; streaming projector; identity store boundary; Console write có token và audit |
| **6, nền tảng** | Provenance access, PostgreSQL identity/read model, bounded `QueryService` và hợp đồng đánh giá query adapter |

Kiến trúc nền B1–B8 theo `docs/architecture.md` §4: log transport bền vững
(§4.1), projector sequence-ordered (§4.3), lake manifest-authoritative (§4.4),
review queue bền vững và un-merge (§4.5), IdentityStore boundary, reference
lane (§4.6), unresolved identity SHACL (§4.7) và benchmark E2E (§4.8) đều đã
triển khai. Bảng commit mapping ở `docs/architecture.md` §6.

---

## 3. Backlog có thứ tự

### 3.1 Nền tảng query vận hành đã hoàn thành

Identity và read model nay có adapter PostgreSQL; projector lưu checkpoint cùng
projection writes trong transaction. `QueryService` cung cấp operational query
với pagination, timeout và giới hạn traversal. 41 CQ vẫn là hợp đồng cho đường
SPARQL; so sánh PostgreSQL với Oxigraph kiểm tra kết quả trên cùng workload,
còn số đo latency không được xem là engine-only comparison.

### 3.2 Phase 6 — Scale và federation

| # | Việc | Ghi chú |
|---|------|---------|
| 1 | **Scale ladder** 10M entity / 100M edge → 100M / 1B | Probe in-memory đạt 100K entity / 1M location observation ngày 2026-11-12. PostgreSQL backend đã có; cần benchmark durable projection, build index và recovery ở 1M rồi 10M entity. Xem `requirements/scale_targets.md`. |
| 2 | **Federation** theo domain KG và query layer | Nhiều domain KG cùng tuân thủ `core`, `middle` và semantic contracts |
| 3 | **Multi-writer log** | Chưa triển khai; chỉ thiết kế khi benchmark cho thấy single-writer là giới hạn |
| 4 | **Vector candidate generation** | Có thể bổ sung candidate generation; mọi match vẫn cần evidence và review policy hiện hành |

### 3.3 Phase 7 — Vietnam profile và AI

| # | Việc | Ghi chú |
|---|------|---------|
| 1 | `profiles/vietnam/` — VN là **profile**, không phải ontology riêng | Tái sử dụng domain chung (`Vietnam rdf:type Country`); không tạo `VietnameseCityClass` |
| 2 | **Membership/affiliation lane** | `organization.ttl` đã có `memberOf`; dùng reserved terms participation/role |
| 3 | **AI semantic query interface** | NL → competency-query templates → validated SPARQL |

### 3.4 Giải phóng nợ ontology (review mỗi milestone)

21 reserved terms trong core (participation/role, capability/quality,
mereology, temporal boundary) được giữ tới khi Phase 6/7 chốt thiết kế; xóa term
cần MAJOR release. Chi tiết:
`docs/guides/ontology-status.md` §2.

---

## 4. KPI cấp architecture

| # | KPI | Mục tiêu | Hiện tại |
|---|-----|----------|----------|
| 1 | Entity resolution error rate (false merge) | ≈ 0 | Chưa có phép đo production; adversarial tests kiểm tra không tự gộp chỉ theo tên |
| 2 | unresolved rate (nguồn có external ID) | < 1% | Snapshot lịch sử xấp xỉ 0%; cần đo lại trên log hiện hành |
| 3 | Query p95 (Q1 / Q-current / Q4) | 50 / 100 / 500 ms | Gate local đạt trên workload và môi trường đã cấu hình |
| 4 | Projection lag | < 5 s | Gate local đạt trên workload và môi trường đã cấu hình |
| 5 | Ingest E2E throughput | ≥ 20 event/s | Baseline đã commit: 85.8 event/s ở 60 entity (`benchmarks/baseline-e2e.json`) |
| 6 | Semantic stability | core > 0.99, domain > 0.95 | Ngày 2026-11-16: core 0.3333, assertion 0.6667 và identity 0.5 dưới ngưỡng; bốn module còn lại đạt 1.0 |

## 5. Rủi ro chính

| Rủi ro | Giảm thiểu |
|--------|-----------|
| LLM extraction false positive | Model score không phải xác suất fact đúng; candidate có provenance và đi qua validation/review |
| Under-merge tích luỹ (2 QID = 1 thực thể) | chấp nhận theo ADR-0006; repair bằng merge CLI + audit trail |
| Log single-writer giới hạn throughput | đo throughput trước khi thiết kế multi-writer; hiện chưa có multi-writer |
| Read model lỗi hoặc stale | rebuild PostgreSQL projection từ event log rồi kiểm tra checkpoint |
| Reserved terms thành dead weight | review mỗi Phase 6/7 milestone; xoá chỉ qua MAJOR release |

## 6. Quy ước cập nhật tài liệu này

- Snapshot §1 đồng bộ **mỗi milestone**: chạy `make check`; ghi test count theo
  ngày xác minh và events/entities kèm ngày, nguồn dữ liệu.
- Hoàn thành việc nào → chuyển sang §2 với một dòng bằng chứng (commit/file).
- Việc mới phát sinh → §3 theo đúng phase; **không** thêm status patchwork
  rải rác (nguyên nhân roadmap cũ rối).
- Đối tác tài liệu: `docs/architecture.md` (kiến trúc + redesign),
  `docs/guides/ontology-status.md` (đánh giá ontology),
  `docs/CHANGELOG.md` (release ontology tự sinh).
