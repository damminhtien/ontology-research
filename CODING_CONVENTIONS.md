# Coding Conventions & Commit Discipline

Tài liệu này là **bắt buộc** cho mọi đóng góp vào repo — con người lẫn AI agents.
Mọi thay đổi phải pass: `make check` (lint + SHACL validate + DAG check + tests).

---

## 1. Commit Discipline (QUAN TRỌNG NHẤT)

> **RULE: Sau MỖI lần thực hiện xong một task/feature/fix, PHẢI commit.**
> Không để code nằm ở working tree qua nhiều phiên làm việc.

### 1.1 Khi nào phải commit

- Hoàn thành một task có thể mô tả trong 1 câu → commit ngay.
- Một session làm việc kết thúc mà còn thay đổi → commit (kể cả WIP, đánh dấu
  rõ bằng prefix `wip:` và commit tiếp theo phải gỡ bỏ trạng thái WIP).
- Fix được một bug kèm regression test → 1 commit atomic.

### 1.2 Quy trình commit bắt buộc

```bash
git status --short          # xem chính xác cái gì thay đổi
make check                  # lint + validate + dag + tests PHẢI xanh trước khi commit
git add <files-cụ-thể>      # KHÔNG BAO GIỜ dùng git add . / git add -A
git diff --cached --stat    # review staged change
git commit -m "<type>: <subject ngắn, mệnh lệnh, dưới 72 ký tự>"
git log --oneline -3        # verify
```

Cấm tuyệt đối:
- `git add .` khi repo có thay đổi không liên quan;
- commit khi `make check` đang đỏ;
- trộn feature + refactor + format trong 1 commit;
- `git push --force`, `git reset --hard`, `git clean` nếu chưa có sự đồng ý rõ ràng.

### 1.3 Message format (Conventional Commits)

```text
<type>(<scope>): <subject ngắn, mệnh lệnh>

[body: giải thích WHY, invariant, trade-off — không mô tả lại diff]

[CLOSES/TICKET nếu có]
```

Type hợp lệ:

| Type | Dùng khi |
|------|----------|
| `feat` | thêm functionality mới |
| `fix` | sửa bug (phải kèm regression test trong cùng commit) |
| `docs` | chỉ thay đổi documentation |
| `refactor` | đổi cấu trúc code, không đổi behavior |
| `test` | thêm/sửa tests |
| `build` | build system, dependencies, lint config |
| `chore` | việc bảo trì khác |

Ví dụ tốt: `fix(tools): reject observations missing atTime in SHACL gate`
Ví dụ xấu: `fix bug`, `update`, `final fix`, `WIP code`

---

## 2. Python Coding Standards

Tooling chuẩn của repo: **ruff** (linter + formatter), config trong `pyproject.toml`.
Chạy `make fmt` trước khi commit; CI chạy `make lint` với `--no-fix`.

### 2.1 Style cơ bản

- Line length ≤ 100; formatter là ruff format (không dùng black/isort riêng).
- Double quotes; type hints bắt buộc trên mọi function signature công khai.
- `from __future__ import annotations` trong các module có type hints phức tạp.
- Import: stdlib → third-party → local, được ruff isort quản lý.
- Không dùng ký tự Unicode nhập nhằng (`–`, `≠`, `→`) trong docstring/message test —
  ruff RUF001/002 sẽ chặn.

### 2.2 Chất lượng senior

**Correctness trước, mọi thứ sau:**

- Mọi external input (file, API, LLM output) là untrusted → validate tại boundary,
  raise exception rõ nghĩa với context (`raise RuntimeError(f"Failed to parse {path}: {exc}") from exc`).
- Không bao giờ `except Exception:` nuốt lỗi im lặng. Chỉ catch broad ở boundary
  deliberate, luôn log/re-raise có ngữ cảnh.
- Prefer `pathlib.Path` over string paths (rule PTH).
- No mutable default arguments; no global mutable state.
- Deterministic behavior: tránh phụ thuộc hash-order, thời gian hiện tại, network
  trong tests. Test phải chạy được offline và lặp lại ra cùng kết quả.

**Structure:**

- Mỗi module một responsibility rõ ràng; shared helpers đặt ở một chỗ duy nhất
  (hiện tại: `tools/ontology_utils.py`) — không copy-paste logic giữa CLI và tests.
- Functions nhỏ, một mục đích; đặt tên nói được hành động (`materialize_type_closure`,
  `layer_of`).
- Constants tập trung đầu module (`FORMATS`, `LAYER_BY_PREFIX`).

**Ontology-specific (riêng repo này):**

- Kernel giữ budget 15–25 classes / 30–50 predicates — test chặn vi phạm.
- Mọi predicate mới PHẢI khai báo `rdfs:domain` + `rdfs:range`.
- Domain concepts (`Tank`, `Sensor`, `Company`…) cấm vào core — thuộc middle/domain layer.
- Mọi dữ liệu sample/benchmark PHẢI conform SHACL shapes trước khi commit.
- Đổi nghĩa một term đã publish = breaking change → bắt buộc migration, không sửa silently.

**Testing:**

- Bug fix = bug fix + regression test trong cùng commit.
- Test observable behavior/contracts, không test implementation trivia.
- Positive VÀ negative cases (ví dụ: role-playing ⇏ reclassification).
- Chạy đúng thứ tự: `make lint && make check` — đỏ ở bước nào sửa bước đó.

### 2.3 Definition of Done cho một thay đổi code

1. `ruff check .` — 0 errors
2. `ruff format --check .` — no diffs
3. `pytest` — all green
4. `make validate` — data conforms SHACL
5. `make dag` — dependency invariant giữ nguyên
6. Committed với message đúng convention, chỉ chứa files liên quan

---

## 3. Non-Python files

- Turtle (.ttl): 4-space indent, prefix block đứng đầu file, comment section
  phân cách `#####`. Validate bằng `tools/validate.py` trước khi commit.
- Markdown: heading hierarchy nhất quán, code fence có language tag.
- Shell scripts/hooks: `set -euo pipefail` khi viết mới, comment giải thích WHY.
