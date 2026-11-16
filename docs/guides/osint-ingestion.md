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
identifier rules, mapping và quality checks của từng nguồn. Hiện có ba adapter:

| Nguồn | Identifier và mapping | Freshness |
| --- | --- | --- |
| Wikidata | QID là identity của source record. P31 dùng allowlist để chọn loại Organization. P1278 tạo crosswalk LEI sau khi kiểm tra checksum và collision. Tên/alias chỉ tạo lookup candidate. | Fetch theo yêu cầu; lưu entity IDs/version trong raw snapshot. |
| GLEIF | LEI hợp lệ là identifier authoritative trong namespace `gleif-lei`. Legal name và other names là label/alias, không phải bằng chứng merge. | API trả Golden Copy publication time; refresh bằng run mới. |
| Wikipedia tiếng Anh | Page ID xác định document; revision ID và timestamp xác định version. Chỉ ánh xạ câu tường minh `subsidiary of` thành `organization:subsidiaryOf`; subject phải khớp pageprops Wikidata QID, còn object phải resolve bằng alias chính xác và duy nhất. | Lấy revision mới nhất và giữ revision metadata. |

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
