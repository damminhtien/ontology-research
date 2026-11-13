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
| `migrate MODULE` | Sinh SPARQL Update migration cho thay đổi breaking |
| `align add SOURCE TARGET --source-type TYPE --target-type TYPE [--mode {reference,export}] [--relation REL] [--predicate IRI] [--evidence IRI] [--verified-identity] [--note TEXT]` | Ghi alignment đã được kiểm tra kiểu; mapping RDF cần predicate và evidence |
| `align list` / `align check` | Liệt kê hoặc validate alignment registry |
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
| `.venv/bin/python tools/benchmark_e2e.py [--entities N] [--iterations N]` | E2E benchmark theo stage (ingest qua SHACL → lake → projector → lake query) → `build/benchmark-e2e-report.json` |
| `.venv/bin/python tools/benchmark_e2e.py --check` | Gate e2e: floor tuyệt đối từng stage + 1/1.2× baseline (`benchmarks/baseline-e2e.json`) |
| `.venv/bin/python tools/benchmark_e2e.py --generate-baseline` | Cập nhật baseline e2e (cùng `--entities` với gate) |

## Data plane

| Lệnh | Chức năng |
|------|-----------|
| `.venv/bin/python tools/seed_console_data.py [--force]` | Seed event log demo qua ingestion pipeline thật |
| `.venv/bin/python tools/ingest_wikidata.py [--class QID] [--limit N] [--timeout S] [--from-lane] [--log PATH] [--lake PATH] [--no-lake]` | Nạp entity thật từ Wikidata: mặc định fetch SPARQL trực tiếp, hoặc `--from-lane` đọc snapshot Parquet từ reference lane. Registry derive từ log nên chạy lặp lại là dedupe |
| `.venv/bin/python tools/ingest_tracking_feed.py --feed feed.jsonl [--log PATH] [--lake PATH]` | Nạp feed AIS JSONL vào tracking vertical: platform resolve theo MMSI, sensor theo serial, observation/track qua domain SHACL |
| `.venv/bin/python tools/build_reference_lane.py --class QID [--pages N]` | Fetch Wikidata (cursor paging theo QID) → snapshot typed Parquet versioned trong reference lane |
| `.venv/bin/python tools/measure_wikidata.py [--counts QID,...] [--batches QID:LIMIT,...] [--delay S] [--out PATH]` | Đo số lượng / kích thước batch Wikidata; cần network tới Wikidata |
| `.venv/bin/python tools/merge_entities.py --survivor ID --duplicate ID [--reason TEXT] [--log PATH] [--lake PATH]` | Repair under-merge: append `EntityMerged` sau khi validate trên registry rebuild từ log; exit 1 không ghi gì nếu bị từ chối |
| `.venv/bin/python tools/split_entity.py --survivor ID --duplicate ID [--reason TEXT]` | Undo một merge đã ghi: restore set đọc từ `EntityMerged`; append `EntitySplit` |
| `.venv/bin/python tools/backfill_external_ids.py [--log PATH] [--no-lake]` | Khôi phục QID binding của log cũ (quy ước `source_id: wikidata:QID`) thành `ExternalIdBound` events; idempotent |
| `.venv/bin/python tools/import_events.py --src PATH --dst PATH [--skip N] [--dry-run]` | Copy events giữa hai log, re-stamp sequence theo log nhận (event_id giữ nguyên) |
| `.venv/bin/python tools/export_rdf.py --event-log PATH --profile {canonical-v1,interop-v1} --output PATH [--geometry CSV]` | Export offline event log thành TriG; xem [RDF export profiles](rdf-export.md) |
| `make oxigraph-test` | Kiểm tra atomic publication, read-only reopen và full rebuild trong môi trường riêng |
| `make oxigraph-eval` | So sánh 41 CQ và đo Oxigraph/TDB2 ở 10K, 100K entity; xem [Oxigraph snapshot evaluation](oxigraph-evaluation.md) |

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
| `make conformance` | ROBOT/HermiT, Jena/pySHACL, 41 CQ và W3C RDFC-1.0; chạy độc lập với `make check` |
| `make fmt` | `ruff check --fix` + `ruff format` |
