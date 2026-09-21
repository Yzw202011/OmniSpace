"""文档对账闸（第5闸，2026-09-19）：tools/doc_claims.json 断言 vs 仓库实况。

背景：四闸（compileall/ruff/smoke/mypy）只查代码不查文档，文档漂移已三轮复发
（09-10 文档漂移审计 / INDEX 漏登记 29+38 份 / 09-17 幽灵警告「安全盾勿提交」
在已入库后仍挂着）。本闸套用 torch_contract.json 与 style_pack_lint.py 的
成熟模式：机器可校验断言文件 + 校验脚本 + 挂闸口。

用法:
    runtime/py312/python.exe tools/doc_drift_check.py          # 全量（pre-commit 第5步 / run_tests L0）
    runtime/py312/python.exe tools/doc_drift_check.py --list   # 列出全部断言
    runtime/py312/python.exe tools/doc_drift_check.py --strict # warn 级失配也计失败

退出码: 0=无 block 级失配（--strict 下 warn 也计）；1=有失配。
绕过（自担风险）: git commit --no-verify。
维护铁律: 相关事实变动的同一提交内同步更新 doc_claims.json（先读该条 why）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAIMS_PATH = REPO_ROOT / "tools" / "doc_claims.json"

_VALID_TYPES = {
    "file_contains",
    "file_not_contains",
    "path_exists",
    "path_absent",
    "count_matches",
}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _first_match_line(path: Path, pattern: str) -> int:
    """返回 pattern 首次命中的行号（1 起），未命中返回 -1——失配详情带行号便于定位。"""
    rx = re.compile(pattern)
    for lineno, line in enumerate(_read_text(path).splitlines(), start=1):
        if rx.search(line):
            return lineno
    return -1


def _glob_files(root: Path, patterns: list[str]) -> list[Path]:
    files: set[Path] = set()
    for pat in patterns:
        files.update(p for p in root.glob(pat) if p.is_file())
    return sorted(files)


def check_claim(root: Path, claim: dict[str, Any]) -> tuple[bool, str]:
    """校验单条断言，返回 (是否通过, 人话详情)。root 为仓库根（可注入，供单测用）。"""
    ctype = claim.get("type")
    if ctype not in _VALID_TYPES:
        return False, f"未知断言类型 {ctype!r}（合法: {sorted(_VALID_TYPES)}）"
    if ctype in {"file_contains", "file_not_contains"}:
        path = root / claim["path"]
        if not path.is_file():
            return False, f"文件不存在: {claim['path']}"
        hit = re.compile(claim["pattern"]).search(_read_text(path))
        if ctype == "file_contains":
            return (hit is not None), (
                f"命中 /{claim['pattern']}/" if hit else f"未找到 /{claim['pattern']}/"
            )
        if hit is None:
            return True, f"已无 /{claim['pattern']}/"
        lineno = _first_match_line(path, claim["pattern"])
        return False, f"仍命中 /{claim['pattern']}/（首见 :{lineno}）"
    if ctype == "path_exists":
        path = root / claim["path"]
        return path.exists(), (f"在位 {claim['path']}" if path.exists() else f"缺失 {claim['path']}")
    if ctype == "path_absent":
        path = root / claim["path"]
        gone = not path.exists()
        return gone, (f"已清除 {claim['path']}" if gone else f"仍存在 {claim['path']}")
    # count_matches
    files = _glob_files(root, claim["paths"])
    rx = re.compile(claim["pattern"])
    got = sum(len(rx.findall(_read_text(p))) for p in files)
    expected = int(claim["expected"])
    return (got == expected), f"实测 {got}，断言 {expected}（扫描 {len(files)} 文件）"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="文档对账闸：doc_claims.json 断言校验（防文档漂移）")
    ap.add_argument("--list", action="store_true", help="仅列出断言清单")
    ap.add_argument("--strict", action="store_true", help="warn 级失配也计失败")
    args = ap.parse_args(argv)

    if not CLAIMS_PATH.is_file():
        print(f"[doc-drift] ✗ 断言清单缺失: {CLAIMS_PATH}")
        return 1
    claims: list[dict[str, Any]] = json.loads(_read_text(CLAIMS_PATH)).get("claims", [])

    if args.list:
        for c in claims:
            print(f"  [{c.get('severity', 'block'):5}] {c.get('id', '?'):36} {c.get('why', '')}")
        print(f"共 {len(claims)} 条断言（{CLAIMS_PATH.name}）")
        return 0

    failed = warned = 0
    for c in claims:
        ok, detail = check_claim(REPO_ROOT, c)
        if ok:
            continue
        if c.get("severity", "block") == "warn":
            warned += 1
            print(f"[doc-drift] ⚠ [warn] {c.get('id', '?')}: {detail} —— {c.get('why', '')}")
        else:
            failed += 1
            print(f"[doc-drift] ✗ [block] {c.get('id', '?')}: {detail} —— {c.get('why', '')}")

    total = len(claims)
    if failed or (args.strict and warned):
        print(
            f"[doc-drift] ✗ {total} 条断言中断配：{failed} block + {warned} warn"
            "（按 why 修复文档或更新断言后重试；绕过 = git commit --no-verify 自担）"
        )
        return 1
    print(f"[doc-drift] ✓ {total}/{total} 断言通过（warn {warned}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
