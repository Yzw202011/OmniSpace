"""隔离区真空删除脚本（2026-09-16 写，09-20 观察期满执行）。

用法：
    python scripts/purge_quarantine.py            # 干跑（默认）：只列清单与体积
    python scripts/purge_quarantine.py --execute  # 真删（不可逆，删前二次确认）

安全设计：
- 只认 data/backups/ 下名字以 quarantine- 开头的目录（白名单前缀，
  绝不碰 backup_*.json / omnispace_*.db 等正常备份）；
- 每个目录删除前要求其内 README.md 存在（隔离区惯例：有说明才是
  隔离区，防误把普通目录当隔离区）；
- --execute 时逐目录打印体积并要求环境变量 OMNISPACE_PURGE_CONFIRM=1
  二次确认；
- 删除后落一行审计日志到 logs/quarantine_purge.log。
背景：docs/收尾计划与 v3 W2 的 41G+235M 隔离区观察期 2026-09-13 起
7 天（09-20 期满）；quarantine-20260915 README 已勘误（TE 已回迁，
区内仅剩 unet 7.75G）——本脚本按目录实际内容删，不依赖 README 清单。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUPS = ROOT / "data" / "backups"
AUDIT_LOG = ROOT / "logs" / "quarantine_purge.log"
PREFIX = "quarantine-"


def _dir_size_gb(p: Path) -> float:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total / 1024 ** 3


def main() -> int:
    ap = argparse.ArgumentParser(description="隔离区真空删除（09-20 期满）")
    ap.add_argument("--execute", action="store_true",
                    help="真删（默认干跑）")
    args = ap.parse_args()

    if not BACKUPS.is_dir():
        print(f"备份目录不存在: {BACKUPS}")
        return 1

    targets = sorted(d for d in BACKUPS.iterdir()
                     if d.is_dir() and d.name.startswith(PREFIX))
    if not targets:
        print("无隔离区目录。")
        return 0

    total_gb = 0.0
    print(f"发现 {len(targets)} 个隔离区目录：")
    for d in targets:
        has_readme = (d / "README.md").is_file()
        size_gb = _dir_size_gb(d)
        total_gb += size_gb
        print(f"  {d.name:44s} {size_gb:7.2f} GB  README={'有' if has_readme else '无'}")

    print(f"合计 {total_gb:.2f} GB")

    if not args.execute:
        print("\n[干跑] 确认无误后加 --execute 真删（需 OMNISPACE_PURGE_CONFIRM=1）。")
        return 0

    if os.environ.get("OMNISPACE_PURGE_CONFIRM") != "1":
        print("拒绝执行：请设 OMNISPACE_PURGE_CONFIRM=1 二次确认。")
        return 1

    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    for d in targets:
        if not (d / "README.md").is_file():
            print(f"跳过 {d.name}（无 README.md，不符合隔离区惯例——人工核查）")
            continue
        size_gb = _dir_size_gb(d)
        shutil.rmtree(d)
        line = (f"{time.strftime('%F %T')} PURGED {d.name} "
                f"{size_gb:.2f}GB")
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(f"已删 {d.name}（{size_gb:.2f} GB）")
    print("完成。审计日志：logs/quarantine_purge.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
