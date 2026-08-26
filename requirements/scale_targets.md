# Scale Targets

Architecture phải thiết kế hướng tới các con số dưới đây ngay từ đầu,
dù không nhất thiết đạt trong năm đầu.

## Targets đích

```text
Entities        10^8
Relations       10^9
Events/day      10^7
Sources         10^3
Ontology terms  10^4–10^5
```

## Ladder kiểm chứng

| Bước | Dataset | Mục đích |
|-----:|---------|----------|
| 0 | Seed dataset (repo này) | Correctness của CQ + SHACL |
| 1 | Synthetic 10M entities / 100M edges | Ingestion throughput, index build, query p95 |
| 2 | Synthetic 100M entities / 1B edges | Horizontal scalability, replication, recovery |
| 3 | Federation (nhiều domain KG) | Federated queries, semantic contracts |

Ở mỗi bước đo: ingestion throughput, query p95/p99, memory, storage,
index build time, recovery time, replication lag.

## Quy tắc kiến trúc liên quan scale

- Không dùng một database cho mọi thứ — polyglot:
  graph (traversal), relational/columnar (history/analytics), search (text/alias),
  vector (candidate generation), stream (live events).
- Federation > one huge KG: các domain KG riêng nhưng conform chung
  `core + middle + semantic contracts`; query federation xử lý cross-domain.
- Global ID strategy ngay từ đầu: `urn:world:entity:<uuid>`;
  không dùng database auto-increment làm semantic identity.
  Mỗi entity có `canonical_id`, `external_ids[]`, `aliases[]`, `source_ids[]`.
- Event log append-only, immutable — history không bị overwrite.
