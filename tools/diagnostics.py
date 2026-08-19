#!/usr/bin/env python3
"""
系统诊断工具（CLI）
- 数据基础：backend/startup_check.run_startup_check() 的真实 26 项检查
  （审计 BK-047 修复：原引用不存在的 backend.routers.system.build_diagnostics）
- 检查项：Python/OS/GPU/CUDA/显存/驱动/CPU/内存/磁盘/依赖/FFmpeg/模型目录等
- 可导出诊断包（zip），供技术支持分析
用法:
  python tools/diagnostics.py            运行诊断并打印报告
  python tools/diagnostics.py --export   运行诊断并导出诊断包 zip
"""
import argparse
import json
import sys
import time
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import config  # noqa: E402
from backend.startup_check import run_startup_check  # noqa: E402

EXPORT_DIR = config.DATA_DIR / "generated" / "exports"


def _to_item(r: dict) -> dict:
    """startup_check 结果 → CLI 诊断项（passed+level → pass/warn/fail）。"""
    if r["passed"]:
        status = "pass"
    else:
        status = "fail" if r.get("level") == "error" else "warn"
    return {"name": r["name"], "status": status, "detail": r["detail"]}


def main() -> int:
    ap = argparse.ArgumentParser(description="OmniSpace 系统诊断")
    ap.add_argument("--export", action="store_true", help="导出诊断包")
    args = ap.parse_args()

    items = [_to_item(r) for r in run_startup_check()]
    fails = [i for i in items if i["status"] == "fail"]
    warns = [i for i in items if i["status"] == "warn"]

    print("===== OmniSpace 系统诊断 =====")
    overall = "存在严重问题" if fails else ("存在降级项" if warns else "健康")
    print(f"总体状态: {overall}（共 {len(items)} 项，失败 {len(fails)}，降级 {len(warns)}）")
    for item in items:
        mark = {"pass": "✓", "warn": "△", "fail": "✗"}.get(item["status"], "?")
        print(f"  {mark} {item['name']}: {item['detail']}")

    if args.export:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        zip_path = EXPORT_DIR / f"diagnostics_{time.strftime('%Y%m%d_%H%M%S')}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("diagnostics.json",
                       json.dumps(items, ensure_ascii=False, indent=2, default=str))
        print(f"\n诊断包已导出: {zip_path}")

    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
