# Competency Questions

Mục tiêu Phase 0: định nghĩa **50–100 competency questions (CQ)** trước khi viết ontology.
Ontology phải được ép vào shape bởi workload, không ngược lại.

Mỗi CQ được gán một nhóm (Q1–Q6) và một SLO p95. Bảng dưới là 16 CQ khởi điểm;
mở rộng dần đến 50–100. Mỗi CQ mới phải có: query SPARQL (hoặc spec),
dataset xác định, và expected results để làm regression test.

| ID | Nhóm | Question | Target p95 | Evidence |
|----|------|----------|------------|----------|
| CQ-001 | Q1 | Find entity by identifier/name. | < 50ms | Required |
| CQ-002 | Q1 | Return all attributes of entity X. | < 50ms | Backlog |
| CQ-003 | Q2 | Which organization operates platform X? | < 100ms | Required |
| CQ-004 | Q2 | Who are members of organization Y? | < 100ms | Backlog |
| CQ-005 | Q2 | What capabilities does system Z have? | < 100ms | Backlog |
| CQ-006 | Q3 | Find platforms operated by organizations located in region X. | < 300ms | Required |
| CQ-007 | Q3 | Find facilities within administrative region R (transitive). | < 300ms | Backlog |
| CQ-008 | Q4 | Where was entity X at time T? | < 500ms | Required |
| CQ-009 | Q4 | Show location history of entity X between T1 and T2. | < 500ms | Backlog |
| CQ-010 | Q5 | Which sources support fact/assertion F? | < 500ms | Required |
| CQ-011 | Q5 | List all facts derived from source S in interval [T1,T2]. | < 500ms | Backlog |
| CQ-012 | Q6 | Count entities grouped by type/region/time. (chạy OLAP, không graph store) | n/a | Required |
| CQ-013 | Q3 | Which organizations report (transitively) to organization Y? | < 300ms | Required |
| CQ-014 | Q2 | Which entity does track T resolve to? | < 100ms | Required |
| CQ-015 | Q3 | Which tracks belong to entities operated by organizations in region X? | < 300ms | Required |
| CQ-016 | Q5 | Which sensors produced the observations a track derives from? | < 500ms | Required |

`make quality` dùng cột Evidence để kiểm tra từng CQ. Mỗi câu `Required`
phải có một expected result trong `benchmarks/expected_results/`, cùng ID
và nhóm với bảng, tham chiếu đến một query trong `benchmarks/queries/`.
Khi bổ sung query và expected result cho một câu `Backlog`, đổi trạng thái
sang `Required` trong cùng commit. Console hiện hiển thị các câu đã có spec.

Nhóm query:

- **Q1 Entity lookup** — point lookup theo identity. Target p95 < 50ms.
- **Q2 1-hop relationship** — quan hệ trực tiếp. Target p95 < 100ms.
- **Q3 2–3 hop graph traversal** — chain qua nhiều loại node. Target p95 < 300ms.
- **Q4 Temporal** — trạng thái tại thời điểm / lịch sử. Target p95 < 500ms.
- **Q5 Provenance** — fact ↔ nguồn. Target p95 < 500ms.
- **Q6 Analytical** — aggregation lớn, chạy trên OLAP engine, không bao giờ trên operational graph store.
- **Vertical tracking** — CQ-013…CQ-016 kiểm chứng chuỗi Observation → Track → Entity → Organization → Location của domain modules (Phase 4).

Performance invariant đặt ra cho toàn bộ CQ operational:

```
80% operational queries <= 3 graph hops
```

Đây là SLO engineering — phải benchmark, không phải giả định ontology.
