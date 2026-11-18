# Nạp OSINT quanh Việt Nam và Biển Đông

`tools/ingest_osint_region.py` chạy mười adapter theo vùng. Registry có 13
nguồn từ 13 đầu mối: ba nguồn tham chiếu/định danh chạy qua tool riêng và mười
nguồn khu vực bên dưới.

## Danh mục 13 nguồn

| Nguồn | Dữ liệu dùng trong MVP | Truy cập |
| --- | --- | --- |
| Wikidata | QID, tên, alias và crosswalk LEI có kiểm tra | Không cần key; `ingest_osint_sample.py` / `ingest_wikidata.py` |
| GLEIF | Legal Entity Identifier và tên pháp lý | Không cần key; `ingest_osint_sample.py` |
| Wikipedia tiếng Anh | Revision tài liệu và câu quan hệ công ty tường minh | Không cần key; `ingest_osint_sample.py` |
| [OpenSky Network](https://opensky-network.org/data/api) | Snapshot vị trí aircraft | Công khai; chạy `--source opensky` |
| [ADSB.lol](https://api.adsb.lol/docs) | Vị trí ADS-B trong vòng 250 hải lý quanh trung tâm Biển Đông | Không cần key cho feed công khai; chạy `--source adsb-lol` |
| [adsb.fi](https://github.com/adsbfi/opendata) | Vị trí ADS-B qua API v3 trong vòng 250 hải lý | API công khai; chạy `--source adsb-fi` |
| [GDELT 2.0 Events](https://www.gdeltproject.org/data.html#documentation) | Sự kiện báo chí đã mã hóa và ActionGeo | Công khai; chạy `--source gdelt` |
| [AISStream](https://aisstream.io/documentation) | PositionReport trực tiếp | Cần `AISSTREAM_API_KEY` |
| [OpenStreetMap Overpass](https://wiki.openstreetmap.org/wiki/Overpass_API) | Feature cảng/harbour có tag phù hợp | Công khai; chạy `--source openstreetmap` |
| [USGS Earthquake Catalog](https://earthquake.usgs.gov/fdsnws/event/1/) | Feed GeoJSON động đất tuần gần nhất | Công khai; chạy `--source usgs-earthquakes` |
| [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/api/area) | Phát hiện nhiệt VIIRS NOAA-20 | Cần `FIRMS_MAP_KEY` |
| [Global Fishing Watch](https://globalfishingwatch.org/our-apis/documentation/) | Fishing events trong polygon và cửa sổ ngày | Cần `GFW_API_ACCESS_TOKEN` và quyền dataset |
| [GDACS](https://www.gdacs.org/gdacsapi/swagger/index.html) | GeoJSON alert episodes được truy vấn theo polygon WKT | Public API, mặc định 4 ngày; chạy `--source gdacs` |

Đây là các đầu mối dữ liệu riêng, không khẳng định có cùng số phép đo độc lập.
OpenSky, ADSB.lol và adsb.fi đều phân phối trạng thái aircraft từ mạng thu ADS-B
nên có thể cùng báo cáo một máy bay hoặc dùng các đầu thu chồng lặp. AISStream
và Global Fishing Watch cũng có thể dựa trên AIS; GFW và GDACS là sản phẩm phân
tích tổng hợp. Giữ provider, thời gian và cách tạo dữ liệu trong provenance;
không xem các feed này là xác nhận độc lập của cùng một sự kiện.

## Vùng thu thập

AOI mặc định là hình chữ nhật từ 0° đến 25° vĩ Bắc và 98° đến 122° kinh Đông.
Đây là bộ lọc không gian, không phải biên giới quốc gia hay tuyên bố chủ quyền.
Mapper kiểm tra tọa độ lại sau khi nhận response.

## Cách dùng

Chạy mười adapter vùng có thể dùng trong môi trường hiện tại:

```bash
.venv/bin/python tools/ingest_osint_region.py \
  --source all \
  --max-records 200
```

Nguồn thiếu credential được ghi là `skipped`; các nguồn công khai vẫn chạy.
Đặt key trong environment nếu cần bật các nguồn tùy chọn:

```bash
export AISSTREAM_API_KEY='...'
export FIRMS_MAP_KEY='...'
export GFW_API_ACCESS_TOKEN='...'
```

Chạy một nguồn riêng bằng `--source opensky`, `adsb-lol`, `adsb-fi`, `gdelt`, `aisstream`,
`openstreetmap`, `usgs-earthquakes`, `nasa-firms` hoặc `global-fishing-watch`.
Có thể chọn thêm `gdacs` để lấy public event feed hiện tại.
GFW lấy cửa sổ 7 ngày, kết thúc trước ba ngày gần nhất; NASA FIRMS lấy một
ngày UTC đã hoàn tất. Cửa sổ ngày được ghim trong `run-config.json` để retry
cùng `--run-id` không đổi phạm vi. Dùng run ID mới để tạo snapshot mới.

AISStream cần WebSocket client tùy chọn:

```bash
.venv/bin/pip install -r requirements-osint.txt
```

## Semantics và giới hạn

- **OpenSky:** Chỉ vector hợp lệ, mới, có ICAO24 và `time_position` tạo
  assertion vị trí theo khoảng nửa mở một giây. ICAO24 được dùng làm định danh
  aircraft chung giữa các feed; callsign chỉ là metadata.
- **ADSB.lol / adsb.fi:** Mỗi adapter lấy một vòng tròn 250 hải lý quanh
  12.5°N, 110°E rồi lọc lại theo AOI. `seen_pos` (giây trước `now`, Unix ms)
  xác định valid time; thiếu thời gian, tọa độ hoặc ICAO24 hợp lệ thì bỏ qua.
  Cả ba feed dùng namespace `icao24`, vì [ICAO quy định địa chỉ Mode S 24-bit
  được phân bổ độc quyền cho từng aircraft](https://www.icao.int/WACAF/Documents/APIRG/APIRG%2026/EN/WPs-ENG/APIRG26%20WP03F12%20-%20App.%20Guidelines%20for%20ICAO%2024%20Bits%20Adresses%20Assignment.pdf);
  ICAO cũng lưu ý địa chỉ cũ có thể được trả lại khi đăng ký thay đổi.
  Nếu có bằng chứng tái cấp gây nhầm danh tính, sửa bằng event split có kiểm
  toán. Các feed vẫn không phải xác nhận độc lập của vị trí.
- **GDELT:** Tải export mới nhất theo `lastupdate.txt` và kiểm tra MD5. ActionGeo
  là vị trí do GDELT geocode; GlobalEventID/CAMEO/SOURCEURL được lưu thành
  source report, không phải sự kiện đã xác minh.
- **AISStream:** Frame hợp lệ trong AOI được lưu như document. Timestamp chỉ có
  giây trong phút nên không dựng `valid time` tuyệt đối. Feed live không có
  replay bền vững.
- **OpenStreetMap:** Chỉ lấy object gắn tag cảng/harbour đã chọn. Way/relation
  dùng tâm hình học do Overpass trả về. AOI được chia bốn tile, checkpoint từng
  tile; giới hạn `--max-records` có thể làm run kết thúc ở trạng thái `limited`
  trước khi quét hết vùng. Tag không chứng minh operator, ownership hoặc danh
  tính tổ chức.
- **USGS:** Weekly GeoJSON được lọc tại máy chạy. Magnitude, status, thời gian
  và tọa độ giữ nguyên là báo cáo catalog; không suy ra thiệt hại hay tác động.
- **NASA FIRMS:** Lưu từng dòng VIIRS có tọa độ trong AOI. Detected hotspot
  không tự xác nhận cháy tại mặt đất. Key chỉ xuất hiện trong request URL trong
  bộ nhớ; không ghi vào URI, fingerprint, log hay archive.
- **Global Fishing Watch:** Lưu fishing event ID, thời gian, vị trí và metadata
  tàu trong response. Event là AIS-derived model output, không phải ground
  truth hoặc bằng chứng identity. Token chỉ gửi qua `Authorization` header.
- **GDACS:** Lưu alert episode ID, loại sự kiện, mức cảnh báo, thời gian và tọa
  độ centroid do nhà cung cấp báo cáo. API lọc event theo giao cắt vùng; mapper
  chỉ nhận centroid trong AOI. Alert không xác nhận độc lập mức độ tác động.

OpenSky, ADSB.lol và adsb.fi tạo location assertion có source/time sau khi qua
kiểm tra vùng và freshness. Ba nguồn dùng chung namespace `icao24`, do đó snapshot
của cùng aircraft được gắn vào một canonical entity; source-record key vẫn giữ
riêng từng provider. Các nguồn báo cáo còn lại đăng ký `DocumentRegistered` và
được deduplicate theo source-record key cùng content hash. Chúng chưa được gắn
vào một entity domain khi mapper không cung cấp identifier đủ tin cậy; không
suy ra liên kết từ tên, tiêu đề, vị trí gần nhau hoặc nội dung tự do.

Event mới được ghi vào production event log chung, mặc định
`~/Data/ontology-research/data/production.jsonl`. Raw response, checkpoint,
`run-config.json` và `summary.json` vẫn nằm theo từng crawl run ngoài repo:

```text
~/Data/ontology-research/data/crawl-runs/osint-vietnam-scs/<run-id>/
├── checkpoints/
├── raw/
├── run-config.json
└── summary.json
```

Log từng run từ các lần crawl cũ được giữ nguyên làm dữ liệu lịch sử. Chạy
`.venv/bin/python tools/consolidate_osint_runs.py --dry-run` để kiểm tra trước,
sau đó bỏ `--dry-run` để replay chúng vào production log. Tool đọc lại snapshot
ADS-B đã archive để khôi phục các record từng bị đưa vào review do namespace cũ.
Manifest kết quả nằm ở crawl root, ngoài Git.

Mỗi nguồn có checkpoint riêng; page chỉ được đánh dấu hoàn tất sau khi raw
snapshot đã archive và mọi mapped document/assertion đã được commit vào event
log chung. Dữ liệu
chỉ là snapshot theo vùng và giới hạn response; không cam kết bao phủ toàn bộ
vận tải biển, cảng, cháy hoặc sự kiện an ninh.
