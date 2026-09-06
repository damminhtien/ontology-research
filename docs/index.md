# Semantic Foundry

**Production-first semantic platform** — nơi ontology là một phần của kiến trúc,
không phải toàn bộ dự án. Nguyên tắc xuyên suốt:

!!! quote "Quy tắc duy nhất"
    **Never make the operational query path pay for semantic complexity it does not need.**
    Ontology bảo đảm *meaning*; materialized data models bảo đảm *speed*;
    modularity + versioning bảo đảm *evolution*.

## Hệ thống hiện có

| Lớp | Thành phần | Điểm vào |
|-----|------------|----------|
| **Ontology kernel** | 20 classes / 33 predicates, SHACL contracts | `ontology/core/core.ttl` |
| **Ingestion** | Append-only event log, identity resolution precision-first, SHACL gate | `foundry/` |
| **Read model** | CQRS projector + query latency benchmark với SLO gate | `foundry/projector.py` |
| **Governance** | SemVer registry, migration gate cho MAJOR, blast radius, stability | `registry/` |
| **Console UI** | Dashboard / Explorer / Versions / Impact / Monitor / Projection | `make console` |

## Quickstart — 5 phút tới trạng thái xanh

```bash
git clone https://github.com/damminhtien/ontology-research.git
cd ontology-research
make setup     # tạo .venv + cài dependencies
make check     # lint + version check + SHACL validate + DAG + tests + SLO gate
```

Kết quả mong đợi:

```text
All checks passed!
...
254 passed
SLO regression gate PASSED
```

Nếu gặp lỗi, bắt đầu với [Tutorial 01 — Getting started](tutorials/01-getting-started.md).

## Tài liệu theo nhu cầu

| Bạn muốn… | Đọc |
|-----------|-----|
| Hiểu kiến trúc tổng thể và hướng thiết kế lại | [Kiến trúc hệ thống](architecture.md) |
| Hiểu cách hệ thống chạy end-to-end | [Tutorials 01–06](tutorials/01-getting-started.md) |
| Nạp dữ liệu thật từ Wikidata vào lake | [Tutorial 03 — Ingestion](tutorials/03-ingestion.md) |
| Tra cứu lệnh CLI | [CLI reference](guides/cli-reference.md) |
| Biết kế hoạch 12 tháng | [Roadmap](generated/roadmap.md) |
| Hiểu các quyết định kiến trúc | [ADRs 0001–0010](adr/ADR-0001-polyglot-persistence.md) |
| Xem SLO / targets | [Performance SLOs](generated/performance_slo.md) · [Scale targets](generated/scale_targets.md) |
| Quy tắc đóng góp | [Coding conventions](generated/conventions.md) |

## Cấu trúc repo

```text
ontology/core/      # semantic kernel (Turtle + RDFS), namespace frozen (ADR-0008)
shapes/             # SHACL contracts
foundry/            # platform: events (schema v2), identity, ingestion, projector, lake, console
tools/              # CLI: validate, DAG check, manage_ontology, benchmark, check_slo,
                    #      ingest_wikidata, merge_entities, backfill_external_ids
registry/           # release registry (SemVer enforcement)
benchmarks/         # competency queries + expected results + baseline SLO
docs/               # bộ tài liệu này
```
