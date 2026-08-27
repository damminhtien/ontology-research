# Semantic Foundry

Production-first semantic platform scaffold — Phase 0 + Phase 1 của
[roadmap.md](roadmap.md) đã được implement và kiểm chứng bằng CI tests.

📚 **Tài liệu & tutorials: <https://damminhtien.github.io/ontology-research/>**

## Cấu trúc

```text
roadmap.md                     # Kế hoạch 12 tháng + KPIs + DoD
requirements/                  # Engineering contract (Phase 0)
  competency_questions.md      #   50–100 CQ, chia nhóm Q1–Q6
  performance_slo.md           #   SLO p95 + quality gates + regression policy
  scale_targets.md             #   Scale ladder tới 10^9 relations
ontology/
  core/core.ttl                # Semantic kernel v0.1: 20 classes, 33 predicates
shapes/
  core_shapes.ttl              # SHACL contracts cho Observation/LocationAssertion/named things
benchmarks/
  queries/*.rq                 # 6 competency queries chạy được (SPARQL)
  datasets/sample_data.ttl     # Seed dataset xác định
  expected_results/*.json      # Ground truth cho regression test
tools/
  validate.py                  # SHACL validator (pyshacl), exit≠0 nếu vi phạm
  check_dependency_dag.py      # Invariant core←middle←domain, không circular imports
tests/                         # pytest: kernel contract + reasoning + SHACL + CQ regression
```

## Chạy

```bash
make setup     # tạo .venv + cài rdflib/pyshacl/pytest/ruff
make check     # lint (ruff) + SHACL validate + DAG check + toàn bộ tests
make fmt       # auto-fix lint + format trước khi commit
```

> **Commit discipline**: bắt buộc đọc [CODING_CONVENTIONS.md](CODING_CONVENTIONS.md).
> Tóm lại: mỗi task xong phải commit ngay, `make check` phải xanh trước khi commit,
> message theo Conventional Commits, cấm `git add .`.

Từng bước:

```bash
.venv/bin/python tools/validate.py --shapes shapes/core_shapes.ttl \
                                   --data benchmarks/datasets/sample_data.ttl
.venv/bin/python tools/check_dependency_dag.py
.venv/bin/python -m pytest -q
```

## Nguyên tắc chính

1. **Never make the operational query path pay for semantic complexity it does not need.**
2. Benchmark → Data pipeline → Query → Ontology richness (không ngược lại).
3. Kernel giữ kích thước nhỏ (15–25 classes, 30–50 predicates); domain concepts
   (`Tank`, `Sensor`, `Company`…) không bao giờ vào core.
4. SHACL là contract bắt buộc: CI fail khi dữ liệu vi phạm.
5. Dependency module là DAG hướng xuống: `core ← middle ← domain`;
   import ngược chiều hoặc circular → CI fail.

## Ontology Console (UI)

Web console cho người quản lý và monitor ontology — read-only, tái sử dụng 100%
logic của các tool đã test:

```bash
make seed-console   # sinh data/events.jsonl demo qua ingestion pipeline thật
make console        # mở http://127.0.0.1:8787
```

Views: **Dashboard** (health cards: version check, DAG, CQ regression, stability,
events) · **Explorer** (D3 hierarchy + chi tiết term + blast radius) ·
**Versions** (pending release + suggested bump, diff 2 versions, release
timeline) · **Impact** (blast radius theo term) · **Data Monitor** (event log,
SHACL gate, competency queries).

REST API cùng nguồn dữ liệu: `GET /api/overview`, `/api/ontology/*`,
`/api/releases/*`, `/api/impact?term=`, `/api/monitor/*` (xem `/api/docs`).
Console chỉ đọc — release/tag vẫn qua CLI flow; không auth, bind localhost.

## Ontology versioning

Mỗi module khai báo `dcterms:version` (SemVer); `registry/` lưu release log +
term snapshots làm baseline. `make check` chạy `check-versions`: sửa semantics
mà không bump, hoặc bump sai mức, sẽ fail CI.

```bash
make versions                                              # kiểm tra consistency
.venv/bin/python tools/manage_ontology.py release core --dry-run  # preview + blast radius
.venv/bin/python tools/manage_ontology.py release core \
    --migration "gadget range widened; re-map data"        # MAJOR bắt buộc migration note
.venv/bin/python tools/manage_ontology.py blast-radius Platform
.venv/bin/python tools/manage_ontology.py stability        # core >= 0.99 được enforce
```

Phân loại: đổi `domain`/`range`, xóa term → **MAJOR** (+migration bắt buộc);
thêm term → **MINOR**; label/comment → **PATCH**. Release log nằm ở
`registry/releases.json`; `docs/CHANGELOG.md` sinh tự động, không sửa tay.

## Ontology tooling

### Visualization & management

```bash
make visualize-ontology   # sinh build/ontology.html (D3 interactive) + ontology.mmd (Mermaid)
make ontology-report      # sinh build/ontology-report.md + bảng stats từng module
```

Quản lý lifecycle (`tools/manage_ontology.py`):

```bash
.venv/bin/python tools/manage_ontology.py stats                    # đếm terms theo module
.venv/bin/python tools/manage_ontology.py diff old.ttl new.ttl     # semantic diff + SemVer severity
.venv/bin/python tools/manage_ontology.py new-module organization --layer middle
```

`diff` thực thi rule **"never redefine silently"** (CODING_CONVENTIONS.md):
đổi `domain`/`range` hoặc xóa term = MAJOR (exit 1, chặn CI); thêm term mới =
MINOR; đổi label/comment = PATCH. Chỉ vượt qua MAJOR bằng `--allow-breaking`.

## Agent tooling (rtk + graphify)

Repo này đã cấu hình sẵn hai tool hỗ trợ AI coding agents:

### rtk — token-optimized CLI proxy
Lọc/nén output của các lệnh shell trước khi vào context của LLM.

```bash
rtk ls | rtk git status | rtk deps | rtk test   # ví dụ
rtk gain                                        # xem token savings
```

- Instructions cho agents nằm trong `CLAUDE.md` (mục RTK).
- Project filters: `.rtk/filters.toml` — thêm filter riêng cho output của
  `tools/validate.py`, pytest… nếu cần nén thêm.
- Golden rule của rtk: **luôn prefix lệnh bằng `rtk`** — có filter chuyên dụng thì
  dùng, không có thì pass-through, nên luôn an toàn.

### graphify — codebase knowledge graph
Xây `graphify-out/graph.json` từ codebase; agents tra graph thay vì đọc raw source.

```bash
graphify query "How does SHACL validation work?"   # BFS traversal, trả subgraph nhỏ
graphify explain "type_closure"                    # giải thích 1 node + neighbors
graphify affected "core.ttl"                       # reverse traversal: cái gì bị ảnh hưởng
graphify god-nodes --top 5                         # architectural hubs
graphify update .                                  # rebuild sau khi sửa code (no LLM)
```

- Đã cài: section trong `AGENTS.md` + `CLAUDE.md`, PreToolUse hooks cho Claude Code
  (`.claude/settings.json`) và Codex (`.codex/hooks.json`), git hooks
  post-commit/post-checkout tự rebuild graph, merge driver cho `graph.json`.
- `graphify-out/` là artifact sinh tự động → đã ignore trong git.
- Lưu ý: `graphify hook status` hiện báo "not installed" dù hooks đã cài đúng
  (quirk của tool v0.9.48) — kiểm tra bằng `ls .git/hooks/` nếu nghi ngờ.

## Trạng thái

- [x] Phase 0: requirements + benchmark harness (correctness regression)
- [x] Phase 1: semantic-core v0.1 + SHACL + CI tests
- [ ] Phase 2: ingestion pipeline + identity service + append-only event log
- [ ] Phase 3: projector + read models + latency benchmark dashboard
