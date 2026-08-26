# Competency Questions

Mục tiêu Phase 0: định nghĩa **50–100 competency questions (CQ)** trước khi viết ontology.
Ontology phải được ép vào shape bởi workload, không ngược lại.

Mỗi CQ được gán một nhóm (Q1–Q6) và một SLO p95. Bảng dưới là 12 CQ khởi điểm;
mở rộng dần đến 50–100. Mỗi CQ mới phải có: query SPARQL (hoặc spec),
dataset xác định, và expected results để làm regression test.

| ID | Nhóm | Question | Target p95 |
|----|------|----------|-----------|
| CQ-001 | Q1 | Find entity by identifier/name. | < 50ms |
| CQ-002 | Q1 | Return all attributes of entity X. | < 50ms |
| CQ-003 | Q2 | Which organization operates platform X? | < 100ms |
| CQ-004 | Q2 | Who are members of organization Y? | < 100ms |
| CQ-005 | Q2 | What capabilities does system Z have? | < 100ms |
| CQ-006 | Q3 | Find platforms operated by organizations located in region X. | < 300ms |
| CQ-007 | Q3 | Find facilities within administrative region R (transitive). | < 300ms |
| CQ-008 | Q4 | Where was entity X at time T? | < 500ms |
| CQ-009 | Q4 | Show location history of entity X between T1 and T2. | < 500ms |
| CQ-010 | Q5 | Which sources support fact/assertion F? | < 500ms |
| CQ-011 | Q5 | List all facts derived from source S in interval [T1,T2]. | < 500ms |
| CQ-012 | Q6 | Count entities grouped by type/region/time. (chạy OLAP, không graph store) | n/a |

Nhóm query:

- **Q1 Entity lookup** — point lookup theo identity. Target p95 < 50ms.
- **Q2 1-hop relationship** — quan hệ trực tiếp. Target p95 < 100ms.
- **Q3 2–3 hop graph traversal** — chain qua nhiều loại node. Target p95 < 300ms.
- **Q4 Temporal** — trạng thái tại thời điểm / lịch sử. Target p95 < 500ms.
- **Q5 Provenance** — fact ↔ nguồn. Target p95 < 500ms.
- **Q6 Analytical** — aggregation lớn, chạy trên OLAP engine, không bao giờ trên operational graph store.

Performance invariant đặt ra cho toàn bộ CQ operational:

```
80% operational queries <= 3 graph hops
```

Đây là SLO engineering — phải benchmark, không phải giả định ontology.
