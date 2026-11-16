# Data & correction workflows

Các luồng vận hành thực tế trên production log
(`~/Data/ontology-research/data/production.jsonl`). Mỗi
workflow dùng đúng công cụ của nó; không workflow nào sửa log in-place
(ADR-0002).

## Sơ đồ tổng quan

```text
   NGUỒN DỮ LIỆU                      LOG PRODUCTION
─────────────────────           ─────────────────────────────────────
Wikidata SPARQL ──► build_reference_lane.py ─► reference/ (typed Parquet)
                       │                             │ ingest_wikidata.py --from-lane
  AIS JSONL feed ──────┼──► ingest_tracking_feed.py ─┤
  Báo cáo văn bản ─────┘      (pipeline.ingest_document)│
                                                        ▼
                                          ~/Data/ontology-research/data/production.jsonl
                                                                (write model)
                                                        │
   CORRECTIONS                                          ▼
   ────────────────────            merge_entities.py ─► EntityMerged
   duplicate entities ────────────► split_entity.py ────► EntitySplit
   QID binding khuyết ────────────► backfill_external_ids.py ► ExternalIdBound
   import giữa hai log ───────────► import_events.py ────► re-stamp sequence
                                                        │
                                    ┌───────────────────┴──────────┐
                                    ▼                              ▼
                              read model (Console)            lake (OLAP)
```

## 1. Nạp dữ liệu tham chiếu: reference lane trước, ingestion sau

Wikidata là **reference data**, không phải fact vận hành. Tách hai bước:

```bash
# 1. fetch snapshot typed Parquet (cursor paging theo QID — không mất item
#    qua trần LIMIT, snapshot versioned + manifest authoritative)
.venv/bin/python tools/build_reference_lane.py --class Q3918 --pages 3

# 2. ingest TỪ lane (không mạng, vẫn qua identity + SHACL + review gate)
.venv/bin/python tools/ingest_wikidata.py --from-lane --class Q3918
```

Chạy lại bước 2 an toàn. Nếu PostgreSQL chưa được cấu hình, ingestion dựng
registry từ log. Khi dùng PostgreSQL, projector phải theo kịp event log trước
khi ingest. Khóa ổn định của source record và external ID giúp nhận diện dữ
liệu đã nạp.

## 2. Nạp feed tracking (sensor → observation → track)

Feed AIS JSONL (`mmsi`, `name`, `sensor_id`, `track_id`, `position_uri`,
`measurements`, `at_time`; `location_uri` cũ vẫn được chấp nhận làm bí danh đầu vào):

```bash
.venv/bin/python tools/ingest_tracking_feed.py --feed data/tracking-feed.jsonl
```

Mapping: platform được resolve hoặc tạo mới theo **MMSI** (external ID có thẩm
quyền), sensor theo serial. `ObservationRecorded` ghi sự kiện quan sát;
`tracking:ObservationResult` riêng lưu vị trí, giá trị có kiểu dữ liệu, đơn vị
và evidence. Event và result không tự tạo khẳng định thế giới
`core:locatedAt`. `SensorRegistered`, `ObservationRecorded` và `TrackObserved`
phải qua SHACL trước khi ghi vào log. Read model lưu track, truy cập qua
`get_track` và `tracks_of`.

## 3. Trích xuất fact từ văn bản (LLM chỉ đề xuất)

```python
from foundry.ingestion import IngestionPipeline
from foundry.extraction import PatternExtractor, LlmExtractor

# mặc định: deterministic PatternExtractor (mẫu báo cáo VI/EN, không cần LLM)
result = pipeline.ingest_document(
    uri="https://example.org/report-7",
    title="Báo cáo tuần 37",
    source_system="crawler",
    text="Cảnh sát biển Việt Nam đặt tại Đà Nẵng từ 2026-05-01.",
)

# LLM backend: inject completion callable (text -> JSON candidate list);
# đổi vendor không đụng pipeline
from foundry.extraction import LlmExtractor

result = pipeline.ingest_document(..., extractor=LlmExtractor(complete=my_llm_fn))
```

Candidate không tự mint entity. Subject phải resolve qua identity evidence;
trường hợp chưa resolve được giữ unresolved reference hoặc vào durable review
queue khi ambiguous. Extractor lưu `model_score` tối đa 0.7 và provenance tới
source/document; score không phải xác suất fact đúng. Predicate phải được khai
báo và tương thích với subject/object; mọi assertion qua SHACL.

## 4. Sửa dữ liệu: merge / split / backfill

| Tình huống | Công cụ | Event |
|---|---|---|
| Hai canonical entity = một thực thể thật (under-merge, ADR-0006) | `tools/merge_entities.py --survivor ID --duplicate ID` cùng `--evidence-id`, `--evidence-source`, `--evidence-basis`, `--recorded-by` | `EntityMerged` |
| Merge sai cần undo | `tools/split_entity.py --survivor ID --duplicate ID` | `EntitySplit` (restore set đọc từ `EntityMerged` — split không có merge record bị reject) |
| QID binding khuyết trên log cũ (trước khi payload có `external_ids`) | `tools/backfill_external_ids.py` | `ExternalIdBound` (idempotent) |
| Đưa events từ log khác vào production log | `tools/import_events.py --src … --dst …` | re-stamp sequence theo log nhận |

Bề mặt review queue: mọi rejection identity-stage được ghi thành
`ResolutionReviewQueued` — đọc được, replay được, không mất khi process chết.
Xử lý queue bằng cách resolve reference rồi thực hiện merge/import theo receipt;
`POST /api/admin/review` chỉ liệt kê item, không xóa event khỏi append-only log.
Khi dùng PostgreSQL, projector phải theo kịp event log trước khi chạy merge,
split hoặc ingestion; correction CLI dùng persistent identity index.

## 5. Kiểm tra sức khoẻ sau mọi thao tác

```bash
make check   # lint + versions + alignment + SHACL(4 files) + DAG + tests + 2 SLO gates
```

Log tự validate toàn bộ (sequence discontinuity = corruption); lake dùng manifest
để xác định các file đã commit (`verify()`). PostgreSQL read model có thể được
rebuild từ event log; `--rebuild` xóa projection và checkpoint trong database,
giữ nguyên event log và lake.

## 6. Đổi ontology và chuẩn bị migration

Core giữ các khái niệm cross-domain ổn định. Thuật ngữ domain thuộc module
versioned hiện có; tạo domain pack mới cần một quyết định riêng.

```bash
# Diff hai schema: SemVer severity kèm SAFE / COMPATIBLE / BREAKING
.venv/bin/python tools/manage_ontology.py diff old-core.ttl new-core.ttl

# Với breaking change, scan toàn bộ nguồn cũ có thẩm quyền trước khi tạo script
.venv/bin/python tools/manage_ontology.py migrate core \
    --data "$HOME/Data/ontology-research/data/production.jsonl" \
    --data "$HOME/Data/ontology-research/data/lake"
```

`migrate` tạo SPARQL Update và file `.impact.json`. Report nêu số data item nhắc tới
term đổi cùng các consumer được tìm trong ontology, SHACL, event contract,
Console/API, projector/read model, lake và benchmark queries. `--data` nhận RDF,
JSON/JSONL, Parquet hoặc thư mục lake; thư mục có `manifest.json` chỉ scan các
Parquet file đã đăng ký. Truyền mỗi nguồn authoritative bằng một cờ. Thiếu nguồn
thì preflight dừng. Count unit là RDF triple, JSON item, JSONL record hoặc
Parquet row. Khi ghi MAJOR release cùng script, lệnh yêu cầu report khớp
module/phiên bản, có lịch sử data đã scan và SHA-256 khớp script hiện tại. Các
kết quả consumer là lexical leads để review, còn data match không thay SHACL
hay application validation. Chỉ áp dụng migration sau khi report và script đã
được xem xét.
