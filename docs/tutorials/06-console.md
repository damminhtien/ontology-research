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

## Crawl Control — cấu hình và theo dõi ingest

Tab **Crawl Control** chạy lại pipeline regional hiện có qua
`tools/ingest_osint_region.py`; connector, mapper, checkpoint và raw archive
không bị thay thế. Có thể chọn một trong 10 nguồn regional hoặc chạy lần lượt
tất cả nguồn, rồi giới hạn số record mỗi nguồn và thời lượng AISStream.
Trang tự làm mới trạng thái, log và kết quả từng nguồn mỗi 5 giây.

Crawl root là `FOUNDRY_CRAWL_ROOT` nếu biến này được đặt; nếu không, service
dùng `$FOUNDRY_DATA_ROOT/data/crawl-runs`, với data root mặc định
`~/Data/ontology-research`. Cấu hình và log nằm trong
`<crawl-root>/crawler-service/`; raw snapshots nằm trong
`<crawl-root>/osint-vietnam-scs/`. Mỗi run có thư mục riêng và summary. Một
run đang chạy sẽ chặn lệnh start tiếp theo; có thể dừng từ tab này hoặc khi
console shutdown.

`GET /api/crawler/state` chỉ trả trạng thái, hợp đồng nguồn và cờ cho biết
credential đã cấu hình; nó không trả giá trị secret. `make console` tự tạo
`FOUNDRY_CONSOLE_TOKEN` ngẫu nhiên nếu chưa có, lưu file quyền `0600` tại
`$FOUNDRY_DATA_ROOT/secrets/console-token` (mặc định
`~/Data/ontology-research/secrets/console-token`) và chỉ bind vào loopback.
Crawler UI tự nhận phiên browser qua cookie `HttpOnly`; token không được gửi về
JavaScript. Có thể đặt `FOUNDRY_CONSOLE_TOKEN` trước khi chạy để dùng giá trị
quản lý sẵn. API vẫn nhận bearer token cho các client trực tiếp. Đặt
`AISSTREAM_API_KEY`, `FIRMS_MAP_KEY` và/hoặc `GFW_API_ACCESS_TOKEN` trong môi
trường của tiến trình console khi dùng các nguồn tương ứng. Log được lọc các
credential đã cấu hình trước khi ghi ra đĩa.

Trang Crawl Control hiển thị mapped records, event count, tốc độ quan sát,
thời lượng, exit code, trạng thái từng nguồn, số entity/document/position,
review/skipped, snapshot hash, loại event và log của từng run. Tổng mapped
records cộng theo nguồn; crawl lại có thể chứa record nguồn đã thấy trước đó.

Các connector hiện tại gọi API/feeds có cấu trúc và giữ mapping/provenance của
repo. Chưa thêm framework crawler HTML riêng; Scrapy phù hợp khi cần spider các
website, còn pipeline hiện tại không cần thay connector bằng nó.

## Admin — corrections có xác thực

Tab **Admin** hỗ trợ xem review queue, merge/split entities và chạy external-id
backfill. `make console` luôn cấu hình token cho local service; nếu chạy app
theo cách khác, phải cấu hình `FOUNDRY_CONSOLE_TOKEN` trước khi khởi động.
Merge và split ghi actor trong event;
backfill ghi `ExternalIdBound` events.

Các thao tác Admin mặc định đọc và ghi `$FOUNDRY_ADMIN_LOG`, mặc định là
`~/Data/ontology-research/data/production.jsonl`. Chúng không dùng event log
demo `~/Data/ontology-research/data/events.jsonl`. `data/` trong repo chỉ là
symlink tương thích ở local setup. Cấu hình PostgreSQL projector trong
[hướng dẫn vận hành](../guides/postgres-readmodel.md).
Chỉ dùng Console write operations khi đã trỏ tới log phù hợp.

Trong database mode, event monitor và operational projection cùng đọc production
log. Không cấu hình database thì console giữ demo log; `FOUNDRY_EVENT_LOG` ghi đè
log dùng cho monitor và local read-model scaffold.

## Projection — read model

- Cards read-model stats + projection lag
- Biểu đồ D3: p95 latency vs SLO (xanh/vàng theo ngưỡng)
- Bảng p50/p95/p99 đầy đủ từ `build/benchmark-report.json`
  (chạy `make benchmark` nếu chưa có)
- Operational query API gọi `QueryService` qua adapter PostgreSQL; xem
  [OSINT query contract](https://github.com/damminhtien/ontology-research/blob/main/requirements/osint_queries.md) để biết 10 query
  paths và các giới hạn.

## Khám phá REST API

Mọi view đều gọi REST endpoint — dùng trực tiếp được:

```bash
curl -s http://127.0.0.1:8787/api/projection
```

Response gồm trạng thái projection, số entity/location, projection lag và thời
điểm event gần nhất. Giá trị phụ thuộc event log và trạng thái runtime hiện tại.

```bash
curl -s 'http://127.0.0.1:8787/api/projection/lookup?name=Patrol+Vessel+01'
```

Kết quả có canonical ID, entity type và name của từng match; ID và danh sách
match thay đổi theo event log.

## Quyền xem provenance

Read API trả dữ liệu projection; provenance bị ẩn khi request không có token
được cấp. Cấu hình riêng `FOUNDRY_PROVENANCE_TOKENS` là
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

1. Theo [hướng dẫn PostgreSQL](../guides/postgres-readmodel.md), restart
   projector rồi xác nhận checkpoint và projection bắt kịp event log.
2. Dùng endpoint `/api/projection/entities/{id}/location/as-of?at=...` để hỏi
   "Vessel 01 ở đâu trước khi được quan sát lần đầu?"

## Kết quả

Console hiển thị trạng thái ontology, event log và read projection. Write
operations ghi event vào cùng event log; view không giữ một nguồn tri thức riêng.
