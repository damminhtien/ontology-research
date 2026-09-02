# ADR-0008: Freeze namespace và scheme định danh trước khi có production facts

- Status: accepted
- Date: 2026-08-31

## Context

Mọi URI/URN được mint từ hệ thống sẽ nằm trong event, lake row và RDF graph —
vĩnh viễn. Đổi namespace **sau** khi có dữ liệu thật đồng nghĩa orphan toàn bộ
facts cũ: không có cách rename in-place an toàn trong mô hình append-only
([ADR-0002](ADR-0002-append-only-event-log.md)).

Trước freeze, ontology vẫn dùng placeholder `https://ontology.example/core#` và
canonical id chưa được chuẩn hóa scheme.

## Decision

`foundry/namespaces.py` là **điểm mint duy nhất** của mọi định danh, với giá trị
đã khóa:

- Ontology: `https://damminhtien.github.io/ontology-research/ontology` (+`/core#`,
  `/middle/location#`, `/domain/tracking#`) — khớp `@prefix` trong `ontology/*.ttl`.
- Canonical entity: `urn:world:entity:<uuid4-hex>` — surrogate id ổn định, không
  bao giờ là DB auto-increment (ADR-0003).
- Fact subject: `urn:fact:<uuid4-hex>`; location do pipeline mint:
  `urn:world:location:<uuid4-hex>`.

Freeze guards (`tests/test_namespaces.py`) pin từng giá trị và cross-check với
`vann:preferredNamespaceUri` trong Turtle header. Test fail = thay đổi không
phải do tai nạn: phải làm namespace mới + explicit migration, không đổi in-place.

## Consequences

- (+) Mọi fact mint từ nay có IRI ổn định, public-resolvable qua GitHub Pages.
- (+) Không ai vô tình đổi URI — CI chặn trước khi merge.
- (-) Mọi đổi tên term tiếp theo là MINOR/MAJOR release có migration note
  (xem [Tutorial 05 — Versioning](../tutorials/05-versioning.md)).
