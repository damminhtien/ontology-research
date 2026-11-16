# ADR-0006: Trusted external id bỏ qua fuzzy review

- Status: accepted
- Date: 2026-08-30
- Amends: [ADR-0003](ADR-0003-identity-resolution.md) (một phần)
- Amendment (2026-09-02): under-merge repair đã có sẵn —
  [ADR-0007](ADR-0007-under-merge-repair-via-append-only-merge-events.md);
  binding external id bây giờ là fact bền vững trên log
  ([ADR-0010](ADR-0010-registry-as-log-projection.md)).
- Amendment (2026-11-15): chỉ namespace được khai báo unique và authoritative
  mới được dùng để tự resolve; tên và identifier namespace khác chỉ tạo candidate.

## Context

ADR-0003 định tuyến mọi fuzzy name match ≥ 0.50 sang review. Với nguồn có
định danh bên ngoài toàn cục (ví dụ Wikidata QID), hai record QID khác nhau
là hai entity theo định nghĩa của nguồn: tên gần giống nhau không phải bằng
chứng merge. Khi nạp 8.741 tổ chức Wikidata thật, 62.2% bị review chỉ vì tên
các cơ quan hành chính giống nhau — dữ liệu thật bị chặn khỏi lake dù danh
tính đã được nguồn khẳng định qua external id.

## Initial decision (2026-08-30)

Trong `foundry/identity.py`, khi caller cung cấp `external_id` (assertion
danh tính bên ngoài) và external id miss:

- Fuzzy candidates **không còn** ép review → tạo mới canonical entity, bind
  external id và alias như luồng tạo mới bình thường.
- Không có external id → giữ nguyên behavior ADR-0003: fuzzy ≥ 0.50 →
  `review`, không bao giờ auto-merge.

Trong policy ban đầu, lookup order là external ID hit → exact alias → fuzzy →
new. Không có luồng nào tự gộp hai canonical entity.

### Current policy

Namespace policy được định nghĩa trong `foundry/identity.py`. Chỉ external ID
trong namespace có uniqueness và authority đã khai báo mới được dùng làm bằng
chứng `MATCH`. Nếu ID đó chưa có owner, resolver có thể tạo canonical entity
mới dù tên tạo candidate. Alias, fuzzy match và identifier từ namespace không
unique chỉ tạo `REVIEW` candidate; conflict về owner hoặc entity type trả
`CONFLICT`. Resolver score mô tả identity evidence, không phải xác suất fact.

## Consequences

- (+) Record có external ID authoritative chưa được đăng ký có thể được ingest;
  unresolved rate vẫn cần đo theo từng lượt và nguồn dữ liệu.
- (+) Không có auto-merge: lỗi đắt nhất theo ADR-0003 vẫn không xảy ra.
- ~~(-) Hai QID mô tả cùng một thực thể thật (duplicate hiếm của chính nguồn)
  cho hai canonical entity — under-merge, rẻ hơn false-merge; gộp dồn xử lý
  bằng merge-tool + audit trail ở Phase 5 như kế hoạch của ADR-0003.~~ Under-merge
  giờ repair được bằng `EntityMerged`
  ([ADR-0007](ADR-0007-under-merge-repair-via-append-only-merge-events.md)) —
  đã chạy thật với 185 cặp QID trùng.
