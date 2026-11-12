# Ontology status & evaluation (2026-11-12)

Báo cáo đánh giá định kỳ của ontology — nguồn số liệu: `tools/manage_ontology.py
stats / stability / blast-radius / report`, audit usage chạy trên toàn bộ
`foundry/`, `shapes/`, `benchmarks/queries`.
Chạy `make quality` để lấy số liệu mới về khai báo schema, focus node của
SHACL và kết quả CQ; xem [phương pháp đánh giá](ontology-quality.md).

## 1. Inventory: 7 modules, 28 classes, 68 properties

| Module | Lớp | Classes | Obj-props | Data-props | Dùng bởi production code |
|--------|-----|--------:|----------:|-----------:|--------------------------|
| core | 0 | 19 | 24 | 9 | mọi module (kernel) |
| middle/assertion | 1 | 2 | 7 | 6 | `foundry/assertions.py`, `ingestion.py` |
| middle/identity | 1 | 2 | 1 | 3 | `foundry/identity_rdf.py` (Identifier, unresolved references) |
| middle/location | 1 | 2 | 0 | 0 | Country, AdministrativeRegion |
| middle/organization | 1 | 0 | 1 | 0 | membership mapping (chưa có lane) |
| domain/sensor | 2 | 1 | 2 | 0 | `foundry/tracking.py` |
| domain/tracking | 2 | 2 | 7 | 8 | `foundry/tracking.py`, `readmodel.py` |

`middle/assertion` tách `Assertion` khỏi `core:Event` (nay ⊑
`core:InformationObject` — `AssertionMade` mới là event) và có
`assertion:predicate` (quan hệ dạng IRI), `assertion:literalValue`, correction
links and evidence. Location facts use this same assertion class. The existing
tracking module adds `ObservationResult` for sensor position and typed
measurements; identity adds `Identifier` for scheme/value/entity links. No
module or domain taxonomy was added. Inventory was checked with
`tools/manage_ontology.py stats` on 2026-11-12.

Namespace frozen (ADR-0008), freeze guards trong CI; SemVer registry +
`check-versions` xanh. `tools/manage_ontology.py stability` hiện báo:

| Module | Stability | Breaking / releases | Status |
|---|---:|---:|---|
| core | 0.3333 | 2 / 3 | dưới ngưỡng 0.99 |
| assertion | 0.6667 | 1 / 3 | dưới ngưỡng 0.95 |
| sensor | 1.0 | 0 / 1 | đạt ngưỡng |
| tracking | 1.0 | 0 / 2 | đạt ngưỡng |
| identity | 0.5 | 1 / 2 | dưới ngưỡng 0.95 |
| location | 1.0 | 0 / 1 | đạt ngưỡng |
| organization | 1.0 | 0 / 1 | đạt ngưỡng |

Ba module dưới ngưỡng là cảnh báo governance được báo bởi công cụ; chạy lại
`stability` sau mỗi release để cập nhật trạng thái.

## 2. Reserved terms và usage coverage

Kernel hiện giữ 53 terms. Usage audit trước đây ghi nhận 32 terms được shipped
code/shapes/queries tham chiếu; đây là snapshot audit, không được tính lại tự
động. Các terms chưa có consumer được giữ làm scaffold cho Phase 6–7 theo nhóm
sau:

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
| core_shapes.ttl | ObservationShape, NamedThingShape | pipeline gate + `make validate` |
| domain_shapes.ttl | TrackShape, ObservationResultShape, SensorShape | pipeline gate (tracking vertical) + `make validate` |
| assertion_shapes.ttl | AssertionShape, DocumentShape | pipeline gate (assertion lane) + `make validate` |
| identity_shapes.ttl | UnresolvedReferenceShape, IdentifierShape | pipeline gate (pending/identity) + `make validate` |

SHACL contracts được kiểm tra trong `make check`. Snapshot 2026-11-12 có 9/9
shape target theo class với focus node và corpus conform; unit regressions cũng
kiểm tra assertion, document, unresolved reference, identifier và observation
result. Có 41 competency queries bắt buộc, chạy được trên workload cố định;
các câu ngoài workload hiện tại đã được gỡ khỏi contract
(xem [Competency Questions](../generated/competency_questions.md)).

## 5. Gaps & hành động

1. **organization.ttl chỉ có 1 property (`memberOf`), 0 class** — membership
   lane chưa có data source; giữ nguyên tới khi Phase 7 VN profile cần.
2. **Reserved terms (21)** — đánh dấu trong report này thay vì xoá; reviewed
   lại mỗi Phase 6/7 milestone.
3. **`core:externalId` remains unused** — source-scoped values are represented
   as `identity:Identifier` resources instead of a flat entity literal, and
   mapped to RDF from `EntityCreated`/`ExternalIdBound`. Keep the reserved core
   mirror unchanged until an explicit compatibility decision.
