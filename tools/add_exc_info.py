# 本项目仅供学习使用，商业授权请+Q 3559331368
"""批2-3（2026-09-18）：给 except 块内缺堆栈的日志调用补 exc_info=True。

审计口径：438 个带 log.warning/error 的 except 块仅 10% 带 exc_info——
排障时九成异常没有现场堆栈。本工具 AST 定位后精准插入，保守规则：

- 只动 except 处理块内的 logger.warning(...) / logger.error(...) 调用；
- 已有 exc_info= 或 log.exception（自带堆栈）不动；
- 只动单行调用（end_lineno==lineno）且该行以 ')' 收尾——多行调用跳过
  （宁缺勿错，手工补）；
- 插入点：行尾最后一个 ')' 前，补 ", exc_info=True"。

用法：runtime/py310/python.exe tools/add_exc_info.py <文件...> [--dry]
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    par: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            par[id(child)] = node
    return par


def _in_except(node: ast.AST, par: dict[int, ast.AST]) -> bool:
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, ast.ExceptHandler):
            return True
        cur = par.get(id(cur))
    return False


def patch_file(path: Path, dry: bool = False) -> int:
    raw = path.read_bytes().decode("utf-8")
    # 换行符保真（CRLF 历史提交件整翻老坑）：按主导行尾原样回写
    crlf = raw.count("\r\n")
    nl = "\r\n" if crlf >= (raw.count("\n") - crlf) else "\n"
    src = raw.replace("\r\n", "\n")
    lines = src.split("\n")
    tree = ast.parse(src)
    par = _parents(tree)
    edits: list[tuple[int, int]] = []  # (lineno0, insert_col)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute)
                and fn.attr in ("warning", "error")):
            continue
        if any(kw.arg == "exc_info" for kw in node.keywords):
            continue
        if not (node.lineno == node.end_lineno):
            continue
        if not _in_except(node, par):
            continue
        line = lines[node.lineno - 1]
        stripped = line.rstrip()
        if not stripped.endswith(")"):
            continue  # 行尾非右括号（注释/续行）跳过
        col = len(stripped) - 1  # 最后一个 ')' 的位置
        edits.append((node.lineno - 1, col))
    for ln0, col in sorted(edits, reverse=True):
        line = lines[ln0]
        lines[ln0] = line[:col] + ", exc_info=True" + line[col:]
    if edits and not dry:
        path.write_bytes(nl.join(lines).encode("utf-8"))
    return len(edits)


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--dry"]
    dry = "--dry" in sys.argv
    total = 0
    for a in args:
        n = patch_file(Path(a), dry=dry)
        total += n
        print(f"{a}: {n} 处{'（dry）' if dry else ''}")
    print(f"合计 {total} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
