# 本项目仅供学习使用，商业授权请+Q 3559331368
"""async 内同步 SQLite 调用普查（重构批 1 前置，2026-09-17）。

AST 扫描：async def 函数体内，凡经 get_db_safe()/get_db() 赋值的变量
（或直呼 get_db_safe().xxx）调用 Database 同步方法（query/execute/insert/
update/delete/sql/one）的调用点，逐条列出函数+行号+调用形态。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_METHODS = {"query", "execute", "execute_in_transaction", "insert",
              "update", "delete", "one", "sql", "executemany", "query_one", "query_value", "upsert"}
DB_GETTERS = {"get_db_safe", "get_db"}

hits: list[tuple[str, int, str, str]] = []

for py in sorted((ROOT / "src").rglob("*.py")):
    if "__pycache__" in str(py):
        continue
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"))
    except SyntaxError:
        continue
    rel = py.relative_to(ROOT)

    class V(ast.NodeVisitor):
        def __init__(self, rel: str = "") -> None:
            self.rel = rel
            self.fn = ""
            self.fn_line = 0
            self.db_names: set[str] = set()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            prev_fn, prev_line, prev_names = (
                self.fn, self.fn_line, set(self.db_names))
            self.fn, self.fn_line = node.name, node.lineno
            self.db_names = set()
            self.generic_visit(node)
            self.fn, self.fn_line, self.db_names = prev_fn, prev_line, prev_names

        def visit_Assign(self, node: ast.Assign) -> None:
            if (isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id in DB_GETTERS):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.db_names.add(t.id)
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            f = node.func
            tag = ""
            if isinstance(f, ast.Attribute) and f.attr in DB_METHODS:
                if isinstance(f.value, ast.Name) and (
                        f.value.id in self.db_names or f.value.id == "db"):
                    tag = f"{f.value.id}.{f.attr}"
                elif (isinstance(f.value, ast.Call)
                      and isinstance(f.value.func, ast.Name)
                      and f.value.func.id in DB_GETTERS):
                    tag = f"{f.value.func.id}().{f.attr}"
            if tag and self.fn:
                hits.append((self.rel, node.lineno, self.fn, tag))
            self.generic_visit(node)

    V(str(rel)).visit(tree)

print(f"总计 {len(hits)} 处 / {len({(f, n) for _, _, f, n in hits})} 个函数")
by_file: dict[str, int] = {}
for rel, _line, _fn, _tag in hits:
    by_file[rel] = by_file.get(rel, 0) + 1
for rel, n in sorted(by_file.items(), key=lambda kv: -kv[1]):
    print(f"  {n:3d}  {rel}")
print()
for rel, line, fn, tag in hits:
    print(f"{rel}:{line}  {fn}()  {tag}")
