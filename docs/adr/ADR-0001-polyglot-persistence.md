# ADR-0001: Polyglot persistence theo query workload

- Status: accepted
- Date: 2026-08-26

## Context

Các nhóm competency queries có đặc tính hoàn toàn khác nhau: entity lookup cần
point access theo canonical id; traversal 2-3 hop cần graph indexes; analytical
aggregation cần columnar scan; text/alias cần full-text; historical events là
append-heavy. Không một storage engine nào phục vụ tốt cả năm loại.

## Decision

Mỗi loại workload dùng engine chuyên biệt, ontology chỉ bind semantics giữa chúng:

| Workload | Engine |
|----------|--------|
| Graph traversal (Q2/Q3) | Graph store |
| Event history / analytics (Q6) | Relational/columnar (Lakehouse) |
| Text, alias search | Search engine |
| Entity candidate generation cho ER | Vector store |
| Live events | Stream (Kafka-like) |

Operational query path chỉ chạm read models đã materialize — không bao giờ
chạy semantic complexity trực tiếp trên canonical store.

## Consequences

- (+) Mỗi SLO p95 có engine đúng nhiệm vụ; scale độc lập từng phần.
- (-) Cần sync giữa các store → projector lag là SLO bắt buộc (< 5s).
- Federated domain KGs thay vì một central KG khổng lồ.
