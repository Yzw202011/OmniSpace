#!/usr/bin/env python3
"""可移植性检查（P0 工具）：扫描发行白名单内的写死盘符。

分级：
  CRITICAL —— 发行白名单目录/文件里出现盘符路径（出包前必须清零）
  WARN     —— 测试/文档等不随发行走的位置出现盘符（不挡发布，记录在案）

用法：
  runtime/py310/python.exe tools/portability_check.py            # 全量扫描
  runtime/py310/python.exe tools/portability_check.py --strict  # WARN 也算失败

返回码：0 通过；1 有 CRITICAL（或 --strict 下有 WARN）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 盘符路径：字母 + 冒号 + 斜杠（http:// 因前面是字母不构成词边界，不会误报）
DRIVE_RE = re.compile(r"(?i)\b[a-z]:[\\/]")

# 行内豁免标记：写上 portable-ok 的行不报（示例文案/刻意为之的系统路径说明）
MARKER = "portable-ok"

# 系统资源路径（任何 Windows 机器都有，不算可移植性问题，单列 INFO）
SYSTEM_PREFIXES = ("c:\\windows", "c:\\program files", "c:\\ffmpeg")

TEXT_EXT = {".py", ".bat", ".ps1", ".cmd", ".yaml", ".yml", ".toml",
            ".cfg", ".ini", ".pth", ".ts", ".tsx", ".js", ".css", ".html", ".json"}

# 发行白名单（CRITICAL 区）：目录、(扩展名集合, 跳过的子目录名)
# 注：tools/ 降到 WARN——它是开发工具箱，发行只挑个别文件（P1 make_dist 自带文件级白名单）
CRITICAL_DIRS = [
    ("backend", TEXT_EXT, set()),
    ("frontend/src", TEXT_EXT, set()),
    ("frontend/vite.config.ts", TEXT_EXT, set()),
    ("launcher", TEXT_EXT, set()),
    ("runtime", {".pth"}, {"py313"}),  # _pth 是历次事故源头，必扫
]
CRITICAL_FILES = [
    "models/models_manifest.json",
    "启动OmniSpace.bat",
    "停止OmniSpace.bat",
]
# 根目录散装脚本/配置（发行打包时按文件名白名单带走）
CRITICAL_ROOT_GLOBS = ["*.py", "*.yaml", "*.toml", "*.bat", "*.ps1"]

# 不随发行走（WARN 区）：知道即可
WARN_DIRS = [
    ("tests", TEXT_EXT),
    ("scripts", TEXT_EXT),
    ("tools", {".py", ".bat", ".ps1"}),
    ("docs", {".md"}),
]

# backend 根下的调试残渣（下划线/tmp_ 开头）不算发行代码，降为 WARN 并提示 P1 排除
DEV_DEBRIS_RE = re.compile(r"^(_|tmp_).+\.py$")


def classify(line: str) -> str | None:
    """返回该行的分级：CRITICAL / SYSTEM / None（不报）。"""
    if MARKER in line:
        return None
    low = line.lower()
    if any(low.find(p) != -1 for p in SYSTEM_PREFIXES):
        return "SYSTEM"
    return "CRITICAL"


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return hits
    for i, line in enumerate(text.splitlines(), 1):
        if DRIVE_RE.search(line):
            cls = classify(line)
            if cls:
                hits.append((i, line.strip()[:160], cls))
    return hits


def is_dev_debris(path: Path) -> bool:
    return bool(DEV_DEBRIS_RE.match(path.name))


def scan_tree(rel: str, exts: set[str], skip: set[str], level: str,
              report: dict[str, list[str]]) -> int:
    root = REPO / rel
    if not root.exists():
        return 0
    if root.is_file():
        if root.suffix in exts:
            for i, line, cls in scan_file(root):
                report[cls if cls == "SYSTEM" else level].append(
                    f"{rel}:{i}: {line}")
            return 1
        return 0
    n = 0
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        rel_parts = p.relative_to(REPO).parts
        if skip & set(rel_parts[:-1]):
            continue
        if any(part.startswith(".") for part in rel_parts[:-1]):
            continue  # 隐藏目录（.git/.mimosa 等工具缓存）不扫
        # 残渣降级只认 backend 根目录与仓库根的下划线/tmp_ 脚本，避免误标第三方 __init__.py
        debris = (level == "CRITICAL" and is_dev_debris(p)
                  and len(rel_parts) == 2 and rel_parts[0] == "backend")
        eff_level = "WARN" if debris else level
        suffix_note = "（开发残渣，P1 打包须排除）" if debris else ""
        n += 1
        for i, line, cls in scan_file(p):
            bucket = cls if cls == "SYSTEM" else eff_level
            report[bucket].append(
                f"{p.relative_to(REPO).as_posix()}:{i}{suffix_note}: {line}")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="WARN 也视为失败")
    args = ap.parse_args()

    report: dict[str, list[str]] = {"CRITICAL": [], "WARN": [], "SYSTEM": []}
    scanned = 0
    for rel, exts, skip in CRITICAL_DIRS:
        scanned += scan_tree(rel, exts, skip, "CRITICAL", report)
    for rel in CRITICAL_FILES:
        p = REPO / rel
        if p.exists():
            scanned += 1
            for i, line, cls in scan_file(p):
                report[cls if cls == "SYSTEM" else "CRITICAL"].append(
                    f"{rel}:{i}: {line}")
    for pattern in CRITICAL_ROOT_GLOBS:
        for p in REPO.glob(pattern):
            if p.is_file():
                scanned += 1
                eff = "WARN" if is_dev_debris(p) else "CRITICAL"
                note = "（开发残渣，P1 打包须排除）" if eff == "WARN" else ""
                for i, line, cls in scan_file(p):
                    bucket = cls if cls == "SYSTEM" else eff
                    report[bucket].append(f"{p.name}:{i}{note}: {line}")
    for rel, exts in WARN_DIRS:
        scan_tree(rel, exts, set(), "WARN", report)

    print(f"扫描完成：{scanned} 个白名单文件")
    for level in ("CRITICAL", "WARN", "SYSTEM"):
        items = report[level]
        tag = {"CRITICAL": "❌", "WARN": "⚠️", "SYSTEM": "ℹ️"}[level]
        label = {"CRITICAL": "白名单内写死盘符", "WARN": "不随发行走的位置",
                 "SYSTEM": "系统资源路径（合法）"}[level]
        print(f"\n[{level}] {label}：{len(items)} 处")
        for item in items[:20]:
            print(f"  {tag} {item}")
        if len(items) > 20:
            print(f"  …另有 {len(items) - 20} 处省略")

    if report["CRITICAL"]:
        print("\n结论：❌ 白名单内仍有写死盘符，出包前必须清零")
        return 1
    if args.strict and report["WARN"]:
        print("\n结论：⚠️ 白名单干净；--strict 模式下 WARN 也算失败")
        return 1
    print("\n结论：✅ 白名单内零盘符，可移植性通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
