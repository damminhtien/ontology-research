# Ontology status & evaluation (2026-09-02)

Báo cáo đánh giá định kỳ của ontology — nguồn số liệu: `tools/manage_ontology.py
stats / stability / blast-radius / report`, audit usage chạy trên toàn bộ
`foundry/`, `shapes/`, `benchmarks/queries`.

## 1. Inventory: 7 modules, 27 classes, 43 properties

| Module | Lớp | Classes | Obj-props | Data-props | Dùng bởi production code |
|--------|-----|--------:|----------:|-----------:|--------------------------|
| core | 0 | 20 | 24 | 9 | mọi module (kernel) |
| middle/assertion | 1 | 2 | 3 | 1 | `foundry/assertions.py`, `ingestion.py` |
| middle/identity | 1 | 1 | 0 | 0 | `foundry/ingestion.py` (D12 pending) |
| middle/location | 1 | 2 | 0 | 0 | LocationAssertion (gate observation) |
| middle/organization | 1 | 0 | 1 | 0 | membership mapping (chưa có lane) |
| domain/sensor | 2 | 1 | 2 | 0 | `foundry/tracking.py` (Phase 4) |
| domain/tracking | 2 | 1 | 2 | 1 | `foundry/tracking.py`, `readmodel.py` (Phase 4) |

> Cập nhật 2026-09-17: `middle/assertion` tách `Assertion` khỏi `core:Event`
> (nay ⊑ `core:InformationObject` — `AssertionMade` mới là event) và thêm
> `assertion:predicate` (quan hệ dạng IRI) + `assertion:literalValue`; số liệu
> inventory trên đã tính theo module hiện hành (`manage_ontology.py stats`).

Namespace frozen (ADR-0008), freeze guards trong CI; SemVer registry +
`check-versions` xanh; **Stability = 1.0 cho cả 6 module** (0 breaking).

## 2. Usage coverage của core kernel: 32/53 terms đang dùng

Kernel giữ 53 terms; **32 terms được shipped code/shapes/queries tham chiếu**
(mọi terms mà 4 data lanes + correction workflows + SHACL shapes cần). 21 terms
còn lại là **reserved scaffold** cho Phase 6–7, KHÔNG phải dead weight —
mỗi nhóm gắn một phase trong roadmap:

| Nhóm reserved | Terms | Phase dự kiến dùng |
|---|---|---|
| Participation / Role | `Activity`, `Role`, `hasRole`, `playsRole`* (`hasParticipant`, `participatesIn`) | Phase 7 (Vietnam profile: chức danh, vai trò) |
| Capability / Quality | `Capability`, `Quality`, `hasCapability` | Phase 7 (năng lực đơn vị) |
| Mereology | `hasPart`, `partOf`* (`connectedTo`, `dependsOn`, `controls`) | Phase 6 (federation: quan hệ cấu trúc) |
| Temporal boundaries | `startTime`, `endTime` | Phase 6 (khoảng thời gian cho State/Activity) |
| Mirrors | `observedBy`, `describedBy`, `hasLocation`, `hasMember`, `externalId` | inverse/nvenience properties — giữ cho query templates |

\* terms tồn tại trong core nhưng chưa mapped. Chính sách: **reserved terms
được giữ nguyên** (blast radius = 0 vì không consumer), chỉ gỡ khi Phase 6/7
chốt design — gỡ term = MAJOR release theo governance, không đáng khi chưa
biết shape cuối.

## 3. Blast radius (sức khoẻ cấu trúc)

| Term | BR | Consumers |
|---|----|-----------|
| `core:Platform` | 4 | core, sensor (modules) + ingestion, tracking (applications) |
| `tracking:Track` | 4 | tracking + ingestion, readmodel, tracking.py |
| `assertion:Assertion` | 3 | assertion module + assertions.py, ingestion.py |

BR nhỏ và có hướng (core → middle → domain → apps). Không term nào có BR lan
vào queries (0) — competency queries chỉ phụ thuộc class hierarchy, đúng design.

## 4. SHACL coverage

| Shapes file | Target | Enforced ở |
|---|---|---|
| core_shapes.ttl | ObservationShape, LocationAssertionShape, NamedThingShape | pipeline gate + `make validate` |
| domain_shapes.ttl | TrackShape, SensorShape | pipeline gate (tracking vertical) + `make validate` |
| assertion_shapes.ttl | AssertionShape, DocumentShape | pipeline gate (assertion lane) + `make validate` |
| identity_shapes.ttl | UnresolvedReferenceShape | pipeline gate (pending) + `make validate` |

Mỗi shape đều có unit test riêng; 16 competency questions pass trong CI.

## 5. Gaps & hành động

1. **organization.ttl chỉ có 1 property (`memberOf`), 0 class** — membership
   lane chưa có data source; giữ nguyên tới khi Phase 7 VN profile cần.
2. **Reserved terms (21)** — đánh dấu trong report này thay vì xoá; reviewed
   lại mỗi Phase 6/7 milestone.
3. **`core:externalId` unused** — external ids vận hành qua event payload
   (`external_ids`/`ExternalIdBound`), không qua RDF graph; chấp nhận (khoản
   chênh giữa write model RDF mapping và event payload), reviewed nếu cần
   query external ids qua SPARQL.
