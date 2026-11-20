# Đánh giá chất lượng OSINT

## Phạm vi snapshot

Snapshot ngày 2026-11-18 dùng event log production có SHA-256 bắt đầu bằng
`7130f580`, 3.918 event, 1.244 source record từ ba nguồn và 1.243 canonical
entity. Một canonical entity có record từ nhiều nguồn. Bộ dữ liệu production hiện
tập trung vào máy bay; chưa có AIS record để đánh giá identity tàu.

Bộ thử tổ chức OSINT có ba entity Wikidata và một record GLEIF đã được liên kết.
Hai nguồn cùng chứa một LEI authoritative. Tên và aliases không khớp chính xác.
Resolver chỉ nhận tên trả về `REVIEW` với hai candidate, trong đó entity đúng nằm
trong top 5. Khi có LEI, source record được liên kết trực tiếp mà không cần đưa
hai candidate qua review. Đây là một ví dụ thực tế về việc identifier semantics
giảm review workload; một cặp không đại diện cho độ chính xác chung của resolver.

Gold pilot được tạo bởi `tools/build_domain_gold.py`. Dataset chỉ chứa fingerprint
đã băm và nhãn dựa trên identifier; không chứa tên hoặc ID thô. Có một positive,
không có negative và chưa có adjudication thủ công. Vì vậy pairwise precision,
recall, false-merge rate và Splink comparison trên domain OSINT chưa ước lượng
được. Không huấn luyện Splink trên một cặp.

Method benchmark FEBRL4 có cùng name-only features, cùng validation threshold
policy và review budget năm candidate mỗi source record:

| Phương pháp | Candidate recall | Recall@5 | Pairwise precision | Pairwise recall | False suggestions |
| --- | ---: | ---: | ---: | ---: | ---: |
| Resolver hiện tại | 0.927 | 0.821 | 0.636 | 0.574 | 328 |
| Splink rerank cùng candidates | 0.927 | 0.862 | 0.878 | 0.713 | 99 |
| Splink blocking riêng | 0.484 | 0.484 | 0.980 | 0.482 | 10 |

Đây là kết quả phương pháp trên FEBRL4, không phải kết quả đại diện cho tàu hoặc
tổ chức OSINT. Nguồn và protocol nằm trong `benchmarks/evaluations/reports/splink-v2.json`.

## Evidence và temporal invariants

Trong operational projection có 1.513 assertion. Tất cả có source, evidence,
locator, span object và recorded time; không assertion nào hiện có `exact_text`.
Không có khoảng valid time sai thứ tự, record thiếu citation hoặc cặp assertion
khác object có khoảng hiệu lực chồng lấn. Đây là kiểm tra lineage và cấu trúc;
chưa có nhãn người thẩm định để đo assertion correctness, evidence precision,
trích đoạn văn bản chính xác hoặc độ đúng của valid time so với nguồn gốc.

PostgreSQL và Oxigraph trả cùng kết quả trên ba probe thực tế: entity lookup,
as-of và provenance. Latency ở phần dưới có boundary khác nhau; chỉ so sánh tính
tương đương kết quả, không xếp hạng engine từ các con số đó.

## Hiệu năng ở snapshot hiện tại

API benchmark gửi 100 request cho mỗi resource ở concurrency 1, 8 và 32, qua
loopback HTTP đến khi đọc xong response body. Mọi request trả HTTP 200. Tại
concurrency 32, throughput chững lại tùy endpoint và p95 tăng so với concurrency
1. Ví dụ:

| API | p95, concurrency 1 | p95, concurrency 8 | p95, concurrency 32 |
| --- | ---: | ---: | ---: |
| Entity search | 23.5 ms | 44.6 ms | 176.6 ms |
| Entity profile | 17.6 ms | 34.4 ms | 134.7 ms |
| Assertion evidence | 4.3 ms | 10.3 ms | 42.2 ms |

Process peak RSS ở mức 103.9 MB trong lượt đo; service đã được warm từ các lượt đo
trước đó. Máy thử có 12
logical CPU và 64 GiB memory. Đây là local baseline trên 1.244 source record.

Với cùng production event log, Oxigraph build snapshot trong 0.91 giây, mở trong
3.4 ms, snapshot chiếm 22.6 MB và process peak RSS là 135.6 MB. Ba probe có
100 lần đo. PostgreSQL adapter p95 là 2.67 ms cho point lookup, 3.66 ms cho
as-of và 3.37 ms cho provenance. Oxigraph direct-query p95 tương ứng là 0.085,
0.237 và 0.271 ms. PostgreSQL số đo gồm QueryService, psycopg và row decoding;
Oxigraph gồm `Store.query` và materialize kết quả. Hai boundary khác nhau nên
latency không phải so sánh engine đối xứng.

Hai projection rebuild trên database PostgreSQL tạm mất 21.57 và 25.87 giây cho
3.918 event. Tiến trình mới xác minh checkpoint trong 144–148 ms; checkpoint
khớp sequence 3.918. REINDEX các bảng entity, external ID và assertion mất
22–28 ms, 13–14 ms và 19–20 ms. Database tạm có dung lượng 14.8 MB; chỉ số
này không phải dự báo dung lượng production ở quy mô lớn. Harness xóa database
tạm sau mỗi lần đo.

## Giới hạn kết luận

- Local crawl chưa có đủ record tàu hoặc tổ chức và chưa có bộ nhãn âm/dương do
  chuyên gia OSINT thẩm định.
- Evidence coverage đầy đủ không chứng minh assertion đúng hoặc citation hỗ trợ
  nội dung assertion.
- Replay, index và latency được đo ở 3.918 event; không ngoại suy tới một triệu
  record, concurrency production hoặc recovery sau lỗi phần cứng.
- Kết quả hiện tại cho thấy semantic crosswalk giúp xử lý một trường hợp tên khác
  nhau và cung cấp baseline overhead. Chưa đủ dữ liệu để kết luận mức cải thiện
  chất lượng tổng thể.

## Tái lập

Các report sinh tự động, dataset pilot và Oxigraph snapshot được lưu ngoài Git:
`$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/`. File quality report ghi SHA-256
của mọi input và chứa số liệu không gồm tên hoặc identifier thô.

```sh
export FOUNDRY_DATA_ROOT=/Users/macbook/Data/ontology-research
export FOUNDRY_DATABASE_URL=postgresql:///ontology_research

.venv/bin/python tools/build_domain_gold.py \
  --event-log "$FOUNDRY_DATA_ROOT/data/crawl-runs/osint-mvp/osint-mvp-20261118-live/events.jsonl" \
  --output "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/osint-organization-crosswalk-pilot-v1.json"

.venv/bin/python tools/benchmark_api.py \
  --base-url http://127.0.0.1:8787 \
  --event-log "$FOUNDRY_DATA_ROOT/data/production.jsonl" \
  --requests 100 --concurrency 1,8,32 \
  --output "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/api-boundary-v1.json"

make query-adapter-eval QUERY_ADAPTER_ARGS="--event-log $FOUNDRY_DATA_ROOT/data/production.jsonl --snapshot-root $FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/oxigraph --runs 100 --output $FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/query-adapters-v1.json"

.venv/bin/python tools/benchmark_postgres_replay.py \
  --event-log "$FOUNDRY_DATA_ROOT/data/production.jsonl" \
  --output "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/postgres-replay-v1.json"

.venv/bin/python tools/assemble_osint_quality_report.py \
  --event-log "$FOUNDRY_DATA_ROOT/data/production.jsonl" \
  --organization-event-log "$FOUNDRY_DATA_ROOT/data/crawl-runs/osint-mvp/osint-mvp-20261118-live/events.jsonl" \
  --database-url "$FOUNDRY_DATABASE_URL" \
  --gold-report "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/osint-organization-crosswalk-pilot-v1.json" \
  --api-report "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/api-boundary-v1.json" \
  --adapter-report "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/query-adapters-v1.json" \
  --replay-report "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/postgres-replay-v1.json" \
  --output "$FOUNDRY_DATA_ROOT/evaluations/osint-quality-v1/quality-report-v1.json"
```

`benchmark_api.py` requires the local console to be running. `benchmark_postgres_replay.py`
creates and drops a uniquely named temporary database; the PostgreSQL role must have
permission to create and drop databases. `make splink-eval` reruns the separate FEBRL4
method benchmark.
