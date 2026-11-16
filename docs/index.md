# Semantic Foundry

Semantic Foundry ghi tri thức chuẩn vào event log chỉ ghi nối tiếp. Ontology
Turtle/OWL và các SHACL shape định nghĩa, kiểm tra mô hình; các read model phục
vụ truy vấn theo nhu cầu.

!!! quote "Quy tắc duy nhất"
    Đường truy vấn vận hành dùng read model có index. Đường truy vấn ngữ nghĩa
    dùng ontology và SPARQL.

## Hệ thống hiện có

| Lớp | Thành phần | Điểm vào |
|-----|------------|----------|
| **Ontology kernel** | 7 module / 31 lớp / 78 thuộc tính; bốn nhóm SHACL canonical và interop profile | `ontology/` · [Trạng thái ontology](guides/ontology-status.md) |
| **Ingestion** | Event log chỉ ghi nối tiếp; identity resolution và SHACL gate | `foundry/` |
| **Read model** | PostgreSQL indexes cho identity, assertion và relation; replay có checkpoint | `foundry/projector.py` · `foundry/postgres_readmodel.py` |
| **Query service** | Truy vấn vận hành có phân trang, timeout và giới hạn traversal | `foundry/query_service.py` · [OSINT query contract](https://github.com/damminhtien/ontology-research/blob/main/requirements/osint_queries.md) |
| **Semantic queries** | 41 CQ regression cases chạy qua RDFLib, Jena và Oxigraph trong các evaluation targets | `benchmarks/` · `make conformance` |
| **Governance** | SemVer registry, migration preflight, blast radius và stability | `registry/` |
| **Console UI** | Dashboard / Explorer / Versions / Impact / Monitor / Projection / provenance-scoped reads / token-gated Admin | `make console` |

## Bắt đầu

```bash
git clone https://github.com/damminhtien/ontology-research.git
cd ontology-research
make setup     # tạo .venv + cài dependencies
make check     # lint + versions + semantic alignment + SHACL + DAG + tests + SLO gates
```

Kết quả mong đợi:

```text
All checks passed!
...
pytest: all collected tests passed
SLO regression gate PASSED
```

Nếu gặp lỗi, bắt đầu với [Tutorial 01 — Getting started](tutorials/01-getting-started.md).

## Tài liệu theo nhu cầu

| Bạn muốn… | Đọc |
|-----------|-----|
| Hiểu kiến trúc hiện tại và các vấn đề đã xử lý | [Kiến trúc hệ thống](architecture.md) |
| Hiểu cách hệ thống chạy end-to-end | [Tutorials 01–06](tutorials/01-getting-started.md) |
| Nạp dữ liệu thật từ Wikidata vào lake | [Tutorial 03 — Ingestion](tutorials/03-ingestion.md) |
| Chạy các luồng sửa dữ liệu và data lanes | [Workflows guide](guides/workflows.md) |
| Tra cứu lệnh CLI | [CLI reference](guides/cli-reference.md) |
| Xem trạng thái triển khai và backlog | [Roadmap](generated/roadmap.md) |
| Hiểu các quyết định kiến trúc | [ADRs 0001–0010](adr/ADR-0001-polyglot-persistence.md) |
| Xem SLO / targets | [Performance SLOs](generated/performance_slo.md) · [Scale targets](generated/scale_targets.md) |
| Quy tắc đóng góp | [Coding conventions](generated/conventions.md) |

## Cấu trúc repo

```text
ontology/core/      # semantic kernel (Turtle + RDFS), namespace frozen (ADR-0008)
shapes/             # SHACL contracts
foundry/            # platform: events (schema v4), identity, ingestion, projector, lake, console
tools/              # CLI: validate, DAG check, manage_ontology, benchmark, check_slo,
                    #      ingest_wikidata, merge_entities, backfill_external_ids
registry/           # release registry (SemVer enforcement)
benchmarks/         # 41 executable competency queries + expected results + SLO baselines
docs/               # bộ tài liệu này
```

Sơ đồ cấu trúc trên chỉ minh họa các module chính. Event schema hiện tại là v6;
PostgreSQL phục vụ identity và operational read model. Bốn shape file canonical
được bổ sung bằng `shapes/interop_shapes.ttl` cho kiểm tra profile liên thông.
