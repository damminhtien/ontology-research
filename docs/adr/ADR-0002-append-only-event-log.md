# ADR-0002: Append-only event log làm canonical write model

- Status: accepted
- Date: 2026-08-26
- Amended (2026-08-31): contract v2 — sequence/offset, valid-time, upcasters;
  xem [ADR-0009](ADR-0009-event-contract-v2-sequence-valid-time-upcasters.md).
  Contract v4 introduced the assertion-based location model; the current event
  schema is v6. Version history is in `VERSION` and `CHANGELOG-DATA.md`.
  Historical event types were later expanded with `EntityMerged`
  ([ADR-0007](ADR-0007-under-merge-repair-via-append-only-merge-events.md)) và
  `ExternalIdBound` ([ADR-0010](ADR-0010-registry-as-log-projection.md)).

## Context

Canonical knowledge cần audit, provenance, temporal correctness ("where was X at
time T") và khả năng replay/rebuild read models. Overwrite-in-place phá history
và khiến provenance không thể truy vết.

## Decision

Mọi thay đổi kiến thức được ghi thành immutable domain event trong append-only
log (`foundry/events.py`, ban đầu là schema v1). Schema v6 dùng `AssertionMade`
cho location và các world claim; `AssertionSuperseded` ghi retraction. Event
`LocationObserved` lịch sử được upcast khi đọc và không được nhận khi ghi mới.
Log không có update/delete API; correction được ghi thành event mới.

Phase hiện tại dùng JSONL file; transport có thể nâng cấp lên Kafka-style
stream mà không đổi payload contract (schema_version field đã có sẵn).

## Consequences

- (+) Full history + replay; read models rebuild được từ log bất kỳ lúc nào.
- (+) Provenance trở thành first-class: mọi event mang source_ids.
- (-) Read models luôn lag sau log → ProjectionLag < 5s là SLO.
- ~~Supersede/correction events sẽ được thêm ở Phase 5 khi có governance.~~
  Đã có: `EntityMerged`
  ([ADR-0007](ADR-0007-under-merge-repair-via-append-only-merge-events.md)) —
  correction là event mới supersede event cũ, đúng nguyên tắc này.
