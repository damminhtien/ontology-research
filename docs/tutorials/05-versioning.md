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
        ├─► release ghi nhận: migration note BẮT BUỘC cho MAJOR
        └─► blast-radius + stability đo chi phí thay đổi
```

## Bước 1 — Kiểm tra tính nhất quán version

```bash
.venv/bin/python tools/manage_ontology.py check-versions
```

```text
[ok   ] ontology/core/core.ttl: no changes since 0.1.0

Version check passed.
```

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

```text
[error] core: no semantic changes since 0.1.0; nothing to release
```

Khi có thay đổi thật, dry-run cho thấy: danh sách changes theo severity,
**suggested version**, blast radius từng term — trước khi quyết định ghi release.

Luồng release chuẩn khi có thay đổi hợp lệ:

```bash
# 1. sửa ontology/core/core.ttl + bump dcterms:version đúng mức
# 2. verify
.venv/bin/python tools/manage_ontology.py check-versions
# 3. preview
.venv/bin/python tools/manage_ontology.py release core --dry-run
# 4. ghi release (MAJOR bắt buộc --migration)
.venv/bin/python tools/manage_ontology.py release core \
    --migration "range of X widened; re-map downstream data"
# 5. commit registry/ + docs/CHANGELOG.md + core.ttl; tag core/x.y.z
```

## Bước 3 — Blast radius: đổi term ảnh hưởng ai?

```bash
.venv/bin/python tools/manage_ontology.py blast-radius Platform
```

```text
Blast radius for 'Platform': BR = 2
  modules (1): ontology/core/core.ttl
  queries (0): none
  applications (1): foundry/ingestion.py
```

Trước khi sửa/xóa một term, luôn xem BR. BR càng cao → chi phí thay đổi càng lớn →
cân nhắc giữ term cũ + thêm term mới thay vì redefine.

## Bước 4 — Stability metric

```bash
.venv/bin/python tools/manage_ontology.py stability
```

```text
core                 stability=1.0     (breaking 0/1) [ok]
```

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
