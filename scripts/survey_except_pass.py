# 本项目仅供学习使用，商业授权请+Q 3559331368
"""except-pass 存量普查（重构批 2 前置，2026-09-17）。

AST 找 except 子句体仅为 pass（或仅注释——源码层注释会被 AST 剥掉，
体为空等价 pass）的处理器，排除已有 noqa BLE001 且体非空的。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

total = 0
by_file: dict[str, int] = {}
for py in sorted((ROOT / "src").rglob("*.py")):
    if "__pycache__" in str(py):
        continue
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"))
    except SyntaxError:
        continue
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            body = node.body
            # 体仅一个 pass 或空
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                n += 1
            elif not body:
                n += 1
    if n:
        rel = str(py.relative_to(ROOT))
        by_file[rel] = n
        total += n

print(f"总计 {total} 处 / {len(by_file)} 文件")
for rel, n in sorted(by_file.items(), key=lambda kv: -kv[1])[:15]:
    print(f"  {n:3d}  {rel}")
