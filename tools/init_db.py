#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库初始化/检查工具（CLI）
- 初始化 SQLite 并校验核心业务表（backend/data/database.py _SCHEMA）
- 诚实标注：明文 SQLite，未启用 SQLCipher 全库加密；仅本地 127.0.0.1 运行
  （规格 §14 约束1/2；审计 BK-046 由 v1.0 残留重写为现行表清单）
用法:
  python tools/init_db.py            初始化并校验
  python tools/init_db.py --check    仅校验不写入
"""
import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend import config  # noqa: E402
from backend.data.database import get_db  # noqa: E402

# 现行核心业务表（对齐 backend/data/database.py _SCHEMA；
# knowledge_meta/kg_*/behavior_logs/learning_* 等为各服务按需自建表，
# 不属于核心 schema 校验范围）
CORE_TABLES = [
    "dialog_sessions", "dialog_messages", "models",
    "projects", "storyboards", "storyboard_rows",
    "director_stages", "director_cameras", "director_characters",
    "voice_profiles", "video_tasks", "train_tasks",
    "system_settings", "schedule_history",
]


def main() -> int:
    ap = argparse.ArgumentParser(description="OmniSpace 数据库初始化")
    ap.add_argument("--check", action="store_true", help="仅校验")
    args = ap.parse_args()

    db = get_db()  # 初始化即完成 schema 建表与存量库列迁移
    if not args.check:
        print(f"数据库初始化完成: {config.DB_PATH}")

    rows = db.query("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {r["name"] for r in rows}
    missing = [t for t in CORE_TABLES if t not in existing]

    wal = db.query_one("PRAGMA journal_mode")
    mode = list(wal.values())[0] if wal else "unknown"
    print(f"journal_mode: {mode}")
    print(f"核心表: {len(CORE_TABLES) - len(missing)}/{len(CORE_TABLES)} 存在")
    if missing:
        print(f"缺失表: {', '.join(missing)}")
        return 1
    print("OK 数据库结构完整（明文 SQLite，仅本地 127.0.0.1 运行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
