"""B3 死端点精确切除器（AST 定位，dry-run 先行）。

用法：
  runtime/py312/python.exe tools/scratch/cut_dead_endpoints.py --dry-run
  runtime/py312/python.exe tools/scratch/cut_dead_endpoints.py --apply
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TARGETS: dict[str, list[str]] = {
    "backend/api/dialog.py": [
        '@router.get("/dialog/history")',
        '@router.get("/dialog/sessions")',
        '@router.post("/dialog/sessions")',
        '@router.delete("/dialog/sessions/{session_id}")',
    ],
    "backend/api/models.py": [
        '@router.get("/models/list")',
        '@router.post("/models/switch")',
        '@router.get("/models/switch/list")',
        '@router.get("/models/switch/{task_id}")',
        '@router.post("/models/switch/{task_id}/cancel")',
        '@router.delete("/models/{model_id}/files")',
    ],
    "backend/api/novel.py": [
        '@router.put("/novel/project/{project_id}")',
        '@router.put("/novel/characters/{character_id}")',
    ],
    "backend/api/learn.py": [
        '@router.post("/learn/lora/train")',
    ],
    "backend/api/knowledge.py": [
        '@router.post("/learn/behavior/reset")',
    ],
    "backend/api/learning.py": [
        '@router.get("/learn/settings/get")',
        '@router.put("/learn/settings/update")',
        '@router.get("/learn/session/log")',
    ],
    "backend/api/voice.py": [
        '@router.get("/voice/models")',
        '@router.post("/voice/transcribe")',
        '@router.post("/voice/synthesize")',
    ],
    "backend/api/draw.py": [
        '@router.post("/paint/img2img")',
        '@router.delete("/draw/history/{task_id}")',
        '@router.delete("/paint/history/{task_id}")',
        '@router.post("/draw/history/batch-delete")',
    ],
    "backend/api/manga/video.py": [
        '@router.get("/video/history")',
    ],
}


def decorator_src(node: ast.FunctionDef, lines: list[str]) -> str:
    deco = node.decorator_list[0]
    return lines[deco.lineno - 1].strip()


def main() -> int:
    apply = "--apply" in sys.argv
    total_fn = 0
    total_deco = 0
    for rel, targets in TARGETS.items():
        p = ROOT / rel
        src = p.read_text(encoding="utf-8")
        lines = src.splitlines(keepends=True)
        tree = ast.parse(src)
        kill_blocks: list[tuple[int, int]] = []      # (start0, end0) 全函数删除
        kill_decos: list[int] = []                   # 仅删一行装饰器
        found: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decos = [lines[d.lineno - 1].strip() for d in node.decorator_list]
            hits = [d for d in decos if d in targets]
            if not hits:
                continue
            found.update(hits)
            others = [d for d in decos if d not in targets]
            if others:
                # 堆叠：本函数还有活路由 → 只摘死装饰器行
                for h in hits:
                    idx = next(i for i, d in enumerate(node.decorator_list)
                               if lines[d.lineno - 1].strip() == h)
                    kill_decos.append(node.decorator_list[idx].lineno - 1)
                    total_deco += 1
                    print(f"[堆叠摘行] {rel}:{node.decorator_list[idx].lineno} {h}")
            else:
                start = min(d.lineno for d in node.decorator_list) - 1
                end = node.end_lineno  # 含尾注释行需回溯空行
                kill_blocks.append((start, end))
                total_fn += 1
                print(f"[全函数] {rel}:{start+1}-{end} {hits[0]} "
                      f"({node.name})")
        missing = set(targets) - found
        if missing:
            print(f"[!! 未命中] {rel}: {missing}")
            return 1
        if not apply:
            continue
        # 从大到小删，免行号漂移
        for s, e in sorted(kill_blocks, reverse=True):
            # 吞掉紧随其后的纯空行（最多 2 行）保持文件整洁
            while e < len(lines) and lines[e].strip() == "" \
                    and len(lines[e]) <= 2 and e - s > 3:
                e += 1
            del lines[s:e]
        for ln in sorted(kill_decos, reverse=True):
            del lines[ln]
        p.write_text("".join(lines), encoding="utf-8", newline="")
    print(f"\n合计：全函数 {total_fn} 个，堆叠摘行 {total_deco} 行；"
          f"模式={'APPLY' if apply else 'DRY-RUN'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
