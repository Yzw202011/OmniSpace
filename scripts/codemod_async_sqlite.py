# 本项目仅供学习使用，商业授权请+Q 3559331368
"""async→run_blocking codemod（重构批 1，2026-09-17）。

把 async 函数体内对 Database 的直调改写为
``await run_blocking(lambda: <原调用>)``——语义零变（lambda 闭包捕获
局部量，参数/多行调用逐字保留），仅执行线程从事件循环挪到卸载池。
database.py 连接 check_same_thread=False + 全局 _write_lock，跨线程
安全（:453）。幂等：已包在 run_blocking(...) 参数内的调用不再改；
同步内函数体内的调用不改（随外层整体卸载）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_METHODS = {"query", "execute", "execute_in_transaction", "insert",
              "update", "delete", "one", "sql", "executemany",
              "query_one", "query_value", "upsert"}
DB_GETTERS = {"get_db_safe", "get_db"}

TARGETS = [
    "src/api/manga/comic_asset.py",
    "src/api/manga/storyboard.py",
    "src/api/manga/keyframe.py",
    "src/api/manga/video.py",
    "src/api/manga/voice.py",
    "src/api/models.py",
    "src/api/novel.py",
    "src/main.py",
    "src/services/novel_service.py",
]


class Rewriter(ast.NodeVisitor):
    def __init__(self, src: str) -> None:
        self.src = src
        # (start_line, start_col, end_line, end_col, 包装文本)
        self.spans: list[tuple[int, int, int, int, str]] = []
        self._fn_stack: list[bool] = []
        self._db_stack: list[set[str]] = []
        self._loop_vars: set[str] = set()   # 当前 async fn 内全部循环目标名

    def _is_db_call(self, node: ast.Call) -> bool:
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in DB_METHODS:
            v = f.value
            if isinstance(v, ast.Name):
                names = self._db_stack[-1] if self._db_stack else set()
                if v.id == "db" or v.id in names:
                    return True
            if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                    and v.func.id in DB_GETTERS):
                return True
        return False

    def _loop_targets(self, node: ast.For | ast.AsyncFor) -> None:
        for t in ast.walk(node.target):
            if isinstance(t, ast.Name):
                self._loop_vars.add(t.id)

    def visit_AsyncFunctionDef(self, node) -> None:
        self._fn_stack.append(True)
        self._db_stack.append(set())
        prev_loops, self._loop_vars = self._loop_vars, set()
        # B023 口径：循环体内所有被赋值的名（目标元组 + 循环体里的
        # Assign/AugAssign/NamedExpr/With 目标）都算「循环变量」
        for sub in ast.walk(node):
            if isinstance(sub, (ast.For, ast.AsyncFor, ast.While)):
                if isinstance(sub, (ast.For, ast.AsyncFor)):
                    for t in ast.walk(sub.target):
                        if isinstance(t, ast.Name):
                            self._loop_vars.add(t.id)
                for stmt in sub.body:
                    for n in ast.walk(stmt):
                        if isinstance(n, ast.Name) \
                                and isinstance(n.ctx, ast.Store):
                            self._loop_vars.add(n.id)
        self.generic_visit(node)
        self._loop_vars = prev_loops
        self._fn_stack.pop()
        self._db_stack.pop()

    def visit_FunctionDef(self, node) -> None:
        self._fn_stack.append(False)
        self._db_stack.append(set())
        self.generic_visit(node)
        self._fn_stack.pop()
        self._db_stack.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        if (self._fn_stack and self._fn_stack[-1]
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id in DB_GETTERS):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self._db_stack[-1].add(t.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Name) and f.id == "run_blocking":
            return  # 已是卸载面，不深入
        if (self._fn_stack and self._fn_stack[-1]
                and self._is_db_call(node)):
            seg = ast.get_source_segment(self.src, node)
            if seg:
                # B023 根治：调用中引用的循环变量绑成默认参数
                used = {n.id for n in ast.walk(node)
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
                binds = sorted(used & self._loop_vars)
                params = ", ".join(f"{b}={b}" for b in binds)
                wrapped = (f"await run_blocking(lambda: {seg})"
                           if not binds else
                           f"await run_blocking(lambda {params}: {seg})")
                self.spans.append((
                    node.lineno, node.col_offset,
                    node.end_lineno, node.end_col_offset, wrapped))
        self.generic_visit(node)


def _apply(src: str, spans: list[tuple[int, int, int, int, str]]) -> str:
    lines = src.splitlines(keepends=True)
    # 自底向上：起点行号/列降序
    for sl, sc, el, ec, wrapped in sorted(
            spans, key=lambda s: (s[2], s[3]), reverse=True):
        if sl == el:
            line = lines[sl - 1]
            lines[sl - 1] = line[:sc] + wrapped + line[ec:]
        else:
            first = lines[sl - 1]
            last = lines[el - 1]
            lines[sl - 1:el] = [first[:sc] + wrapped + last[ec:]]
    return "".join(lines)


def process(path: Path) -> tuple[int, int]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    rw = Rewriter(src)
    rw.visit(tree)
    if not rw.spans:
        return 0, 0
    multi = sum(1 for s in rw.spans if s[0] != s[2])
    out = _apply(src, rw.spans)
    ast.parse(out)  # 语法自证
    path.write_text(out, encoding="utf-8")
    return len(rw.spans), multi


def main() -> int:
    total = multi_total = 0
    for rel in TARGETS:
        n, m = process(ROOT / rel)
        total += n
        multi_total += m
        print(f"{rel}: {n} 处（多行 {m}）")
    print(f"合计 {total} 处（多行 {multi_total}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
