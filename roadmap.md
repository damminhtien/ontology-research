# Semantic Foundry — Roadmap 12 tháng

> Nguyên tắc duy nhất xuyên suốt project:
>
> **Never make the operational query path pay for semantic complexity it does not need.**
>
> Ontology bảo đảm **meaning**; materialized data models bảo đảm **speed**;
> modularity + versioning bảo đảm **evolution**.

Mục tiêu: xây một **Production Semantic Platform**, không phải một "Ontology Project".
Ontology chỉ là một phần của architecture, không nằm trên critical data-path nếu không cần thiết.

$$
\text{Production Semantic Platform} > \text{Beautiful Ontology}
$$

Nguyên tắc kiến trúc:

$$
\text{Correct semantics} + \text{bounded evolution} + \text{fast operational queries} + \text{horizontal scalability}
$$

## North-star architecture

```text
                    ┌──────────────────────┐
                    │  Ontology Registry   │
                    │ OWL + SHACL + SKOS   │
                    └──────────┬───────────┘
                               │  Semantic Contracts
        ┌──────────────────────┼───────────────────────┐
        ▼                      ▼                       ▼
 Structured DB             Event Stream            Documents
 SQL / API                 Kafka-like              OSINT/PDF/etc.
        └───────────────┬──────┴──────────────┬────────┘
                        ▼                     ▼
                Semantic Mapping       Entity Resolution
                        └──────────┬──────────┘
                                   ▼
                          Canonical Knowledge
                          (provenance / time / source)
                                   │
                     ┌─────────────┴─────────────┐
                     ▼                           ▼
             Materialized Graph              Lakehouse
             operational view              historical/raw
                     │                           │
               Graph / SQL / Search          OLAP Analytics
                     │
                     ▼
                Application / AI / C2
```

Polyglot storage: **Graph** (traversal), **Relational/columnar** (history + analytics),
**Search** (aliases/text), **Vector** (candidate generation), **Stream** (live events).
Federation theo domain KG thay vì một central KG khổng lồ.

## Tổng quan phase

| Phase | Thời gian | Mục tiêu |
| ----- | --------: | -------- |
| 0     | 2 tuần    | Requirements + benchmark contract |
| 1     | 4 tuần    | Semantic kernel v0 (tiny ontology) |
| 2     | 4–6 tuần  | Production ingestion pipeline |
| 3     | 4–6 tuần  | Fast materialized read graph (CQRS) |
| 4     | 6–8 tuần  | Domain module đầu tiên |
| 5     | 6–8 tuần  | Evolution + governance |
| 6     | 2–3 tháng | Scale / federation |
| 7     | liên tục  | AI + reasoning + expansion |

Thứ tự ưu tiên thực tế:

$$
Benchmark \rightarrow Data\ pipeline \rightarrow Query \rightarrow Ontology\ richness
$$

Thứ tự khi chỉ có 1–2 engineer:

```text
1. Query workload   2. Data model   3. Identity      4. Ingestion
5. Read model       6. Benchmark    7. Tiny ontology 8. SHACL
9. First domain     10. Evolution   11. Federation   12. LLM
```

Điểm phản trực giác: **tiny ontology đứng SAU data/query architecture về engineering priority**,
vì production requirements phải ép ontology vào đúng shape.

## Phase 0 — Engineering contract (Week 1–2)

Scale targets ban đầu (architecture hướng tới, chưa cần đạt ngay):

```text
Entities        10^8
Relations       10^9
Events/day      10^7
Sources         10^3
Ontology terms  10^4–10^5
```

Xác định workload trước ontology: **50–100 Competency Queries** chia 6 nhóm,
mỗi nhóm có SLO p95 riêng (chi tiết trong `requirements/`):

| Nhóm | Ví dụ | Target p95 |
| ---- | ----- | ---------- |
| Q1 Entity lookup | Find entity X | < 50ms |
| Q2 1-hop relationship | Which organization operates platform X? | < 100ms |
| Q3 2–3 hop graph | Sensors operated by orgs in region X | < 300ms |
| Q4 Temporal | Where was entity X at time T? | < 500ms |
| Q5 Provenance | Which sources support fact F? | < 500ms |
| Q6 Analytical | Count/group by region/type/time | chạy OLAP, không trên graph store |

Deliverable Phase 0 (đã có trong repo này):

```text
requirements/competency_questions.md
requirements/performance_slo.md
requirements/scale_targets.md
benchmarks/queries/          # CQ dạng SPARQL, chạy được
benchmarks/datasets/         # dataset mẫu xác định
benchmarks/expected_results/ # ground truth cho regression test
```

## Phase 1 — Semantic kernel v0 (Week 3–6) ✅ implemented

Không xây Vietnam/Military ontology. Chỉ xây `semantic-core`:
**15–25 classes, 30–50 predicates**, chỉ những quan hệ cực ổn định.

Quy tắc đưa predicate vào core: `Coverage(p) > k` domain (ví dụ `partOf` dùng cho
organizations/geography/systems/documents/infrastructure → core;
`launchesMissile` → domain). Không có `Tank`, `Radar`, `Company`, `Province` ở core.

**SHACL ngay từ ngày đầu**: CI fail nếu dữ liệu production vi phạm shape
(ví dụ `Observation` phải có `timestamp = exactly 1`, `source >= 1`).

Deliverables đã có:

```text
ontology/core/core.ttl        # 20 classes, 33 predicates, domain+range đầy đủ
shapes/core_shapes.ttl        # SHACL contracts: Observation / LocationAssertion / Entity
tools/validate.py             # CLI validate data bằng pyshacl
tools/check_dependency_dag.py # invariant: core ← middle ← domain, không circular
tests/                        # pytest: ontology unit tests + SHACL + CQ regression
```

## Phase 2 — Production ingestion pipeline (Week 7–12)

Pipeline chuẩn:

```text
Source → Parser → Schema normalization → Entity extraction → Entity resolution
      → Ontology mapping → Validation → Canonical event/fact
```

Chỉ ingest 3 loại data để chứng minh architecture:

- **A. Slowly-changing structured** (organization, infrastructure, geography)
- **B. High-rate events** (track, sensor observation, telemetry)
- **C. Unstructured** (reports, news, documents)

Yêu cầu cốt lõi:

- **Immutable append-only event log**: `EntityCreated`, `LocationObserved`,
  `AffiliationAssessed`, `RelationshipObserved`… không overwrite history.
- **identity-service** riêng: `canonical_id`, `aliases[]`, `external_ids[]`,
  `confidence`, `source`. Ontology không giải quyết identity resolution
  ("USS Gerald R. Ford" / "CVN-78" / "Gerald Ford Carrier" → một canonical id).
- Global ID sớm: `urn:world:entity:<uuid>` — không dùng DB auto-increment làm identity.
- LLM chỉ ở cuối pipeline:
  `Document → LLM extraction → candidate facts → ER → ontology mapping → SHACL
   → confidence/provenance → human review if needed → KG`.
  LLM chỉ đề xuất; semantic system quyết định acceptance.

Target synthetic: `10^4 – 10^5 events/s`.

## Phase 3 — Materialized read graph (Week 10–16)

Dual representation + **CQRS**:

- **Canonical (write model)**: `LocationAssertion123 {entity, location, validFrom, source}`
  — tối ưu correctness / audit / history / provenance.
- **Operational (read model)**: `AircraftA currentLocation Hanoi`
  — tối ưu latency / simplicity / indexability. Query phổ biến chỉ dùng projection.

Performance invariant (đây là SLO engineering, không phải ontology theorem):

$$
80\%\ operational\ queries \le 3\ graph\ hops
$$

Targets: 1-hop p95 < 100ms · 3-hop p95 < 300ms · ProjectionLag < 5s.

## Phase 4 — Domain ontology đầu tiên (Week 13–20)

Vertical đầu tiên: `Observation → Track → Entity → Organization → Location`
(test được identity, temporal, uncertainty, sensor, organization, geography, provenance).

Module layout, dependency bắt buộc là DAG:

```text
ontology/
├── core/      # kernel v0 (đã có)
├── middle/    # organization, location, information, measurement, temporal
└── domain/    # sensor, platform, tracking, c2
```

Invariant: `Core ↛ Middle ↛ Domain`. Import ngược chiều hoặc circular → CI fail
(`tools/check_dependency_dag.py`, đã có sẵn từ Phase 1).

## Phase 5 — Ontology as software engineering (Week 18–26)

Repository governance:

```text
ontology/{core,middle,domains}/  shapes/  mappings/  tests/  migrations/  docs/
```

- **SemVer** cho từng module (`core 1.2.3`): PATCH = label/doc;
  MINOR = thêm backward-compatible; MAJOR = breaking semantic change.
  **Không bao giờ redefine silently** (đổi nghĩa `Aircraft` v1 → v2 phải qua migration hoặc term mới).
- **Ontology unit tests**: positive (`F16 ⊑ FighterAircraft ⊑ Aircraft ⊑ Platform`)
  và negative (`person playsRole Commander ⇏ person rdf:type CommanderKind`).
- **Regression query tests** mỗi release: so answer correctness + p50/p95/p99;
  ontology update làm p95 tăng > 20% → reject hoặc review.

Evolution metrics:

- Change Blast Radius: `BR(Δ) = N_modules + N_queries + N_mappings + N_applications` — mục tiêu giảm dần.
- Semantic Stability: `Stability(c) = 1 − N_breaking/N_releases`; core > 0.99, domain > 0.95.
- Query Complexity Score: `C(q) = w1·Hops + w2·Joins + w3·ReifiedRels + w4·Filters`, monitor qua release.

## Phase 6 — Scale & federation (Month 6–9)

Synthetic scale ladder: 10M entities/100M edges → 100M entities/1B edges.
Test: ingestion throughput, query p95, memory, storage, index build, recovery, replication.

Federation quan trọng hơn "one huge KG": nhiều domain KG (Geo/Infra/Defence/Economy/
Organization) cùng conform `core + middle + semantic contracts`;
federated query layer xử lý cross-domain.

## Phase 7 — Vietnam profile + AI (Month 6+)

Bắt đầu khoảng **Month 6**, sau khi core/mapping/query/evolution pipeline ổn định.
Vietnam là **profile**, không phải ontology đơn khối:

```text
profiles/vietnam/   # VN Geography, Government, Infrastructure, Economy, Defence…
```

Reuse domain ontology chung: `Vietnam rdf:type Country`,
`Hanoi rdf:type AdministrativeRegion`. Không tạo `VietnameseCountryClass` /
`VietnameseCityClass` trừ khi thực sự có semantics riêng.

Cuối phase: AI semantic query interface
(NL → competency-query templates → validated SPARQL).

## KPI cấp architecture

| # | KPI | Mục tiêu |
| - | --- | -------- |
| 1 | Query latency | p95(operational) < 300ms |
| 2 | Scale | architecture test tới ≥ 10^9 relationships |
| 3 | Semantic coverage | CQ coverage > 95% |
| 4 | Stability | breaking core changes < 1/năm sau khi core mature |
| 5 | Evolution | median blast radius giảm dần |
| 6 | Data quality | ValidFacts > 99.9% sau validation |

SLO vận hành khác:

```text
EntityResolutionErrorRate < 1%
ShapeViolationRate        < 0.1%
ProjectionLag             < 5s
```

Observability: mapping failures, SHACL violation rate, unresolved/duplicate entity rate,
query latency, reasoner latency, projector lag, schema/mapping version, data freshness.

## Team & Definition of Done

Team competence: **Semantic Engineer · Data/Streaming Engineer · Database/Platform
Engineer · Domain Expert** (team nhỏ thì kiêm role).
Code ownership theo `semantic/ data-platform/ identity/ query/ domain/ infra/`,
không chia cứng "ontology team vs backend team".

**DoD cho một ontology module** — chỉ có `*.owl` là CHƯA xong:

- [ ] scope rõ, không circular dependency
- [ ] competency questions
- [ ] SHACL contracts
- [ ] positive/negative reasoning tests
- [ ] query benchmarks
- [ ] mappings + version + migration policy
- [ ] documentation + owner
- [ ] performance regression test

## Phân tích khoảng trống & rủi ro (cập nhật sau Phase 1)

Các gap đã được chốt quyết định qua ADR (xem `docs/adr/`):

| # | Gap | Quyết định | ADR |
|---|-----|------------|-----|
| 1 | Storage engine cho 5 loại workload khác nhau | Polyglot, ontology chỉ bind semantics | ADR-0001 |
| 2 | History/provenance/replay | Append-only event log, corrections = event mới | ADR-0002 |
| 3 | Identity: fuzzy match có tự merge không? | Precision-first — review gate bắt buộc | ADR-0003 |

Rủi ro lớn nhất cần theo dõi liên tục:

```text
R1 False-merge entity      -> giảm revenue tin cậy của cả hệ thống  (ADR-0003)
R2 Projection lag          -> phá SLO p95 operational queries
R3 Ontology drift          -> core bị domain concepts xâm nhập (test đang chặn)
R4 SHACL gate quá ngặt     -> review queue phình to, ingestion nghẽn
R5 Benchmark không đo thật -> KPI thành con số trên giấy (Phase 3 phải có dashboard)
```

Việc còn mở cần quyết định trước khi vào Phase 3:

- [ ] Event supersede/correction semantics (Phase 5 nhưng schema nên chốt sớm)
- [ ] Định dạng mapping config (YAML/RML?) khi số nguồn tăng lên
- [ ] Access control ở query API layer (ai được thấy provenance nào)

## Trạng thái hiện tại

- [x] Phase 0: requirements + benchmark scaffold (CQ queries chạy được với expected results)
- [x] Phase 1: `semantic-core` v0.1 + SHACL + CI tests (pytest + rdflib + pyshacl)
- [~] Phase 2 (đang làm): 
  - [x] Append-only event log + immutable event contract (`foundry/events.py`)
  - [x] Identity service precision-first (`foundry/identity.py`, ADR-0003)
  - [x] Ingestion pipeline với SHACL gate (`foundry/ingestion.py`) — structured records + location observations
  - [ ] Unstructured documents (LLM extraction trước cùng gate đó)
  - [ ] Throughput benchmark 10^4-10^5 events/s (synthetic)
- [~] Phase 5 (bắt đầu sớm): version governance
  - [x] Release registry (`registry/`) + SemVer enforcement trong `make check`
  - [x] `release` với migration note bắt buộc cho MAJOR; changelog sinh tự động
  - [x] Blast radius analysis (`blast-radius`) + stability metric (`stability`)
  - [ ] Migration scripts tự động hóa; alignment registry
- [~] Tooling: Ontology Console v0.1 (read-only UI)
  - [x] FastAPI backend (`foundry/console/`) tái dùng logic tools/ + foundry/
  - [x] SPA: Dashboard / Explorer / Versions / Impact / Data Monitor
  - [x] API tests; seed script qua ingestion pipeline thật
  - [ ] Write operations từ UI (release) — cần auth + audit trail (Phase 3+)
- [ ] Phase 3: projector + read models + benchmark dashboard
- [ ] Phase 4+: xem bảng phase ở trên



