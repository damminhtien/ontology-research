# Tutorial 06 — Console UI

!!! info "Thời lượng ước tính"
    15 phút. Nên hoàn thành các tutorial 03–05 trước.

## Khởi động

```bash
make seed-console   # dữ liệu demo (bỏ qua nếu đã chạy)
make console        # uvicorn tại :8787
```

Mở <http://127.0.0.1:8787>. Console là SPA vanilla JS, không cần build chain.
Các màn hình monitor đọc projection và trạng thái từ logic trong `foundry/` và
`tools/`; tab Admin có các thao tác ghi được bảo vệ bằng bearer token.

## Dashboard — health tổng quan

6 health cards:

- **Version check** — PASS/FAIL từ SemVer registry
- **Dependency DAG** — có vòng lặp hay import ngược chiều không
- **CQ regression** — 41 executable competency queries pass/fail
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
- SHACL validation gate trên ontology modules và benchmark datasets, qua mọi shape file
- Competency queries: pass/fail từng query

## Admin — corrections có xác thực

Tab **Admin** hỗ trợ xem review queue, merge/split entities và chạy external-id
backfill. Trước khi khởi động Console, cấu hình `FOUNDRY_CONSOLE_TOKEN`; nếu
chưa có token thì admin API trả `503`. Merge và split ghi actor trong event;
backfill ghi `ExternalIdBound` events.

Các thao tác Admin mặc định đọc và ghi `$FOUNDRY_ADMIN_LOG`, mặc định là
`data/production.jsonl`. Chúng không dùng event log demo `data/events.jsonl`.
Chỉ dùng Console write operations khi đã trỏ tới log phù hợp.

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

Response fields include projection existence, entity/location counts, lag and
the most recent event time. Values depend on the configured event log and
current runtime state.

```bash
curl -s 'http://127.0.0.1:8787/api/projection/lookup?name=Patrol+Vessel+01'
```

Kết quả có canonical ID, entity type và name của từng match; ID và danh sách
match thay đổi theo event log.

## Quyền xem provenance

Read API vẫn cho phép query dữ liệu projection công khai; provenance bị ẩn nếu
request không có token được cấp. Cấu hình riêng `FOUNDRY_PROVENANCE_TOKENS` là
JSON map từ bearer token tới source IDs được xem:

```bash
export FOUNDRY_PROVENANCE_TOKENS='{"reader-token":["urn:doc:123"]}'
curl -H 'Authorization: Bearer reader-token' \
  'http://127.0.0.1:8787/api/projection/entities/urn:entity:123'
```

Mỗi query chỉ trả source IDs nằm trong scope. Giá trị `"*"` cấp toàn bộ
provenance cho token đó. `/api/monitor/events/recent` áp dụng cùng scope và
không trả sự kiện `DocumentRegistered` của tài liệu ngoài scope. Token không
hợp lệ trả `401`; JSON cấu hình sai trả `503`. Đây là token đọc provenance,
tách biệt với `FOUNDRY_CONSOLE_TOKEN` cho Admin.

Swagger đầy đủ tại <http://127.0.0.1:8787/api/docs>.

!!! warning "Bảo mật"
    Các view monitor không cần đăng nhập; provenance chỉ hiển thị với bearer
    token có scope trong `FOUNDRY_PROVENANCE_TOKENS`. Admin operations cần
    `FOUNDRY_CONSOLE_TOKEN` và mặc định ghi vào production log. Bảo vệ cả hai
    loại token như secret và trỏ `$FOUNDRY_ADMIN_LOG` tới log phù hợp trước khi ghi.

## Bài tập

1. Trong tab Projection, điều gì xảy ra nếu xóa log mà `$FOUNDRY_EVENT_LOG`
   trỏ tới rồi reload?
   (Gợi ý: read model rebuild từ log — log mất thì read model mất.)
2. Dùng endpoint `/api/projection/entities/{id}/location/as-of?at=...` để hỏi
   "Vessel 01 ở đâu trước khi được quan sát lần đầu?"

## Điều gì vừa xảy ra

Bạn đã đi qua toàn bộ console — từ health monitoring đến query read model — và thấy
mọi thứ là projection của cùng một logic đã test. Đó là nguyên tắc thiết kế:
**UI không có nguồn chân lý riêng**.
