# Semantic Foundry — Roadmap

> Nguyên tắc duy nhất xuyên suốt project:
>
> **Never make the operational query path pay for semantic complexity it does not need.**
>
> Ontology bảo đảm **meaning**; materialized data models bảo đảm **speed**;
> modularity + versioning bảo đảm **evolution**.

Mục tiêu: **Production Semantic Platform** — ontology chỉ là một phần của
architecture, không nằm trên critical data-path. Tài liệu này là kế hoạch 12
tháng: §1 snapshot đồng bộ với mã nguồn, §2 những gì đã xong, §3 backlog có
thứ tự. North-star architecture (polyglot storage, federation theo domain KG
thay vì một central KG): xem `docs/architecture.md` §2.

---

## 1. Snapshot hiện tại (đồng bộ mã nguồn — 2026-09-02)

Đo bằng chính hệ thống: `make check` xanh, **380 tests**, 2 SLO gates
(read-model + e2e per-stage).

| Thành phần | Trạng thái | Bằng chứng trong repo |
|---|---|---|
| Event log (write model) | ✅ | `foundry/events.py` — JSONL segmented, flock + fsync, tail-recovery, sequence toàn cục, upcaster chain, **13 event types** |
| Identity registry | ✅ | `foundry/identity.py` — pure `lookup()`, alias multimap, type-aware, external id multi-valued, `IdentityStore` boundary; registry = log projection |
| Ingestion pipeline | ✅ | `foundry/ingestion.py` — SHACL gate (4 shapes files), **4 data lanes**: reference (Wikidata), tracking (AIS), documents (extraction), corrections (merge/split/backfill/import) |
| Read model (CQRS) | ✅ | `foundry/projector.py` + `readmodel.py` — sequence-ordered, checkpointed, per-event idempotent, snapshot persist; track store |
| Lake | ✅ | `foundry/lake.py` — Parquet+zstd, manifest-authoritative query, event-id dedup, compaction |
| Assertions | ✅ | `foundry/assertions.py` — Document/Assertion ledger, supersedes, SHACL assertion shapes |
| Extraction | ✅ | `foundry/extraction.py` — `PatternExtractor` deterministic + `LlmExtractor` (completion injectable), confidence cap 0.7, không auto-mint |
| Reference lane | ✅ | `foundry/reference.py` + `wd.iter_entities` — cursor paging theo QID, snapshot versioned |
| Tracking vertical | ✅ | `foundry/tracking.py` — `SensorRegistered`/`ObservationRecorded`/`TrackObserved`, gate qua domain SHACL |
| Console | ⚠️ v0.1 read-only | `foundry/console/` — FastAPI + SPA; projection hydrate từ snapshot; **chưa có write ops/auth** |
| Governance | ✅ | `tools/manage_ontology.py` — SemVer registry, blast-radius, stability, migration/alignment |
| Benchmarks | ✅ | micro (`tools/benchmark.py`) + e2e per-stage gate (`tools/benchmark_e2e.py`) |
| Ontology | ✅ | 7 modules / 27 classes / 41 properties — `docs/guides/ontology-status.md` |

**Production facts**: 24.217 canonical entities Wikidata (song ngữ VI–EN) trong
lake Parquet; 47.628 events trong production log; unresolved_rate ≈ 0 với nguồn
có external id.

**Event types hiện hành (13)**: `EntityCreated`, `LocationObserved`,
`AffiliationAssessed`*, `EntityMerged`, `EntitySplit`, `ExternalIdBound`,
`ResolutionReviewQueued`, `DocumentRegistered`, `AssertionMade`,
`AssertionSuperseded`, `SensorRegistered`, `ObservationRecorded`,
`TrackObserved`.

\* `AffiliationAssessed` khai báo từ contract v1 nhưng **chưa có producer** —
implement ở Phase 7 hoặc xoá trong một MAJOR release.

Quyết định kiến trúc chi tiết: 10 ADRs (`docs/adr/`); bản đồ kiến trúc +
redesign đã triển khai: `docs/architecture.md` §6 (bảng commit mapping).

---

## 2. Phase đã hoàn thành

| Phase | Phạm vi đã ship |
|-------|-----------------|
| **0** — Engineering contract | requirements, scale targets, SLO baseline, CQ harness |
| **1** — Semantic kernel v0 | core.ttl (20 classes / 33 props) + SHACL + CI + 16 CQ regression |
| **2** — Production ingestion | event contract v2 (sequence/valid-time/upcasters), identity precision-first (ADR-0003/0006), SHACL gate, **4 data lanes**, unstructured-document extraction (extractor pluggable — LLM chỉ đề xuất), 24k entities thật |
| **3** — Read graph (CQRS) | projector checkpointed + per-event idempotent (ADR-0004), Console v0.1, latency benchmark + SLO gate, e2e per-stage gate (§4.8) |
| **4** — Domain vertical đầu tiên | location/organization/sensor/tracking modules + domain SHACL + ingestion mapping end-to-end (tracking vertical hoàn thành) |
| **5** (phần lớn) | SemVer registry + blast-radius + stability + migration/alignment; merge/split/review-queue/backfill/import corrections; Document/Assertion model (§4.2); reference lane (§4.6); streaming projector; identity store boundary (§4.5) |

Kiến trúc nền B1–B8 theo `docs/architecture.md` §4: log transport bền vững
(§4.1), projector sequence-ordered (§4.3), lake manifest-authoritative (§4.4),
review queue bền vững + un-merge (§4.5), IdentityStore boundary, reference
lane (§4.6), unresolved identity SHACL (§4.7), e2e benchmark (§4.8) —
**tất cả đã ship**; bảng commit mapping ở `docs/architecture.md` §6.

---

## 3. Backlog có thứ tự

### 3.1 Phase 5 — phần còn lại (ngắn, làm trước khi scale)

| # | Việc | Ghi chú |
|---|------|---------|
| 1 | Console **write operations** (release/merge UI) | cần auth + audit trail trên log; console hiện chỉ read |
| 2 | **Review queue UI/drain workflow** | `ResolutionReviewQueued` đã bền vững; cần giao diện phê duyệt → gọi merge/import theo receipt |
| 3 | Quyết định **`AffiliationAssessed`**: implement hoặc xoá | producer chưa tồn tại; xoá = MAJOR contract release |
| 4 | **Mapping config format** (YAML/RML?) | chốt khi nguồn thứ 3+ gia nhập; hiện 4 lane hardcode mapping |

### 3.2 Phase 6 — Scale & federation (Month 6–9)

| # | Việc | Ghi chú |
|---|------|---------|
| 1 | **Access control ở query API layer** (ai thấy provenance nào) | chặn Phase 7 AI interface |
| 2 | **Scale ladder** 10M entities / 100M edges → 100M / 1B | đo: ingest throughput, query p95, memory, index build, recovery |
| 3 | **Federation** theo domain KG + federated query layer | nhiều domain KG conform core + middle + semantic contracts |
| 4 | **Multi-writer log** | segment foundation (B1) sẵn sàng; chỉ làm khi scale đo được nhu cầu |
| 5 | **Vector-based candidate generation** | thay/thêm token-overlap, vẫn qua cùng review gate |

### 3.3 Phase 7 — Vietnam profile + AI (Month 6+, song song 6)

| # | Việc | Ghi chú |
|---|------|---------|
| 1 | `profiles/vietnam/` — VN là **profile**, không ontology đơn khối | reuse domain chung (`Vietnam rdf:type Country`); cấm `VietnameseCityClass` |
| 2 | **Membership/affiliation lane** | `organization.ttl` đã có `memberOf`; dùng reserved terms participation/role |
| 3 | **AI semantic query interface** | NL → competency-query templates → validated SPARQL |

### 3.4 Giải phóng nợ ontology (review mỗi milestone)

21 reserved terms trong core (participation/role, capability/quality,
mereology, temporal boundaries) — chính sách **keep-not-delete** tới khi
Phase 6/7 chốt design; gỡ term = MAJOR release. Chi tiết:
`docs/guides/ontology-status.md` §2.

---

## 4. KPI cấp architecture

| # | KPI | Mục tiêu | Hiện tại |
|---|-----|----------|----------|
| 1 | EntityResolutionErrorRate (false-merge) | ≈ 0 | 0 — không có luồng auto-merge |
| 2 | unresolved_rate (nguồn có external id) | < 1% | ≈ 0% |
| 3 | Query p95 (Q1 / Q-current / Q4) | 50 / 100 / 500 ms | OK (SLO gate) |
| 4 | ProjectionLag | < 5 s | OK (SLO gate) |
| 5 | Ingest e2e throughput | ≥ 20 ev/s floor | 79.8 ev/s @100 entities |
| 6 | Semantic Stability | core > 0.99, domain > 0.95 | 1.0 toàn registry |

## 5. Rủi ro chính

| Rủi ro | Giảm thiểu |
|--------|-----------|
| LLM extraction false positive | confidence cap 0.7 + provenance document + review queue + SHACL gate |
| Under-merge tích luỹ (2 QID = 1 thực thể) | chấp nhận theo ADR-0006; repair bằng merge CLI + audit trail |
| Log single-writer giới hạn throughput | segment layout (B1) sẵn sàng multi-writer; chỉ làm khi Phase 6 đo được nhu cầu |
| Read-model snapshot stale | snapshot là cache — corrupt/stale tự full replay; log vẫn validate toàn bộ |
| Reserved terms thành dead weight | review mỗi Phase 6/7 milestone; xoá chỉ qua MAJOR release |

## 6. Quy ước cập nhật tài liệu này

- Snapshot §1 đồng bộ **mỗi milestone**: chạy `make check`, ghi số tests /
  events / entities thật.
- Hoàn thành việc nào → chuyển sang §2 với một dòng bằng chứng (commit/file).
- Việc mới phát sinh → §3 theo đúng phase; **không** thêm status patchwork
  rải rác (nguyên nhân roadmap cũ rối).
- Đối tác tài liệu: `docs/architecture.md` (kiến trúc + redesign),
  `docs/guides/ontology-status.md` (đánh giá ontology),
  `docs/CHANGELOG.md` (release ontology tự sinh).