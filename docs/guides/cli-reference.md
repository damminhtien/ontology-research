# CLI reference

Toàn bộ lệnh CLI của repo. Chạy từ root repo với `.venv/` đã setup (`make setup`).

## Ontology management — `tools/manage_ontology.py`

| Lệnh | Chức năng |
|------|-----------|
| `stats` | Đếm classes/object-props/data-props theo module |
| `diff OLD NEW` | Semantic diff giữa 2 file .ttl, phân loại severity |
| `check-versions` | Enforce SemVer nhất quán với release registry (chạy trong `make check`) |
| `release MODULE [--migration TEXT] [--dry-run]` | Ghi release; MAJOR bắt buộc `--migration` |
| `blast-radius TERM` | Đếm consumers theo 3 lớp: modules/queries/applications |
| `stability` | `Stability(m) = 1 − N_breaking/N_releases` theo module |
| `report` | Sinh markdown registry report vào `build/` |
| `new-module NAME --layer middle\|domain` | Scaffold module mới với header chuẩn |

## Validation & invariants

| Lệnh | Chức năng |
|------|-----------|
| `.venv/bin/python tools/validate.py --shapes FILE --data FILE` | SHACL validation, exit ≠ 0 nếu vi phạm |
| `.venv/bin/python tools/check_dependency_dag.py` | Check DAG import core ← middle ← domain |

## Benchmark & SLO

| Lệnh | Chức năng |
|------|-----------|
| `.venv/bin/python tools/benchmark.py [--scale N] [--observations N] [--runs N]` | Synthetic benchmark → `build/benchmark-report.json` |
| `.venv/bin/python tools/check_slo.py` | SLO regression gate (1.2× baseline + SLO tuyệt đối) |
| `.venv/bin/python tools/check_slo.py --generate-baseline` | Cập nhật `benchmarks/baseline.json` |

## Data plane

| Lệnh | Chức năng |
|------|-----------|
| `.venv/bin/python tools/seed_console_data.py [--force]` | Seed event log demo qua ingestion pipeline thật |

## Visualization & Console

| Lệnh | Chức năng |
|------|-----------|
| `make visualize-ontology` | Sinh `build/ontology.html` (D3) + `ontology.mmd` (Mermaid) |
| `make console` | Console UI tại <http://127.0.0.1:8787> |
| `make benchmark` | Benchmark mặc định (scale 1000 × 3 observations) |

## Documentation site

| Lệnh | Chức năng |
|------|-----------|
| `make docs-setup` | Cài mkdocs-material vào `.venv` |
| `make docs-serve` | Live-reload tại <http://127.0.0.1:8000> |
| `make docs-build` | Build strict vào `site/` (CI chạy bước này) |

## Make targets tổng hợp

| Target | Bao gồm |
|--------|---------|
| `make setup` | Tạo `.venv` + cài `requirements.txt` |
| `make check` | `lint` + `versions` + `validate` + `dag` + `test` + `slo` |
| `make fmt` | `ruff check --fix` + `ruff format` |
