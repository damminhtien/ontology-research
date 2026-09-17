# Data & correction workflows

Các luồng vận hành thực tế trên production log (`data/production.jsonl`). Mỗi
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
                                          data/production.jsonl  (write model)
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

Chạy lại bước 2 bao nhiêu lần cũng an toàn: registry derive từ log nên
re-ingest là external-id hit (dedupe, không tạo entity mới).

## 2. Nạp feed tracking (sensor → observation → track)

Feed AIS JSONL (`mmsi`, `name`, `sensor_id`, `track_id`, `location_uri`,
`at_time`):

```bash
.venv/bin/python tools/ingest_tracking_feed.py --feed data/tracking-feed.jsonl
```

Mapping: platform resolve/mint theo **MMSI** (trusted external id), sensor
theo serial; ba event types `SensorRegistered` / `ObservationRecorded` /
`TrackObserved` đều qua domain SHACL contracts trước khi vào log. Read model
giữ track store (`get_track` / `tracks_of`).

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

Cam kết an toàn: candidate **không bao giờ tự mint entity** — subject phải
resolve exact alias/external id, nếu không thì vào durable review queue;
confidence bị cap 0.7; provenance là document id; mọi assertion qua SHACL.

## 4. Sửa dữ liệu: merge / split / backfill

| Tình huống | Công cụ | Event |
|---|---|---|
| Hai canonical entity = một thực thể thật (under-merge, ADR-0006) | `tools/merge_entities.py --survivor ID --duplicate ID` | `EntityMerged` |
| Merge sai cần undo | `tools/split_entity.py --survivor ID --duplicate ID` | `EntitySplit` (restore set đọc từ `EntityMerged` — split không có merge record bị reject) |
| QID binding khuyết trên log cũ (trước khi payload có `external_ids`) | `tools/backfill_external_ids.py` | `ExternalIdBound` (idempotent) |
| Đưa events từ log khác vào production log | `tools/import_events.py --src … --dst …` | re-stamp sequence theo log nhận |

Bề mặt review queue: mọi rejection identity-stage được ghi thành
`ResolutionReviewQueued` — đọc được, replay được, không mất khi process chết.
Drain queue bằng cách resolve reference rồi merge/import theo receipt.

## 5. Kiểm tra sức khoẻ sau mọi thao tác

```bash
make check   # lint + versions + SHACL(3 shapes) + DAG + tests + 2 SLO gates
```

Log luôn tự validate toàn bộ (sequence discontinuity = corruption); lake
manifest-authoritative (`verify()`); read model snapshot là cache — corrupt
thì tự full replay, không bao giờ là nguồn sự thật.
