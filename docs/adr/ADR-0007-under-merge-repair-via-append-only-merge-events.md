# ADR-0007: Under-merge repair qua append-only merge events

- Status: accepted
- Date: 2026-08-30
- Amends: [ADR-0006](ADR-0006-trusted-external-id-overrides-fuzzy-review.md) (phần under-merge)

## Context

ADR-0006 cố tình chọn under-merge: hai QID mô tả cùng một thực thể thật cho hai
canonical entity. Khi dữ liệu thật lớn dần, các cặp trùng này xuất hiện thực tế —
và cần được gộp lại mà **không phá** tính append-only của log (ADR-0002).

## Decision

Correction là một event, không phải một edit:

- Event type mới **`EntityMerged`** với payload `survivor_id`, `duplicate_id`,
  `moved_aliases`, `moved_external_ids`, `reason`.
- `IdentityService.merge_entities()` chuyển toàn bộ alias + external-id binding
  của duplicate sang survivor, ghi redirect vĩnh viễn (`merged_into`), từ chối
  self-merge / unknown id / type conflict / duplicate đã merge.
- Read model projection gộp location history của duplicate vào survivor; id cũ
  chase được qua redirect.
- CLI: `tools/merge_entities.py` rebuild registry từ log trước khi merge để
  validate trên canonical state thật, rồi append event + ghi lake.
- Log không bao giờ bị rewrite: merge là sự kiện xảy ra *sau*, replay lại cho
  cùng kết quả (idempotent theo cấu trúc).

## Consequences

- (+) Mọi merge có audit trail: ai gộp, vì sao, những binding nào di chuyển.
- (+) Replay từ đầu log tái tạo đúng trạng thái sau merge.
- (-) Duplicate giữ một record rỗng trong registry (redirect), không thể tái sử
  dụng id.
- (-) Chưa có un-merge — merge sai phải xử lý bằng một `EntityMerged` khác trỏ
  theo hướng ngược lại (Phase 6 nếu cần).

## Ở ngoài đời thật

Phiên nạp university 2026-09-02 phát hiện **185 QID bị hai canonical entity
claim** (do một lượt chạy ghi nhầm vào log khác). Cả 185 cặp được repair bằng
đúng cơ chế này; sau repair không còn external id nào thuộc nhiều hơn một entity.
