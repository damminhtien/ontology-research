# ADR-0009: Event contract v2 — sequence, valid-time, upcasters

- Status: accepted
- Date: 2026-08-31
- Amends: [ADR-0002](ADR-0002-append-only-event-log.md) (mở rộng, không thay đổi nguyên tắc)

## Context

Log JSONL append-only của ADR-0002 đủ cho replay toàn phần, nhưng thiếu ba thứ
cần thiết khi dữ liệu thật lớn lên:

1. **Không có offset/sequence** → projector không checkpoint được, không phát
   hiện log bị truncate/tamper.
2. **Chỉ có một timestamp** (`occurred_at` = record-time) → không biểu diễn được
   fact hồi tố ("X ở Y tại thời điểm T" biết sau khi T đã qua).
3. **Đổi schema phải chọn giữa** break replay log cũ hoặc cấm luôn tiến hóa.

## Decision

Schema v2 (`foundry/events.py`):

- **`sequence: int`** — số 1-based do `EventLog` gán lúc append; replay bắt
  buộc record phải nằm đúng vị trí (`sequence discontinuity` = corruption).
- **`valid_at: str | None`** — valid-time (fact giữ trong thế giới thực);
  `None` = trùng record-time. Record-time vẫn là `occurred_at`.
- **Upcaster chain** (`UPCASTERS`): mỗi phiên bản schema đăng ký một hàm nâng
  lên đúng một phiên bản; `event_from_dict` nâng mọi record cũ lên hiện tại
  **khi đọc** — log không bị rewrite (nguyên tắc ADR-0002 giữ nguyên).

## Consequences

- (+) Checkpoint/streaming replay cho projector (workstream Phase 5) có offset
  chuẩn để neo vào.
- (+) Fact hồi tố và out-of-band data biểu diễn được mà không giả mạo record-time.
- (+) Log cũ (v1) vẫn replay được — đã test với log 23k events thật.
- (-) Mọi consumer đọc log phải đi qua `event_from_dict` (không đọc JSON thô).
- (-) Xóa một upcaster trong chuỗi = phá replay log đã ghi — được test canh.

## Amendment (2026-09-02)

- Event types hiện tại: `EntityCreated`, `LocationObserved`,
  `AffiliationAssessed`, `EntityMerged`
  ([ADR-0007](ADR-0007-under-merge-repair-via-append-only-merge-events.md)),
  `ExternalIdBound`
  ([ADR-0010](ADR-0010-registry-as-log-projection.md)).
- Số phiên bản hợp đồng dữ liệu không còn hardcode trong source: quản lý bằng
  file `VERSION` (single source of truth) + `CHANGELOG-DATA.md` (lịch sử + quy
  tắc bump) ở gốc repo; code đọc qua `foundry/versioning.py`. Bump = sửa VERSION
  + changelog entry + upcaster (nếu là event_schema) trong cùng commit.
