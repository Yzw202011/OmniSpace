"""风格库加密种子导入（P6 锁4）：首启动空表自动灌入。

大白话：1198→1117 条画风提示词是核心资产之一。发行包里它以加密种子
（assets_enc/art_styles_seed.json.enc）存在，客户第一次启动时自动
解密导入空库——包里翻不到一句明文提示词。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import json
import logging
import sqlite3
import time

from ..asset_vault import read_asset
from ..config import DB_PATH

log = logging.getLogger("omnispace.style_seed")

SEED_FILE = "art_styles_seed.json.enc"


def ensure_seed() -> dict:
    """art_styles 空表且存在种子时导入。返回统计（供日志/验收）。"""
    from ..data.database import get_db
    db = get_db()
    n = db.query_one("SELECT COUNT(*) AS n FROM art_styles")["n"]
    if n:
        return {"skipped": True, "existing": n}
    blob = read_asset(SEED_FILE)
    if blob is None:
        return {"skipped": True, "reason": "无种子或金库未启用"}
    items = json.loads(blob.decode("utf-8"))
    now = time.time()
    rows = [(it.get("id", ""), it.get("name", ""), it.get("prompt", ""),
             float(it.get("created_at") or now)) for it in items]

    def _do_import(conn: sqlite3.Connection) -> int:
        conn.executemany(
            "INSERT OR IGNORE INTO art_styles(id, name, prompt, created_at)"
            " VALUES (?,?,?,?)", rows)
        return len(rows)

    imported = db.execute_in_transaction(_do_import)
    log.info("风格库种子导入：%d 条（库=%s）", imported, DB_PATH.name)
    return {"imported": imported}
