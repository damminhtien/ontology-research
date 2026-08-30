# ADR-0003: Identity resolution precision-first, không auto-merge

- Status: accepted
- Date: 2026-08-26
- Amendment (2026-08-30): khi record có external id từ nguồn có thẩm quyền và
  external id miss, fuzzy match không còn ép review — xem
  [ADR-0006](ADR-0006-trusted-external-id-overrides-fuzzy-review.md).

## Context

"USS Gerald R. Ford", "CVN-78", "Gerald Ford Carrier" phải về một canonical id,
nhưng "Alpha Patrol Unit One" và "Alpha Patrol Unit Two" phải là hai entity.
Thí nghiệm với overlap/Jaccard thuần túy cho thấy không ngưỡng đơn nào tách đúng
hai trường hợp này: metric reward containment sẽ false-merge các unit chỉ khác
token đánh số.

## Decision

(`foundry/identity.py`) Resolution theo tầng, ưu tiên precision:

1. External id hit -> auto-resolve, confidence 1.0.
2. Exact normalized alias hit -> auto-resolve, confidence 0.95.
3. Fuzzy token overlap >= 0.50 -> KHÔNG merge. Trả method=``review`` kèm danh
   sách candidate ids để human/policy xác nhận.
4. Không khớp gì -> tạo mới `urn:world:entity:<uuid>`.

Pipeline ingestion từ chối record ở trạng thái review (structured receipt,
không drop im lặng). Canonical id không bao giờ dùng DB auto-increment.

## Consequences

- (+) EntityResolutionErrorRate giữ thấp — lỗi merge đắt hơn nhiều lỗi review.
- (-) Throughput review queue phụ thuộc human; cần merge-tool + audit trail ở Phase 5.
- Vector-based candidate generation sẽ thay token-overlap ở Phase 6 nhưng
  vẫn qua cùng review gate.
