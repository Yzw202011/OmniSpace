# 本项目仅供学习使用，商业授权请+Q 3559331368
"""except-pass 补日志 codemod（重构批 2，2026-09-17）。

`except ...: pass` → `except ...: log.debug("<函数>降级忽略", exc_info=True)`
——降级链语义可辩护（硬件探测/可选功能），但零日志=排障盲区。
debug 级别不刷屏；logging 惰性求值下未启用时开销可忽略。
模块无 logger 的自动补一个。语法自证后落盘。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGGER_RE = re.compile(r"^(log|logger)\s*=\s*logging\.getLogger", re.M)


def find_fn(tree: ast.Module, lineno: int) -> str:
    """行号所在函数名（最近包围）。"""
    best = "<module>"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                if node.lineno >= getattr(find_fn, "_best_ln", 0):
                    best = node.name
    return best


def process(path: Path) -> int:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return 0

    # 收集纯 pass 的 except 体（自底向上替换）
    spans: list[tuple[int, int, str]] = []  # (pass行, pass缩进列, 新文本)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            p = node.body[0]
            fn = find_fn(tree, p.lineno)
            spans.append((p.lineno, p.col_offset, fn))

    if not spans:
        return 0

    lines = src.splitlines(keepends=True)
    for lineno, col, fn in sorted(spans, reverse=True):
        indent = " " * col
        lines[lineno - 1] = (
            f'{indent}log.debug("{fn}: 降级忽略", exc_info=True)\n')

    out = "".join(lines)
    # 模块无 logger 时补（log 名优先，已有 logger 用 logger——统一探测）
    has_log = re.search(r"^log\s*=\s*logging\.getLogger", out, re.M)
    has_logger = re.search(r"^logger\s*=\s*logging\.getLogger", out, re.M)
    if not has_log and not has_logger:
        # 锚=前导块（注释/空行/模块 docstring/import）中最后一个
        # import 行之后；遇其它语句即停（防 E402）
        anchor = 0
        in_doc = False
        offset = 0
        for line in out.splitlines(keepends=True):
            s = line.strip()
            offset += len(line)
            if in_doc:
                if ('"""' in s or "'''" in s):
                    in_doc = False
                continue
            if s.startswith(('"""', "'''")):
                if not (s.count('"""') >= 2 or s.count("'''") >= 2):
                    in_doc = True
                continue
            if (s.startswith(("import ", "from "))
                    or not s or s.startswith("#")):
                if s.startswith(("import ", "from ")):
                    anchor = offset
                continue
            break
        out = (out[:anchor]
               + '\nlog = logging.getLogger(__name__)\n'
               + out[anchor:])
        if not re.search(r"^import logging$", out, re.M):
            out = out[:anchor] + "import logging\n" + out[anchor:]
    elif not has_log and has_logger:
        # 有 logger 没 log——把插入的 log.debug 换 logger.debug
        out = out.replace("log.debug(", "logger.debug(")

    ast.parse(out)  # 语法自证
    path.write_text(out, encoding="utf-8")
    return len(spans)


def main() -> int:
    total = 0
    files = 0
    for py in sorted((ROOT / "src").rglob("*.py")):
        if "__pycache__" in str(py):
            continue
        n = process(py)
        if n:
            total += n
            files += 1
            print(f"  {py.relative_to(ROOT)}: {n}")
    print(f"合计 {total} 处 / {files} 文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
