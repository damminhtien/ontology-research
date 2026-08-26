# ADR-0002: Append-only event log làm canonical write model

- Status: accepted
- Date: 2026-08-26

## Context

Canonical knowledge cần audit, provenance, temporal correctness ("where was X at
time T") và khả năng replay/rebuild read models. Overwrite-in-place phá history
và khiến provenance không thể truy vết.

## Decision

Mọi thay đổi kiến thức ghi dưới dạng immutable domain events trên log
append-only (`foundry/events.py`, schema v1): `EntityCreated`,
`LocationObserved`, `AffiliationAssessed`. API surface của log không có
update/delete — corrections là event mới supersede event cũ.

Phase hiện tại dùng JSONL file; transport có thể nâng cấp lên Kafka-style
stream mà không đổi payload contract (schema_version field đã có sẵn).

## Consequences

- (+) Full history + replay; read models rebuild được từ log bất kỳ lúc nào.
- (+) Provenance trở thành first-class: mọi event mang source_ids.
- (-) Read models luôn lag sau log → ProjectionLag < 5s là SLO.
- Supersede/correction events sẽ được thêm ở Phase 5 khi có governance.
