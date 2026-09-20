# 本项目仅供学习使用，商业授权请+Q 3559331368
"""screenshot_4in1 存量出库迁移工具（D2-① 根修，2026-09-21）。

背景：video_tasks.screenshot_4in1 曾整列存 base64（实测 98.6MB/61 行，
单行最大 6.5MB，占 131MB 主库约 75%）——列表接口背着 MB 级单行、
库膨胀主因。新代码已改存相对路径（videos/{task_id}_4in1.png）；
本工具把存量内联值迁到文件。建议后端停止时运行（WAL 下亦可，
但会竞争写锁）。

用法：
  py runtime/py312/python.exe tools/screenshot_4in1_export.py          # 体检
  py runtime/py312/python.exe tools/screenshot_4in1_export.py --apply  # 迁移
"""
from __future__ import annotations

import argparse
import base64
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "omnispace.db"
VIDEO_DIR = ROOT / "data" / "videos"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode={'rw' if args.apply else 'ro'}",
                           uri=True)
    try:
        rows = conn.execute(
            "SELECT id, length(screenshot_4in1), screenshot_4in1"
            " FROM video_tasks WHERE length(screenshot_4in1) > 256").fetchall()
        total_b = sum(r[1] for r in rows)
        print(f"内联大值行: {len(rows)}，合计 {total_b/1024/1024:.1f}MB")
        if not rows:
            print("无存量需迁移。")
            return 0
        if not args.apply:
            for r in rows[:5]:
                print(f"  示例 {r[0][:16]}… len={r[1]}")
            print("（体检模式；--apply 执行迁移）")
            return 0

        VIDEO_DIR.mkdir(parents=True, exist_ok=True)
        migrated = skipped = 0
        for tid, _len, val in rows:
            v = (val or "").strip()
            if v.startswith("videos/"):
                skipped += 1
                continue
            try:
                raw = base64.b64decode(v)
            except Exception:
                skipped += 1
                continue
            fp = VIDEO_DIR / f"{tid}_4in1.png"
            fp.write_bytes(raw)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE video_tasks SET screenshot_4in1=? WHERE id=?",
                (f"videos/{fp.name}", tid))
            conn.commit()
            migrated += 1
        print(f"已迁移 {migrated} 行（跳过 {skipped}）→ data/videos/*_4in1.png")
        print("提示：迁移后主库空闲页未回收，可在停机窗口执行"
              " VACUUM（或经备份/恢复链自然收缩）。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
