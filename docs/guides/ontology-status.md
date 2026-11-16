# Trạng thái và đánh giá ontology (2026-11-16)

Báo cáo này tổng hợp kết quả từ `tools/manage_ontology.py stats`, `stability`,
`blast-radius` và `report`, cùng usage audit trên `foundry/`, `shapes/` và
`benchmarks/queries`. Chạy `make quality` để tạo lại các phép đo schema, focus
node của SHACL và kết quả CQ; xem [phương pháp đánh giá](ontology-quality.md).

## 1. Inventory: 7 module, 31 lớp, 79 thuộc tính

| Module | Version | Classes | Object properties | Datatype properties | Dùng bởi production code |
|--------|-----|--------:|----------:|-----------:|--------------------------|
| core | 0 | 19 | 24 | 10 | mọi module (kernel) |
| middle/assertion | 1 | 3 | 7 | 15 | `foundry/assertions.py`, `ingestion.py` |
| middle/identity | 1 | 2 | 1 | 3 | `foundry/identity_rdf.py` (Identifier, unresolved references) |
| middle/location | 1 | 2 | 0 | 0 | Country, AdministrativeRegion |
| middle/organization | 1 | 0 | 2 | 0 | `foundry/public_sources.py` (`subsidiaryOf`) |
| domain/sensor | 2 | 1 | 2 | 0 | `foundry/tracking.py` |
| domain/tracking | 2 | 2 | 7 | 8 | `foundry/tracking.py`, `readmodel.py` |

`middle/assertion` tách `Assertion` khỏi `core:Event`: `Assertion` là một
`core:InformationObject`, còn `AssertionMade` là event tạo assertion. Module
này khai báo `assertion:predicate`, `assertion:literalValue`, correction links
và evidence. Location facts dùng cùng lớp assertion. Module tracking có
`ObservationResult` cho vị trí sensor và measurement có kiểu; identity có
`Identifier` để nối scheme, value và entity. Inventory được kiểm tra bằng
`tools/manage_ontology.py stats` ngày 2026-11-16.

Namespace frozen (ADR-0008), freeze guards trong CI; SemVer registry +
`check-versions` xanh. `tools/manage_ontology.py stability` hiện báo:

| Module | Stability | Breaking / releases | Trạng thái |
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

## 2. Thuật ngữ dự trữ và mức sử dụng

Kernel có 53 term. Usage audit ngày 2026-11-12 ghi nhận 32 term được code,
shape hoặc query đã phát hành tham chiếu. Đây là snapshot, chưa được tính lại
tự động. Các term chưa có consumer được giữ làm scaffold cho Phase 6–7:

| Nhóm | Thuật ngữ | Phase dự kiến dùng |
|---|---|---|
| Participation / Role | `Activity`, `Role`, `hasRole`, `playsRole`* (`hasParticipant`, `participatesIn`) | Phase 7 (Vietnam profile: chức danh, vai trò) |
| Capability / Quality | `Capability`, `Quality`, `hasCapability` | Phase 7 (năng lực đơn vị) |
| Mereology | `hasPart`, `partOf`* (`connectedTo`, `dependsOn`, `controls`) | Phase 6 (federation: quan hệ cấu trúc) |
| Temporal boundaries | `startTime`, `endTime` | Phase 6 (khoảng thời gian cho State/Activity) |
| Thuộc tính phản chiếu | `observedBy`, `describedBy`, `hasLocation`, `hasMember`, `externalId` | Thuộc tính nghịch đảo và tiện ích được giữ cho query template |

\* Các term này có trong core nhưng chưa được map. Giữ reserved terms tới khi
Phase 6/7 chốt thiết kế; việc gỡ term cần MAJOR release theo governance.

## 3. Blast radius

Bảng dưới là kết quả audit ngày 2026-11-12; chạy lại lệnh để xem consumer hiện
tại.

| Thuật ngữ | BR | Thành phần sử dụng |
|---|----|-----------|
| `core:Platform` | 4 | core, sensor (modules) + ingestion, tracking (applications) |
| `tracking:Track` | 4 | tracking + ingestion, readmodel, tracking.py |
| `assertion:Assertion` | 3 | assertion module + assertions.py, ingestion.py |

Các phụ thuộc trong audit đi theo chiều core → middle → domain → applications.

## 4. SHACL coverage

| Shapes file | Target | Enforced ở |
|---|---|---|
| core_shapes.ttl | ObservationShape, NamedThingShape | pipeline gate + `make validate` |
| domain_shapes.ttl | TrackShape, ObservationResultShape, SensorShape | pipeline gate (tracking vertical) + `make validate` |
| assertion_shapes.ttl | AssertionShape, DocumentShape | pipeline gate (assertion lane) + `make validate` |
| identity_shapes.ttl | UnresolvedReferenceShape, IdentifierShape | pipeline gate (pending/identity) + `make validate` |
| interop_shapes.ttl | PROV-O, SOSA, GeoSPARQL export constraints | `make conformance` trên export hợp lệ và fixture lỗi |

`make check` tạo quality report và kiểm tra canonical data contracts. Snapshot
ngày 2026-11-16 có 9 trong 15 shape target theo class với focus node; combined
corpus conform. `make conformance` kiểm tra export canonical và interop bằng
pySHACL và Jena, gồm fixture hợp lệ và lỗi. Unit regressions bao phủ assertion,
document, unresolved reference, identifier và observation result. CQ contract
có 41 câu bắt buộc trên workload cố định; xem
[Competency Questions](../generated/competency_questions.md).

## 5. Gaps & hành động

1. **Reserved terms (21)** — đánh dấu trong report này thay vì xoá; reviewed
   lại mỗi Phase 6/7 milestone.
2. **`core:externalId` chưa được dùng** — giá trị theo nguồn được biểu diễn
   thành tài nguyên `identity:Identifier`, thay vì literal trực tiếp trên
   entity; RDF mapper tạo chúng từ `EntityCreated` và `ExternalIdBound`. Giữ
   thuộc tính core dự trữ cho tới khi có quyết định tương thích rõ ràng.
