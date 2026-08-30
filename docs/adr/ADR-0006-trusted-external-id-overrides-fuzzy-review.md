# ADR-0006: Trusted external id bỏ qua fuzzy review

- Status: accepted
- Date: 2026-08-30
- Amends: [ADR-0003](ADR-0003-identity-resolution.md) (một phần)

## Context

ADR-0003 định tuyến mọi fuzzy name match ≥ 0.50 sang review. Với nguồn có
định danh bên ngoài toàn cục (ví dụ Wikidata QID), hai record QID khác nhau
là hai entity theo định nghĩa của nguồn: tên gần giống nhau không phải bằng
chứng merge. Khi nạp 8.741 tổ chức Wikidata thật, 62.2% bị review chỉ vì tên
các cơ quan hành chính giống nhau — dữ liệu thật bị chặn khỏi lake dù danh
tính đã được nguồn khẳng định qua external id.

## Decision

Trong `foundry/identity.py`, khi caller cung cấp `external_id` (assertion
danh tính bên ngoài) và external id miss:

- Fuzzy candidates **không còn** ép review → tạo mới canonical entity, bind
  external id và alias như luồng tạo mới bình thường.
- Không có external id → giữ nguyên behavior ADR-0003: fuzzy ≥ 0.50 →
  `review`, không bao giờ auto-merge.

Lookup order không đổi: external id hit → exact alias → fuzzy → new. Không có
luồng nào auto-merge hai canonical entity.

## Consequences

- (+) Dữ liệu thật từ nguồn có định danh không bị kẹt review queue; KPI
  `unresolved_rate` tiệm cận 0 cho nguồn đó.
- (+) Không có auto-merge: lỗi đắt nhất theo ADR-0003 vẫn không xảy ra.
- (-) Hai QID mô tả cùng một thực thể thật (duplicate hiếm của chính nguồn)
  cho hai canonical entity — under-merge, rẻ hơn false-merge; gộp dồn xử lý
  bằng merge-tool + audit trail ở Phase 5 như kế hoạch của ADR-0003.
