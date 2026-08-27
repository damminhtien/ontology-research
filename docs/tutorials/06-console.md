# Tutorial 06 — Console UI

!!! info "Thời lượng ước tính"
    15 phút. Nên hoàn thành các tutorial 03–05 trước.

## Khởi động

```bash
make seed-console   # dữ liệu demo (bỏ qua nếu đã chạy)
make console        # uvicorn tại :8787
```

Mở <http://127.0.0.1:8787>. Console là SPA read-only (vanilla JS, không build
chain) — mọi số liệu đi thẳng từ logic tools/foundry đã test, không có nguồn dữ
liệu riêng.

## Dashboard — health tổng quan

6 health cards:

- **Version check** — PASS/FAIL từ SemVer registry
- **Dependency DAG** — có vòng lặp hay import ngược chiều không
- **CQ regression** — 6 competency queries pass/fail
- **Worst stability** — module kém ổn định nhất
- **Projection** — số entities + projection lag so SLO 5s
- **Events logged** — tổng events theo type

Bên dưới: bảng modules (version, classes/props, last release) và bảng stability
với meter bars.

## Explorer — duyệt ontology

- Cây hierarchy D3 collapsible (Entity → Agent/PhysicalObject/Event…)
- Click node → panel chi tiết: comment, parents, properties (kind + range),
  blast radius badge
- Ô search (`/` để focus) lọc class và property realtime

## Versions — governance trực quan

- **Pending release**: changes chưa release + suggested bump + badge status
- **Diff**: chọn 2 phiên bản đã release → bảng changes màu theo severity
- **Release history**: timeline với migration notes
- Nút "Copy CLI command" — ghi release vẫn qua CLI (có review), không qua UI

## Impact — blast radius theo term

Gõ tên term (autocomplete từ model) → BR score + danh sách modules/queries/
applications bị ảnh hưởng.

## Data Monitor — data plane

- Event log: stats theo type + 20 events gần nhất
- SHACL validation gate trên seed data
- Competency queries: pass/fail từng query

## Projection — read model

- Cards read-model stats + projection lag
- Biểu đồ D3: p95 latency vs SLO (xanh/vàng theo ngưỡng)
- Bảng p50/p95/p99 đầy đủ từ `build/benchmark-report.json`
  (chạy `make benchmark` nếu chưa có)

## Khám phá REST API

Mọi view đều gọi REST endpoint — dùng trực tiếp được:

```bash
curl -s http://127.0.0.1:8787/api/projection
```

```json
{
  "exists": true,
  "slo_lag_seconds": 5,
  "within_slo": true,
  "entities": 3,
  "with_location": 1,
  "locations": 1,
  "last_event_time": "2026-08-27T03:29:43Z",
  "lag_seconds": 4.0
}
```

```bash
curl -s 'http://127.0.0.1:8787/api/projection/lookup?name=Patrol+Vessel+01'
```

```json
{
  "query": "Patrol Vessel 01",
  "matches": [
    {
      "entity_id": "urn:world:entity:f073683a41e646ccb51998ab68eaa9bb",
      "entity_type": "Platform",
      "name": "Patrol Vessel 01"
    }
  ]
}
```

Swagger đầy đủ tại <http://127.0.0.1:8787/api/docs>.

!!! warning "Bảo mật"
    Console v0.1 bind localhost, không auth — admin tool nội bộ. Write
    operations (release) có auth + audit trail sẽ đến ở Phase 3+.

## Bài tập

1. Trong tab Projection, điều gì xảy ra nếu xóa `data/events.jsonl` rồi reload?
   (Gợi ý: read model rebuild từ log — log mất thì read model mất.)
2. Dùng endpoint `/api/projection/entities/{id}/location/as-of?at=...` để hỏi
   "Vessel 01 ở đâu trước khi được quan sát lần đầu?"

## Điều gì vừa xảy ra

Bạn đã đi qua toàn bộ console — từ health monitoring đến query read model — và thấy
mọi thứ là projection của cùng một logic đã test. Đó là nguyên tắc thiết kế:
**UI không có nguồn chân lý riêng**.
