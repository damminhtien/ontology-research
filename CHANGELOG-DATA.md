# Data contract changelog

Nhật ký phiên bản của **hợp đồng dữ liệu** — độc lập với SemVer của ontology
modules (xem `docs/CHANGELOG.md`, sinh tự động từ registry). Số phiên bản hiện
hành nằm trong file `VERSION` ở gốc repo; source code đọc qua
`foundry/versioning.py` và **không bao giờ hardcode** số phiên bản.

## event_schema

### 2 — 2026-08-31

- Thêm `sequence` (offset 1-based do `EventLog` gán khi append; replay phát hiện
  discontinuity) và `valid_at` (valid-time, tách khỏi record-time `occurred_at`).
- Upcaster v1→v2 đăng ký trong `UPCASTERS` (`foundry/events.py`); log v1 vẫn
  replay được — ADR-0009.

### 1 — 2026-08-26

- Hợp đồng đầu tiên: `event_id`, `event_type`, `occurred_at`, `payload` — ADR-0002.

## lake

### 1 — 2026-08-30

- Parquet + zstd, partition theo `event_date` (Hive-style), manifest v1 với
  `lake_version` trong `manifest.json`.

## Quy tắc bump phiên bản

1. Sửa file `VERSION` (key tương ứng).
2. Thêm entry vào chính file này: ngày, nội dung thay đổi, ADR tham chiếu.
3. Riêng `event_schema`: đăng ký upcaster từ phiên trước trong `UPCASTERS`
   (`foundry/events.py`) + regression test đọc log phiên cũ — log đã ghi không
   bao giờ bị rewrite (ADR-0002).
4. `make check` phải xanh trong cùng commit.

## reference_lane

### 1 — 2026-09-11

- Lane snapshot đầu tiên: typed Parquet theo `class_qid` (cột `qid`, `name_vi`,
  `name_en`, `type_qids`, `class_qid`, `fetched_at`), manifest-authoritative
  (`snapshots[]` + `latest`), publish atomic qua tmp+rename — ADR-0002/
  architecture §4.6. Lane là reference data, rebuild được độc lập, không bao giờ
  là nguồn sự thật.
