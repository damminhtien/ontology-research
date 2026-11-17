# OSINT source adapters

Các adapter tách phần tải dữ liệu khỏi phần ánh xạ domain:

```text
Connector → RawArchive → DomainMapper → IngestionPipeline → EventLog
                                               ↓
                                     Projector → QueryService
```

Connector chỉ lấy response và trả nguyên bytes cùng source URI, schema version,
record key và thời điểm lấy. Domain mapper chạy thuần, tạo entity/document
commands theo contract canonical. Raw response được lưu content-addressed trước
khi mapping; checkpoint chỉ tiến sau khi toàn bộ command của page đã được commit
hoặc đưa bền vững vào review queue. Nếu xử lý dở, lần chạy lại tải lại page;
idempotency của event log ngăn ghi lặp.

## Nguồn được khai báo

`config/source_registry.json` mô tả schema/API, quyền lưu và chia sẻ, freshness,
identifier rules, mapping và quality checks của từng nguồn:

| Nguồn | Identifier và mapping | Freshness |
| --- | --- | --- |
| Wikidata | QID là identity của source record. P31 dùng allowlist để chọn loại Organization. P1278 tạo crosswalk LEI sau khi kiểm tra checksum và collision. Tên/alias chỉ tạo lookup candidate. | Fetch theo yêu cầu; lưu entity IDs/version trong raw snapshot. |
| GLEIF | LEI hợp lệ là identifier authoritative trong namespace `gleif-lei`. Legal name và other names là label/alias, không phải bằng chứng merge. | API trả Golden Copy publication time; refresh bằng run mới. |
| Wikipedia tiếng Anh | Page ID xác định document; revision ID và timestamp xác định version. Chỉ ánh xạ câu tường minh `subsidiary of` thành `organization:subsidiaryOf`; subject phải khớp pageprops Wikidata QID, còn object phải resolve bằng alias chính xác và duy nhất. | Lấy revision mới nhất và giữ revision metadata. |
| OpenSky | State vector chỉ tạo vị trí assertion khi có ICAO24, tọa độ và `time_position` hợp lệ. ICAO24 thuộc source record OpenSky; callsign không dùng làm identity evidence. | Một state snapshot cho mỗi run; bỏ điểm quá cũ hoặc ngoài AOI. |
| ADSB.lol | Readsb position có ICAO24 và `seen_pos`; ICAO24 namespace riêng, callsign/registration chỉ là metadata. Không dùng làm cross-source merge evidence. | Một snapshot 250 NM quanh 12.5°N, 110°E; lọc lại theo AOI và tuổi tối đa một giờ. |
| adsb.fi | Readsb position từ API v3; ICAO24 namespace riêng, không tự nhập với aircraft từ OpenSky/ADSB.lol. | Một snapshot 250 NM quanh 12.5°N, 110°E; tuân thủ giới hạn công khai một request/giây. |
| AISStream | MMSI là identifier có thể được tái sử dụng. Position reports được lưu nguyên nguồn; timestamp giây trong phút không thành valid time. | Cửa sổ live có giới hạn; cần `AISSTREAM_API_KEY`. |
| GDELT Events | Giữ GlobalEventID, ngày, CAMEO và ActionGeo như metadata báo cáo. Không xem GDELT row hoặc URL bài báo là fact đã kiểm chứng. | Lấy export mới nhất, xác nhận MD5 rồi lọc theo vùng. |
| OpenStreetMap | Object cảng/harbour được chọn theo tag, tọa độ và OSM type/ID. Không suy ra ownership hay operator. | Overpass quét bốn tile với checkpoint; `max_records` có thể dừng trước khi hết vùng. |
| USGS Earthquake Catalog | Feature GeoJSON cùng magnitude, origin time, depth và status do catalog báo cáo. Không suy ra thiệt hại. | Feed tuần mới nhất; mapper lọc tại máy chạy theo AOI. |
| NASA FIRMS | VIIRS hotspot rows có ngày/giờ, confidence và tọa độ nguồn. Không xem detection là xác nhận cháy mặt đất. | Một UTC day; cần `FIRMS_MAP_KEY`, key được loại khỏi archive và logs. |
| Global Fishing Watch | Event id, type, thời gian, vị trí và vessel metadata; đây là kết quả phân tích hoạt động AIS-derived. | Cửa sổ ngày được ghim theo run; cần `GFW_API_ACCESS_TOKEN` và quyền dataset. |
| GDACS | Feature GeoJSON cho alert episode, loại sự kiện, alert level và vị trí do GDACS tổng hợp. | Public API truy vấn theo polygon WKT trong 4 ngày gần nhất; có thể cấu hình tối đa 30 ngày. |

Registry có mười ba nhà cung cấp/đầu mối riêng tính cả Wikidata, GLEIF và
Wikipedia. Chúng không tương đương mười ba tín hiệu độc lập; các feed ADS-B
có thể chồng lặp nhau, AISStream và Global Fishing Watch có thể dùng dữ liệu
AIS chồng lặp. Xem [ingestion theo vùng
Việt Nam và Biển Đông](../osint-vietnam-scs.md) để biết cách chạy, time window
và giới hạn của từng adapter.

Wikidata và GLEIF có thể tự động hội tụ khi một QID mang P1278 LEI hợp lệ và LEI
đó chưa thuộc entity khác. Nếu LEI đã có owner khác, mapper giữ nguyên cả hai
identity và đưa crosswalk vào review queue. Tên trùng nhau không tạo merge.

Với câu tài liệu ở thì hiện tại, revision timestamp là mốc valid-time sớm nhất
mà source snapshot đang hỗ trợ. `recorded_at` vẫn là thời điểm assertion được
ghi vào event log. Span trích xuất lưu exact text; locator trỏ về trang nguồn.

## Chạy mẫu dữ liệu nhỏ

Lệnh sau lấy ba entity Wikidata, một LEI record từ GLEIF và bài “BBC Studios”
từ Wikipedia; không dùng production event log hoặc database:

```bash
.venv/bin/python tools/ingest_osint_sample.py --run-id osint-mvp-001
```

Mặc định, mọi output nằm tại:

```text
~/Data/ontology-research/data/crawl-runs/osint-mvp/osint-mvp-001/
├── checkpoints/
├── events.jsonl
└── raw/
```

Tool xác nhận Microsoft từ Wikidata Q2283 và LEI GLEIF `INR2EJN1ERAN0W5ZP974`
resolve tới cùng canonical ID. Bài Wikipedia đăng ký thành `DocumentRegistered`;
câu về quan hệ công ty được đưa qua extraction/SHACL, rồi `QueryService` đọc
assertion provenance sau khi `Projector` chạy. JSON cuối lệnh ghi revision,
content hash, canonical ID, assertion ID, valid/recorded time và evidence span.

Muốn chạy lại từ đầu, dùng `--run-id` mới. Giữ run artifacts ngoài Git; chúng
chứa dữ liệu tải về, không phải fixture của source tree. AIS mật độ cao không
nằm trong sample này.

## OSINT quanh Việt Nam và Biển Đông

Adapter theo vùng không ghi vào production event log hoặc FastAPI. Raw
snapshots, event log và checkpoints tiếp tục được giữ ngoài Git, trong thư mục
crawl riêng.
