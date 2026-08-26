## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## Commit Discipline (MANDATORY)

Xem chi tiết: `CODING_CONVENTIONS.md`. Tóm tắt bắt buộc:

1. **Sau MỖI task hoàn thành, PHẢI commit ngay** — không để thay đổi nằm ở
   working tree qua nhiều phiên.
2. Trước khi commit: `make check` PHẢI xanh (lint + SHACL validate + DAG + pytest).
3. `git add` từng file cụ thể — cấm `git add .` / `-A`.
4. Message theo Conventional Commits:
   `<feat|fix|docs|refactor|test|build|chore>(<scope>): <subject mệnh lệnh ≤72 ký tự>`
5. Bug fix phải kèm regression test trong cùng commit.
6. Không trộn feature + refactor + format trong 1 commit.

