# 本项目仅供学习使用，商业授权请+Q 3559331368
"""FTS 幽灵索引回收工具（散乱度根查 R3-M3/R6-AB3 根修，2026-09-20）。

背景：knowledge_fts 与 knowledge_meta 主键口径不同（fts.kid vs meta.id），
历史一次性删除批次（topic='编剧'，1257 条）中 FTS5 delete 持续失败且被
双层 debug 吞错——49% 幽灵索引固化至今，关键词检索路对高频词 100% 空转。

用法（建议在后端停止的窗口运行；WAL 下运行中运行亦可，但会竞争写锁）：
  py runtime/py312/python.exe tools/fts_repair.py            # 只读体检
  py runtime/py312/python.exe tools/fts_repair.py --apply    # 回收幽灵
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "omnispace.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="执行回收（默认只读体检）")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode={'rw' if args.apply else 'ro'}",
                           uri=True)
    try:
        ghosts = conn.execute(
            "SELECT count(*) FROM (SELECT distinct kid FROM knowledge_fts"
            " EXCEPT SELECT id FROM knowledge_meta)").fetchone()[0]
        missing = conn.execute(
            "SELECT count(*) FROM (SELECT id FROM knowledge_meta"
            " EXCEPT SELECT distinct kid FROM knowledge_fts)").fetchone()[0]
        total = conn.execute("SELECT count(*) FROM knowledge_fts").fetchone()[0]
        print(f"FTS 总条数: {total}")
        print(f"幽灵索引（kid 不在 meta，删除漏清）: {ghosts}")
        print(f"漏索引（meta 不在 FTS，检索盲区）: {missing}")

        if not args.apply:
            print("\n（只读体检；加 --apply 回收幽灵索引）")
            return 0

        if missing:
            print(f"[WARN] 存在 {missing} 条漏索引——本工具不回填（回填需"
                  f"全文重索引，走知识库重建流程）；先处理幽灵。")
        if ghosts == 0:
            print("无幽灵可回收。")
            return 0
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute(
            "DELETE FROM knowledge_fts WHERE kid NOT IN"
            " (SELECT id FROM knowledge_meta)").rowcount
        conn.commit()
        print(f"已回收幽灵索引 {n} 条（knowledge_fts 仅虚表，rowcount 以"
              f"返回为准，复查为准）")
        ghosts2 = conn.execute(
            "SELECT count(*) FROM (SELECT distinct kid FROM knowledge_fts"
            " EXCEPT SELECT id FROM knowledge_meta)").fetchone()[0]
        print(f"复查剩余幽灵: {ghosts2}")
        return 0 if ghosts2 == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
