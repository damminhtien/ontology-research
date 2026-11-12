# Tutorial 05 — Versioning & governance

!!! info "Thời lượng ước tính"
    20 phút. Cần hoàn thành [Tutorial 02](02-ontology-shacl.md).

## Vì sao cần governance

Ontology là **contract công khai**: nhiều consumer (queries, mappings, applications)
phụ thuộc vào nó. Đổi nghĩa silently là phá hợp đồng. Cơ chế của repo:

```text
core.ttl khai báo dcterms:version (SemVer)
        │
        ├─► make check → check-versions chặn bump sai/quên bump
        ├─► registry/ lưu release log + term snapshots (baseline)
        ├─► diff phân loại SAFE / COMPATIBLE / BREAKING bên cạnh SemVer
        ├─► migrate --data scan record cũ + API + indexing trước MAJOR
        └─► blast-radius + stability đo chi phí thay đổi
```

## Bước 1 — Kiểm tra tính nhất quán version

```bash
.venv/bin/python tools/manage_ontology.py check-versions
```

Nếu không có semantic change, lệnh xác nhận version check thành công. Giá trị
version hiện hành đọc từ `VERSION` và release registry.

Bảng chân lý của công cụ:

| Tình huống | Kết quả |
|------------|---------|
| Không đổi, version giữ nguyên | ✅ ok |
| Không đổi nhưng bump version | ❌ bump rỗng |
| Đổi semantics nhưng không bump | ❌ yêu cầu bump tối thiểu |
| Đổi MAJOR nhưng bump PATCH/MINOR | ❌ **silent redefinition bị chặn** |
| Bump cao hơn mức cần | ⚠️ warn (cho qua) |

## Bước 2 — Preview release

```bash
.venv/bin/python tools/manage_ontology.py release core --dry-run
```

Nếu ontology chưa đổi, lệnh báo không có semantic change để release.

Khi có thay đổi thật, dry-run cho thấy danh sách changes, compatibility class,
**suggested version** và blast radius từng term.

Luồng release chuẩn khi có thay đổi hợp lệ:

```bash
# 1. sửa ontology/core/core.ttl + bump dcterms:version đúng mức
# 2. verify
.venv/bin/python tools/manage_ontology.py check-versions
# 3. preview
.venv/bin/python tools/manage_ontology.py release core --dry-run
# 4. preflight: lặp --data cho mọi nguồn dữ liệu authoritative
.venv/bin/python tools/manage_ontology.py migrate core \
    --data data/production.jsonl \
    --data data/lake
# Review both generated files before applying the migration.
# 5. ghi release (MAJOR bắt buộc migration note; script phải có report khớp)
MIGRATION_SCRIPT="migrations/core/2.0.0_to_3.0.0.rq" # example; thay bằng path migrate đã in
.venv/bin/python tools/manage_ontology.py release core \
    --migration "range of X widened; re-map downstream data" \
    --migration-script "$MIGRATION_SCRIPT"
# 6. commit registry/ + docs/CHANGELOG.md + core.ttl + migrations/; tag core/x.y.z
```

## Bước 3 — Blast radius: đổi term ảnh hưởng ai?

```bash
.venv/bin/python tools/manage_ontology.py blast-radius Platform
```

Kết quả tách consumer theo module, shape, event contract, API, indexing,
benchmark query, application và migration script; danh sách dựa trên tham chiếu
từ code hiện hành.

Trước khi sửa/xóa một term, luôn xem BR. BR càng cao → chi phí thay đổi càng lớn →
cân nhắc giữ term cũ + thêm term mới thay vì redefine.

## Bước 4 — Stability metric

```bash
.venv/bin/python tools/manage_ontology.py stability
```

```text
core                 stability=0.3333  (breaking 2/3) [WARN below 0.99]
sensor               stability=1.0     (breaking 0/1) [ok]
tracking             stability=1.0     (breaking 0/2) [ok]
assertion            stability=0.6667  (breaking 1/3) [WARN below 0.95]
identity             stability=0.5     (breaking 1/2) [WARN below 0.95]
location             stability=1.0     (breaking 0/1) [ok]
organization         stability=1.0     (breaking 0/1) [ok]

3 module(s) below their stability threshold.
```

Snapshot hiện tại (2026-11-12): `core`, `assertion` và `identity` đang dưới
ngưỡng sau các breaking releases đã ghi nhận; các module còn lại đạt ngưỡng.
Chạy lại lệnh để đọc trạng thái mới nhất.

$$Stability(m) = 1 - \frac{N_{breaking}}{N_{releases}}$$

Target roadmap: **core ≥ 0.99**, domain ≥ 0.95. Mỗi MAJOR release kéo stability
xuống — metric này làm "đau" hữu hình cho mọi breaking change.

## Bước 5 — Changelog

`docs/CHANGELOG.md` được `release` sinh tự động từ `registry/releases.json` —
không sửa tay. Tab **Versions** trong [Console](06-console.md) hiển thị cùng dữ
liệu kèm timeline và diff giữa hai phiên bản bất kỳ.

## Bài tập

1. Sửa label của class `Platform` (PATCH-level change), giữ nguyên version, chạy
   `check-versions`. Đọc message gợi ý. Sau đó bump đúng mức và kiểm tra lại.
   (**Revert toàn bộ thay đổi sau khi hoàn thành** — đây là thực hành, không release thật.)
2. Vì sao migration note bắt buộc ở MAJOR nhưng không ở MINOR?

## Điều gì vừa xảy ra

Bạn đã nắm đủ bộ governance: SemVer enforcement trong CI, release flow có preview,
migration gate cho MAJOR, blast radius trước khi đổi, stability để theo dõi dài hạn.

## Tiếp theo

- [Tutorial 06 — Console UI](06-console.md): mọi thứ trên trong một giao diện.
