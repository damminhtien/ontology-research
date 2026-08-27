# Tutorial 01 — Getting started

!!! info "Thời lượng ước tính"
    10–15 phút. Không cần kiến thức RDF trước.

## Prerequisites

- Python ≥ 3.12 (repo đang chạy Python 3.14)
- `make`, `git`
- Kết nối internet lần đầu để cài packages

## Bước 1 — Clone và setup

```bash
git clone https://github.com/damminhtien/ontology-research.git
cd ontology-research
make setup
```

`make setup` tạo virtualenv tại `.venv/` và cài dependencies từ `requirements.txt`
(rdflib, pyshacl, pytest, ruff, fastapi, uvicorn, httpx).

!!! tip "Không dùng make setup lần hai"
    Virtualenv đã tồn tại thì chạy thẳng các target khác. Muốn cài lại từ đầu:
    `rm -rf .venv && make setup`.

## Bước 2 — Chạy bộ kiểm tra đầy đủ

```bash
make check
```

`make check` chạy 6 bước tuần tự — bước nào đỏ dừng ngay tại đó:

| Bước | Lệnh bên dưới | Kiểm tra gì |
|------|---------------|-------------|
| `lint` | `ruff check . && ruff format --check .` | Code style senior chuẩn repo |
| `versions` | `tools/manage_ontology.py check-versions` | SemVer của mọi module khớp release registry |
| `validate` | `tools/validate.py --shapes ... --data ...` | Seed data conform SHACL contracts |
| `dag` | `tools/check_dependency_dag.py` | Import module theo chiều core ← middle ← domain |
| `test` | `pytest` | Toàn bộ test suite |
| `slo` | `tools/check_slo.py` | Query latency không vượt 1.2× baseline và SLO |

Kết quả mong đợi (số test có thể tăng theo thời gian):

```text
All checks passed!
...
[ok   ] ontology/core/core.ttl: no changes since 0.1.0

Version check passed.
RESULT: PASS (32 subclass-closure triples materialized)
Dependency DAG check PASSED (1 files, 0 import edges, no upward deps, no cycles).
121 passed, 1 warning in 3.08s
SLO regression gate PASSED
```

!!! success "Ý nghĩa"
    Đây là **quality gate** của repo: mọi thay đổi sau này phải giữ chuỗi này xanh
    trước khi commit (xem [Coding conventions](../generated/conventions.md)).

## Bước 3 — Định hướng repo

```text
ontology/core/core.ttl     # 20 classes / 33 predicates — semantic kernel
shapes/core_shapes.ttl     # SHACL: Observation cần atTime=1, source>=1...
foundry/                   # platform code: events, identity, ingestion, projector
foundry/console/           # FastAPI + SPA (Dashboard, Explorer, Projection...)
tools/                     # CLI quản lý và đo lường
registry/                  # release registry cho SemVer enforcement
```

## Bước 4 — Chạy thử pipeline thật

```bash
make seed-console   # ghi event log demo qua ingestion pipeline
make console        # mở console UI
```

Mở trình duyệt tại <http://127.0.0.1:8787> — bạn sẽ thấy Dashboard với các
health cards. Dừng server bằng `Ctrl+C`.

## Điều gì vừa xảy ra

Bạn vừa đi qua toàn bộ quality stack của platform: ontology validation (SHACL),
dependency invariant (DAG), version governance (SemVer registry) và performance
contract (SLO gate). Đó là nền tảng cho mọi tutorial tiếp theo.

## Tiếp theo

- [Tutorial 02 — Ontology kernel & SHACL](02-ontology-shacl.md): hiểu kernel và
  vì sao SHACL là contract bắt buộc.
