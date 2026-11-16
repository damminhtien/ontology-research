# Semantic Foundry

[![ci](https://github.com/damminhtien/ontology-research/actions/workflows/ci.yml/badge.svg)](https://github.com/damminhtien/ontology-research/actions/workflows/ci.yml)
[![docs](https://github.com/damminhtien/ontology-research/actions/workflows/docs.yml/badge.svg)](https://damminhtien.github.io/ontology-research/)
[![License: MIT](https://img.shields.io/badge/LICENSE-blue.svg)](LICENSE)

Semantic Foundry lưu tri thức chuẩn trong event log chỉ ghi nối tiếp. Turtle,
OWL và SHACL định nghĩa, kiểm tra mô hình. PostgreSQL phục vụ identity và truy
vấn vận hành; Parquet và DuckDB hỗ trợ phân tích; snapshot Oxigraph dùng để
đánh giá SPARQL ngoại tuyến.

[Tài liệu và tutorials](https://damminhtien.github.io/ontology-research/) ·
[Lộ trình](roadmap.md)

Truy vấn vận hành đi qua read model có index. Truy vấn ngữ nghĩa dùng ontology
và SPARQL.

## Kiến trúc

```text
4 DATA LANES ──► IDENTITY + SHACL ──► EVENT LOG (schema v6, 13 event types)
  reference (Wikidata, cursor-paged Parquet lane)        │
  tracking (AIS feed → sensor/observation/track)         ├─► read model (CQRS,
  documents (extraction — LLM chỉ đề xuất)               │     checkpointed, Console)
  corrections (merge/split/backfill/import)              └─► lake (Parquet+zstd, OLAP)
```

Event log chỉ ghi nối tiếp. Các sửa đổi được ghi thành event mới như
`EntityMerged`, `EntitySplit` và `ExternalIdBound`. Ingestion kiểm tra record
theo hợp đồng identity và SHACL tương ứng trước khi ghi event.

## Cấu trúc

```text
roadmap.md                     # Kế hoạch + snapshot đồng bộ mã nguồn
VERSION + CHANGELOG-DATA.md    # Phiên bản hợp đồng dữ liệu (không hardcode trong code)
requirements/                  # Engineering contract (CQ, SLO, scale targets)
ontology/                      # 7 modules: 31 classes and 78 properties total
shapes/                        # 4 SHACL shape files (core/domain/assertion/identity)
foundry/                       # Events, identity, ingestion, PostgreSQL read model,
                               #   QueryService, assertions, extraction, tracking,
                               #   reference data, RDF projection and lake
foundry/console/               # FastAPI + SPA (Dashboard/Explorer/Versions/Impact/Monitor/Admin)
tools/                         # CLI scripts: validate, DAG, manage_ontology, benchmarks,
                               #   ingest_wikidata/tracking_feed, build_reference_lane,
                               #   merge/split/backfill/import_events, check_slo…
registry/                      # Ontology release registry (SemVer enforcement)
benchmarks/                    # 41 executable competency queries + expected results + SLO/e2e gates
tests/                         # pytest suite (kernel, SHACL, CQ, lanes, corrections)
docs/                          # ADRs 0001-0010, architecture, workflows, tutorials
```

## Chạy

```bash
make setup      # .venv + dependencies
make check      # lint + versions + alignment + SHACL(4 files) + DAG + pytest + 2 SLO gates
make fmt        # auto-fix trước khi commit
```

`make check` cũng chạy ontology design và quality reports. `make conformance` là
gate độc lập: kiểm tra OWL 2 DL/TBox, pySHACL và Jena trên canonical cùng interop
fixtures, 41 competency queries và RDF canonicalization. Bốn shape group trong
`make check` là canonical; `interop_shapes.ttl` được kiểm tra trong conformance.

> **Quy tắc commit**: xem [CODING_CONVENTIONS.md](CODING_CONVENTIONS.md).
> Mỗi task hoàn tất cần một commit; chạy `make check` trước commit; dùng
> Conventional Commits và stage từng đường dẫn cụ thể. Phiên bản hợp đồng dữ
> liệu được quản lý trong `VERSION` và `CHANGELOG-DATA.md`; code đọc qua
> `foundry/versioning.py`.

### Các lane nạp dữ liệu

```bash
# Reference (Wikidata): fetch snapshot Parquet rồi ingest từ lane (không mạng)
.venv/bin/python tools/build_reference_lane.py --class Q3918 --pages 3
.venv/bin/python tools/ingest_wikidata.py --from-lane --class Q3918

# Tracking (AIS feed JSONL → sensor/observation/track qua domain SHACL)
.venv/bin/python tools/ingest_tracking_feed.py \
    --feed "$HOME/Data/ontology-research/data/crawl-runs/tracking-feed.jsonl"

# Corrections: merge/split/backfill/import — xem guides/workflows.md
```

Chi tiết bốn lane và các luồng correction có trong
[hướng dẫn workflows](docs/guides/workflows.md).

## Dữ liệu và đường truy vấn

Tra cứu entity, thời gian, quan hệ và provenance cho vận hành dùng read model
PostgreSQL qua `QueryService`. 41 competency questions chạy như truy vấn ngữ
nghĩa qua các adapter RDFLib, Jena hoặc Oxigraph. Oxigraph hiện là backend đánh
giá; API không chuyển truy vấn vận hành sang đó.

Ontology core có 19 lớp và 34 thuộc tính. Các term theo domain nằm trong module
có version. SHACL kiểm tra hợp đồng dữ liệu của từng lane; dependency DAG giữ
chiều phụ thuộc `core ← middle ← domain`. Identity resolution không gộp record
chỉ vì tên giống nhau. Correction được ghi nối tiếp vào event log.

## Ontology Console

Console hiển thị ontology, registry, event log và read model. Thao tác Admin
cần token và ghi correction có audit vào production log.

```bash
make seed-console   # sinh event log demo qua ingestion pipeline thật
make console        # http://127.0.0.1:8787
```

Identity và read projection cho vận hành dùng PostgreSQL. Runtime data, Parquet
lake và dữ liệu crawl/reference nằm ngoài worktree tại
`~/Data/ontology-research`; `data/` là symlink tương thích. Lệnh cài đặt,
migration, projection tăng dần, rebuild và kiểm tra tích hợp database có trong
[hướng dẫn PostgreSQL](docs/guides/postgres-readmodel.md).

Console có các view Dashboard, Explorer, Versions, Impact, Data Monitor,
Projection và Admin. Projection hỗ trợ tìm entity, truy vấn quan hệ theo thời
gian, traversal có giới hạn, tìm mâu thuẫn, vị trí, track và provenance của
assertion.

REST API cung cấp cùng dữ liệu đọc qua `GET /api/overview`, `/api/ontology/*`,
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

Admin cho phép xem review queue, tạo correction merge/split và backfill external
ID. Các thao tác này cần `Authorization: Bearer $FOUNDRY_CONSOLE_TOKEN`; API trả
`503` nếu chưa cấu hình token. Event merge/split ghi actor. Backfill nối thêm
event `ExternalIdBound` vào `$FOUNDRY_ADMIN_LOG`, mặc định là
`~/Data/ontology-research/data/production.jsonl`. Admin bị tắt khi token chưa
được cấu hình và không dùng demo log.

## Governance và versioning

- Mỗi ontology module khai báo `dcterms:version` theo SemVer. `registry/` lưu
  release log và term snapshot; `make check` chạy `check-versions` và kiểm tra
  alignment. `docs/CHANGELOG.md` được sinh tự động.
- Version của data contract nằm trong `VERSION` và được ghi ở
  `CHANGELOG-DATA.md`. Code đọc qua `foundry/versioning.py`; freeze-guard tests
  ngăn hardcode version.

```bash
.venv/bin/python tools/manage_ontology.py stats | stability | blast-radius Track
.venv/bin/python tools/manage_ontology.py release <module> [--migration TEXT]
make visualize-ontology   # build/ontology.html (D3) + ontology.mmd (Mermaid)
```

`make quality` tạo báo cáo schema, SHACL và competency questions tại
`build/ontology-quality.json`. Xem [phương pháp đánh giá](docs/guides/ontology-quality.md)
và [hiện trạng ontology](docs/guides/ontology-status.md).
`make ontology-design` kiểm tra phân cấp lớp và tham chiếu giữa các module;
xem [hướng dẫn đánh giá thiết kế](docs/guides/ontology-design.md).

## Benchmark và SLO

```bash
make benchmark              # micro benchmark → build/benchmark-report.json
.venv/bin/python tools/benchmark_e2e.py --entities 300   # e2e per-stage report
make check                  # chạy cả 2 SLO gates (read-model + e2e per-stage)
```

Ngưỡng từng stage và baseline: `benchmarks/baseline.json`,
`benchmarks/baseline-e2e.json`, `requirements/performance_slo.md`.

## Công cụ hỗ trợ phát triển

`rtk` và `graphify` hỗ trợ khám phá repository và điều hướng code.

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

| Chủ đề | Tài liệu |
|--------|-------|
| Kiến trúc | [docs/architecture.md](docs/architecture.md) |
| Quyết định kiến trúc | [docs/adr/](docs/adr/) |
| Nạp và sửa dữ liệu | [docs/guides/workflows.md](docs/guides/workflows.md) |
| Đánh giá ontology | [docs/guides/ontology-status.md](docs/guides/ontology-status.md) |
| Tham chiếu CLI | [docs/guides/cli-reference.md](docs/guides/cli-reference.md) |
| Tutorials | [docs/tutorials/](docs/tutorials/) |
| Truy vấn vận hành | [requirements/osint_queries.md](requirements/osint_queries.md) |

## Trạng thái

- [x] Phase 0–1: engineering contract, semantic kernel v0.1, SHACL và CI
- [x] Phase 2: bốn data lane và document extraction; extractor có thể thay thế,
      LLM chỉ tạo đề xuất. Snapshot production lịch sử có 24.217 entity
      (xem roadmap §1).
- [x] Phase 3: projector có checkpoint, Console v0.1 và hai SLO gate
- [x] Phase 4: tracking vertical với domain SHACL contract
- [x] Phase 5: correction, mô hình Document/Assertion, reference lane,
      migration registry và Console write được bảo vệ bằng token
- [x] Nền tảng Phase 6: provenance access, identity/read model PostgreSQL,
      `QueryService` có giới hạn và hợp đồng đánh giá adapter truy vấn
- [ ] Phase 6: scale ladder 10M–100M entity, federation, multi-writer log và
      vector candidate generation
- [ ] Phase 7: Vietnam profile và giao diện truy vấn ngữ nghĩa bằng AI

Chi tiết + snapshot đồng bộ mã nguồn: [roadmap.md](roadmap.md) §1–§3.
