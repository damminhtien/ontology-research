# ADR-0010: Registry là projection của log — pure lookup, multimap, type-aware

- Status: accepted
- Date: 2026-09-02
- Amends: [ADR-0003](ADR-0003-identity-resolution.md) (chi tiết cơ chế), extends [ADR-0006](ADR-0006-trusted-external-id-overrides-fuzzy-review.md)
- Amendment (2026-11-15): added PostgreSQL-backed identity indexes and a
  checkpointed projector; the original in-memory rebuild remains available for
  tests and offline use.

## Context

Sau khi nạp 23k entity thật, bốn hạn chế của registry in-memory trở thành rủi ro
thực tế:

1. `resolve()` vừa tra cứu vừa mint — không thể hỏi "X là ai?" mà không rủi ro
   tạo entity rác.
2. Alias là 1:1 (first-registration-wins) — xung đột tên bị nuốt im lặng, không
   bao giờ nổi lên cho review.
3. Lookup không xét `entity_type` — hai entity khác loại mang cùng tên sẽ merge
   nhầm nhau.
4. External id 1 giá trị mỗi nguồn; và binding chỉ sống trong RAM — restart là
   mất, mỗi lần chạy lại phải re-derive.

## Initial decision (2026-09-02)

- **Pure `lookup()`**: trả `LookupResult` (`external_id` / `alias` /
  `ambiguous` / `miss`), không mutate, không mint. Chỉ `resolve()` mint.
- **Alias + external id là multimap**: key trùng giữa nhiều entity cùng loại →
  `ambiguous` → review thay vì chọn bản ghi đầu tiên.
- **Type-aware**: lookup với `entity_type` bỏ qua binding của loại khác — cùng
  tên khác loại là hai entity hợp lệ.
- **External id multi-valued**: một entity giữ nhiều id mỗi nguồn (cross-walk,
  re-assignment).
- **Binding là fact**: event **`ExternalIdBound`** ghi mọi binding phát sinh sau
  khi tạo (vd. alias-hit kèm QID mới); `rebuild_identity` replay lại. Registry
  vì thế là projection của log.
- **Backfill**: `tools/backfill_external_ids.py` khôi phục binding của log cũ
  (trước khi payload có `external_ids`) từ quy ước `source_id: wikidata:QID`
  thành `ExternalIdBound` events — idempotent, log không bị sửa.

## Current storage behavior (2026-11-15)

`IdentityStore` has in-memory and PostgreSQL implementations. Without
`FOUNDRY_DATABASE_URL`, identity commands build the in-memory projection from
the event log. With PostgreSQL configured, commands use its persistent indexes
and require the projector checkpoint to match the log before resolving an
identity. The projector applies identity and read-model writes with the event
idempotency record and checkpoint in one transaction. Rebuild is explicit and
replays from the event log; normal restart resumes after the checkpoint.

## Consequences

- (+) Restart không mất định danh: toàn bộ state rebuild từ log một mình.
- (+) Xung đột tên/ID nổi lên thành review queue thay vì im lặng.
- (+) Trong audit ban đầu, re-ingest cùng nguồn cho 298/298 external-ID hit
  trên log 47K event. Đây là kết quả lịch sử, không phải số đo của production
  log hiện tại.
- (-) `ambiguous` làm tăng tỷ lệ review cho dữ liệu trùng tên cùng loại — đúng
  ý design (precision-first), xử lý bằng merge tool (ADR-0007) sau khi human
  xác nhận.
