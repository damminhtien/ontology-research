# Tutorial 02 — Ontology kernel & SHACL

!!! info "Thời lượng ước tính"
    20 phút. Cần hoàn thành [Tutorial 01](01-getting-started.md).

## Kernel là gì?

`ontology/core/core.ttl` là **semantic kernel** — bộ từ vựng tối thiểu, cực ổn định,
dùng chung cho mọi domain. Thiết kế giữ budget chặt: **15–25 classes, 30–50 predicates**
(hiện tại: 20 / 33). Test `tests/test_ontology.py` chặn mọi vi phạm budget.

```bash
.venv/bin/python tools/manage_ontology.py stats
```

```text
module                       classes obj-props data-props
ontology/core/core.ttl            20        24          9
TOTAL                             20                            33 properties
```

## Quy tắc thêm term vào kernel

1. **Predicate chỉ vào core khi dùng được ở ≥ nhiều domain.** `partOf` dùng cho
   organizations/geography/systems → core. `launchesMissile` → domain module.
2. **Mọi predicate mới phải khai báo `rdfs:domain` + `rdfs:range`.**
   Test chặn predicate thiếu domain/range.
3. **Domain concepts cấm vào core** (`Tank`, `Radar`, `Company`…).
4. **Không bao giờ redefine silently** — đổi nghĩa term đã publish = breaking change,
   bắt buộc bump MAJOR + migration note (xem [Tutorial 05](05-versioning.md)).

Ví dụ một class và một predicate trong kernel:

```turtle
core:Observation a owl:Class ; rdfs:subClassOf core:Event ;
    rdfs:label "Observation"@en ;
    rdfs:comment "An act of observing, anchored in time and to a source."@en .

core:hasSource a owl:ObjectProperty ;
    rdfs:domain core:Event ; rdfs:range core:Source ;
    rdfs:label "has source"@en .
```

## SHACL — contract dữ liệu bắt buộc

SHACL shapes trong `shapes/core_shapes.ttl` là **gate dữ liệu**: mọi dữ liệu phải
conform trước khi vào hệ thống. Ba shape chính:

- `ObservationShape` — đúng 1 `atTime` (xsd:dateTime), ≥ 1 `hasSource`, ≥ 1 `observes`
- `LocationAssertionShape` — đúng 1 entity, đúng 1 location, đúng 1 `validFrom`, ≥ 1 source
- `NamedThingShape` — agents/artifacts/locations/sources phải có `name`

## Chạy validation

```bash
.venv/bin/python tools/validate.py \
    --shapes shapes/core_shapes.ttl \
    --data benchmarks/datasets/sample_data.ttl
```

```text
RESULT: PASS (32 subclass-closure triples materialized)
```

Bây giờ thử dữ liệu vi phạm — observation thiếu `atTime` và `hasSource`:

```bash
cat > /tmp/bad_obs.ttl << 'EOF'
@prefix core: <https://ontology.example/core#> .
@prefix ex: <https://data.example/entity/> .
ex:obs-x a core:Observation ; core:observes ex:patrol-01 .
EOF
.venv/bin/python tools/validate.py --shapes shapes/core_shapes.ttl --data /tmp/bad_obs.ttl
```

```text
Validation Report
Conforms: False
Results (3):
Constraint Violation in ClassConstraintComponent ...
	Result Path: core:observes
	Message: Observation must observe at least one Entity.
Constraint Violation in MinCountConstraintComponent ...
	Message: Observation must have exactly one xsd:dateTime atTime.
...
```

Exit code ≠ 0 → CI fail → dữ liệu không bao giờ lọt qua gate.

## Kiểm tra cấu trúc module (DAG)

Kernel không được import module khác; middle/domain chỉ được import "xuống dưới":

```bash
.venv/bin/python tools/check_dependency_dag.py
```

```text
Dependency DAG check PASSED (1 files, 0 import edges, no upward deps, no cycles).
```

## Bài tập

1. Thêm một class `Sensor` vào `core.ttl` rồi chạy `make check`. Quan sát điều gì
   chặn bạn (gợi ý: bank sách budget hoặc no-leak test).
2. Sửa `shapes/core_shapes.ttl` cho phép `Observation` không cần source. Điều gì
   xảy ra với contract "provenance là first-class"? Revert lại.

## Điều gì vừa xảy ra

Bạn đã thấy hai lớp bảo vệ của kernel: **budget test** giữ ontology nhỏ và ổn định,
**SHACL gate** ép dữ liệu tuân thủ semantic contract trước khi vào hệ thống.

## Tiếp theo

- [Tutorial 03 — Ingestion pipeline](03-ingestion.md): dữ liệu đi vào hệ thống thế nào.
