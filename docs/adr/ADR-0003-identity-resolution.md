# ADR-0003: Identity resolution precision-first, không auto-merge

- Status: accepted
- Date: 2026-08-26
- Amendment (2026-08-30): khi record có external id từ nguồn có thẩm quyền và
  external id miss, fuzzy match không còn ép review — xem
  [ADR-0006](ADR-0006-trusted-external-id-overrides-fuzzy-review.md).
- Amendment (2026-09-02): cơ chế registry — pure lookup, alias multimap,
  type-aware, external id multi-valued, registry rebuild từ log — xem
  [ADR-0010](ADR-0010-registry-as-log-projection.md).
- Amendment (2026-11-15): tightened identity evidence and added persistent
  indexes; see the current policy below and `foundry/identity.py`.

## Context

"USS Gerald R. Ford", "CVN-78", "Gerald Ford Carrier" phải về một canonical id,
nhưng "Alpha Patrol Unit One" và "Alpha Patrol Unit Two" phải là hai entity.
Thí nghiệm với overlap/Jaccard thuần túy cho thấy không ngưỡng đơn nào tách đúng
hai trường hợp này: metric reward containment sẽ false-merge các unit chỉ khác
token đánh số.

## Initial decision (2026-08-26)

The first resolver version used this order:

1. External id hit -> auto-resolve, confidence 1.0.
2. Exact normalized alias hit -> auto-resolve, confidence 0.95.
3. Fuzzy token overlap >= 0.50 -> KHÔNG merge. Trả method=``review`` kèm danh
   sách candidate ids để human/policy xác nhận.
4. Không khớp gì -> tạo mới `urn:world:entity:<uuid>`.

## Current decision (2026-11-15)

The current resolver distinguishes `MATCH`, `NEW`, `REVIEW` and `CONFLICT`.
Only a source-record key already mapped to the same entity or a hit in a
namespace declared unique and authoritative can return `MATCH`. Exact aliases,
fuzzy names, Splink scores and non-unique identifiers provide candidates for
review; they do not establish identity. A miss without candidates can return
`NEW`. Conflicting record, identifier or type evidence returns `CONFLICT`.

Merge commands require explicit `IdentityEvidence`; a `cannot-link` constraint
blocks the merge. Namespace policy and its resolution scores are defined in
`foundry/identity.py`. These scores describe identity evidence strength and are
not probabilities that an asserted fact is true. Canonical IDs are not database
auto-increment values.

## Consequences

- (+) EntityResolutionErrorRate giữ thấp — lỗi merge đắt hơn nhiều lỗi review.
- ~~(-) Throughput review queue phụ thuộc human; cần merge-tool + audit trail ở
  Phase 5.~~ Merge tool đã có:
  [ADR-0007](ADR-0007-under-merge-repair-via-append-only-merge-events.md) —
  `tools/merge_entities.py`, correction qua `EntityMerged` events.
- Vector-based candidate generation sẽ thay token-overlap ở Phase 6 nhưng
  vẫn qua cùng review gate.
