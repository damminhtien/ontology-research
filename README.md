# Semantic Foundry

[![ci](https://github.com/damminhtien/ontology-research/actions/workflows/ci.yml/badge.svg)](https://github.com/damminhtien/ontology-research/actions/workflows/ci.yml)
[![docs](https://github.com/damminhtien/ontology-research/actions/workflows/docs.yml/badge.svg)](https://damminhtien.github.io/ontology-research/)
[![License: MIT](https://img.shields.io/badge/LICENSE-blue.svg)](LICENSE)

**Production-first semantic platform**: event-sourced canonical knowledge với
identity resolution precision-first, 4 data lanes, SHACL contracts ở mọi điểm
nạp dữ liệu, CQRS read model và lake OLAP — ontology là một phần của kiến trúc,
không phải toàn bộ dự án.

📚 **Tài liệu & tutorials: <https://damminhtien.github.io/ontology-research/>**
📊 **Roadmap**: [roadmap.md](roadmap.md) — Phase 0–5 hoàn thành; Phase 6 đang triển khai, mục provenance đã xong

> Quy tắc duy nhất: **Never make the operational query path pay for semantic
> complexity it does not need.**

## Kiến trúc một màn hình

```text
4 DATA LANES ──► IDENTITY + SHACL GATE ──► EVENT LOG (12 types, append-only)
  reference (Wikidata, cursor-paged Parquet lane)        │
  tracking (AIS feed → sensor/observation/track)         ├─► read model (CQRS,
  documents (extraction — LLM chỉ đề xuất)               │     checkpointed, Console)
  corrections (merge/split/backfill/import)              └─► lake (Parquet+zstd, OLAP)
```

Mọi thay đổi kiến thức là **event bất biến** (không bao giờ rewrite); corrections
là event mới (`EntityMerged`/`EntitySplit`/`ExternalIdBound`); mọi record qua
identity resolution + SHACL gate trước khi chạm log.

## Cấu trúc

```text
roadmap.md                     # Kế hoạch + snapshot đồng bộ mã nguồn
VERSION + CHANGELOG-DATA.md    # Phiên bản hợp đồng dữ liệu (không hardcode trong code)
requirements/                  # Engineering contract (CQ, SLO, scale targets)
ontology/                      # 7 modules: core (20/33) + middle + domain (27 classes)
shapes/                        # 4 SHACL shape files (core/domain/assertion/identity)
foundry/                       # Platform: events, identity, ingestion, lake,
                               #   projector, readmodel, assertions, extraction,
                               #   tracking, reference, merge, namespaces
foundry/console/               # FastAPI + SPA (Dashboard/Explorer/Versions/Impact/Monitor/Admin)
tools/                         # 18 CLI scripts: validate, DAG, manage_ontology, benchmarks,
                               #   ingest_wikidata/tracking_feed, build_reference_lane,
                               #   merge/split/backfill/import_events, check_slo…
registry/                      # Ontology release registry (SemVer enforcement)
benchmarks/                    # 10 implemented competency queries + baselines + e2e gate
tests/                         # pytest suite (kernel, SHACL, CQ, lanes, corrections)
docs/                          # ADRs 0001-0010, architecture, workflows, tutorials
```

## Chạy

```bash
make setup      # .venv + dependencies
make check      # lint + versions + alignment + SHACL(4 files) + DAG + pytest + 2 SLO gates
make fmt        # auto-fix trước khi commit
```

> **Commit discipline**: đọc [CODING_CONVENTIONS.md](CODING_CONVENTIONS.md).
> Tóm lại: task xong commit ngay, `make check` xanh trước commit, Conventional
> Commits, cấm `git add .`. Phiên bản hợp đồng dữ liệu bump qua file `VERSION`
> + `CHANGELOG-DATA.md` (code đọc qua `foundry/versioning.py`, cấm hardcode).

### Data lanes — nạp dữ liệu thật

```bash
# Reference (Wikidata): fetch snapshot Parquet rồi ingest từ lane (không mạng)
.venv/bin/python tools/build_reference_lane.py --class Q3918 --pages 3
.venv/bin/python tools/ingest_wikidata.py --from-lane --class Q3918

# Tracking (AIS feed JSONL → sensor/observation/track qua domain SHACL)
.venv/bin/python tools/ingest_tracking_feed.py --feed data/tracking-feed.jsonl

# Corrections: merge/split/backfill/import — xem guides/workflows.md
```

Chi tiết cả 4 lanes + correction workflows:
[docs/guides/workflows.md](docs/guides/workflows.md).

## Nguyên tắc chính

1. **Never make the operational query path pay for semantic complexity it does not need.**
2. Benchmark → Data pipeline → Query → Ontology richness (không ngược lại).
3. Kernel nhỏ (core 20 classes); domain concepts không bao giờ vào core.
4. SHACL là contract: CI fail khi dữ liệu vi phạm — kể cả dữ liệu từ LLM.
5. Dependency DAG `core ← middle ← domain`; ngược chiều/circular → CI fail.
6. Log append-only: corrections là event mới; precision-first identity —
   lexical similarity không bao giờ tự merge (ADR-0003/0006/0007).

## Ontology Console (UI)

Web console cho quản lý và monitor: các màn hình đọc tổng hợp ontology,
registry, event log và read model; tab Admin cung cấp corrections có token bảo
vệ và audit trên production log.

```bash
make seed-console   # sinh event log demo qua ingestion pipeline thật
make console        # http://127.0.0.1:8787
```

Admin API mặc định bị vô hiệu hóa nếu chưa cấu hình
`FOUNDRY_CONSOLE_TOKEN`. Các thao tác merge/split/backfill dùng production log
(`FOUNDRY_ADMIN_LOG`, mặc định `data/production.jsonl`), không dùng log demo.

Views: **Dashboard** (health cards) · **Explorer** (D3 hierarchy + blast
radius) · **Versions** (SemVer, diff, timeline) · **Impact** (blast radius) ·
**Data Monitor** (event log, SHACL gate, CQ regression) · **Projection** (read
model: entity, location as-of, snapshot resume) · **Admin** (review queue,
merge/split, external-id backfill).

REST API cùng nguồn dữ liệu: `GET /api/overview`, `/api/ontology/*`,
`/api/releases/*`, `/api/impact?term=`, `/api/monitor/*`, `/api/projection/*`,
`/api/admin/*` (xem `/api/docs`).

Query API giữ nguyên dữ liệu chiếu nhưng mặc định ẩn provenance. Cấu hình
`FOUNDRY_PROVENANCE_TOKENS` thành JSON map từ bearer token tới danh sách source
ID được xem; dùng `"*"` để cấp toàn bộ provenance cho một token. Ví dụ:

```bash
export FOUNDRY_PROVENANCE_TOKENS='{"reader-token":["urn:doc:123"]}'
curl -H 'Authorization: Bearer reader-token' \
  http://127.0.0.1:8787/api/projection/entities/urn:entity:123
```

Request không token nhận `source_ids: []`; token không hợp lệ trả `401`, cấu
hình sai trả `503`. `/api/monitor/events/recent` chỉ trả source ID trong scope
và ẩn hẳn `DocumentRegistered` ngoài scope để không lộ URI/tiêu đề. Token này
độc lập với `FOUNDRY_CONSOLE_TOKEN` dành cho thao tác admin.

**Write operations** (Admin view, token-gated): review queue inspection,
merge/split corrections and external-id backfill. Auth: `Authorization: Bearer
$FOUNDRY_CONSOLE_TOKEN` (constant-time compare; biến unset → admin API trả
503 — admin routes bị vô hiệu hóa nếu token chưa cấu hình). **Audit trail =
event log**: merge/split ghi `actor` trong payload; backfill ghi các event
`ExternalIdBound` vào production log (`$FOUNDRY_ADMIN_LOG`, mặc định
`data/production.jsonl`).

## Governance & versioning

- **Ontology modules**: mỗi module khai báo `dcterms:version` (SemVer);
  `registry/` lưu release log + term snapshots; `make check` chạy
  `check-versions` + alignment. `docs/CHANGELOG.md` sinh tự động.
- **Data contracts**: phiên bản quản lý trong file `VERSION` + lịch sử
  `CHANGELOG-DATA.md` (event schema, lake layout, reference lane) — code đọc
  qua `foundry/versioning.py`, freeze-guard test chặn hardcode.

```bash
.venv/bin/python tools/manage_ontology.py stats | stability | blast-radius Track
.venv/bin/python tools/manage_ontology.py release <module> [--migration TEXT]
make visualize-ontology   # build/ontology.html (D3) + ontology.mmd (Mermaid)
```

`make quality` sinh báo cáo schema, SHACL và competency questions tại
`build/ontology-quality.json`. Xem [phương pháp đánh giá](docs/guides/ontology-quality.md)
và [hiện trạng ontology](docs/guides/ontology-status.md).
`make ontology-design` kiểm tra phân cấp lớp và tham chiếu giữa các module;
xem [hướng dẫn đánh giá thiết kế](docs/guides/ontology-design.md).

## Benchmarks & SLO

```bash
make benchmark              # micro benchmark → build/benchmark-report.json
.venv/bin/python tools/benchmark_e2e.py --entities 300   # e2e per-stage report
make check                  # chạy cả 2 SLO gates (read-model + e2e per-stage)
```

Chi tiết stage floors + baseline: `benchmarks/baseline.json`,
`benchmarks/baseline-e2e.json`, `requirements/performance_slo.md`.

## Agent tooling (rtk + graphify)

Hai tool hỗ trợ AI coding agents, đã cài đặt đầy đủ:

### rtk — token-optimized CLI proxy

```bash
rtk ls       # liệt kê files
rtk git status --short
rtk deps     # xem dependency tree
rtk test     # chạy test suite
rtk gain     # xem token savings
```

Filters: `.rtk/filters.toml`; instructions cho agents trong `CLAUDE.md`.

### graphify — codebase knowledge graph

Repo có code graph tại `graphify-out/graph.json`. Hiện chưa cài git hooks tự cập
nhật graph; sau khi sửa code, chạy lệnh cập nhật thủ công:

```bash
graphify query "how does identity resolution work"   # BFS traversal → subgraph
graphify explain "type_closure"                      # 1 node + neighbors
graphify affected "core.ttl"                         # reverse traversal
graphify god-nodes --top 5                           # architectural hubs
graphify update .                                    # rebuild thủ công (no LLM)
```

Sau khi sửa code, chạy `graphify update .`, rồi `graphify query "…"` để agents
tra graph thay vì đọc raw source.

## Tài liệu

| Đọc gì | Ở đâu |
|--------|-------|
| Kiến trúc tổng thể + redesign đã ship | [docs/architecture.md](docs/architecture.md) |
| ADRs 0001–0010 | [docs/adr/](docs/adr/) |
| Data & correction workflows | [docs/guides/workflows.md](docs/guides/workflows.md) |
| Ontology evaluation | [docs/guides/ontology-status.md](docs/guides/ontology-status.md) |
| CLI reference | [docs/guides/cli-reference.md](docs/guides/cli-reference.md) |
| Tutorials 01–06 | [docs/tutorials/](docs/tutorials/) |

## Trạng thái

- [x] Phase 0–1: engineering contract + semantic kernel v0.1 + SHACL + CI
- [x] Phase 2: production ingestion — 4 data lanes, unstructured-document
      extraction (extractor pluggable, LLM chỉ đề xuất); 24.217 entities trong
      historical production snapshot (xem roadmap §1)
- [x] Phase 3: projector checkpointed/idempotent + Console v0.1 + 2 SLO gates
- [x] Phase 4: tracking vertical end-to-end qua domain SHACL contracts
- [x] Phase 5: merge/split/review tooling + Document/Assertion model + reference
      lane + streaming/checkpointed projector + migration/alignment registry +
      **Console admin write ops (token auth + audit trên event log)** — hoàn thành
- [x] Phase 6 item 1: query API provenance access (`FOUNDRY_PROVENANCE_TOKENS`)
- [ ] Phase 6: scale ladder 10M–100M; federation; multi-writer log; vector
      candidate generation
- [ ] Phase 7: Vietnam profile + AI semantic query interface

Chi tiết + snapshot đồng bộ mã nguồn: [roadmap.md](roadmap.md) §1–§3.
