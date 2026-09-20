# 本项目仅供学习使用，商业授权请+Q 3559331368
"""system_settings KV 单一访问器（散乱度根查 B1 根修，2026-09-20）。

此前 `_kv_get/_kv_set` 在 api/system.py 与 api/models.py 逐行克隆
（连注释都相同），另有约 10 处内联裸 SQL 各自 try/except——同一张表
共 12 种读法。本模块收拢为唯一实现；调用方迁移分批进行
（本批：消灭两份克隆；内联站点见总账 B1 排队）。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import database as _dbmod

log = logging.getLogger("omnispace.data.settings_kv")


def kv_get(key: str, default: Any = None) -> Any:
    """读 system_settings 表（JSON 值）；异常/缺失返回 default。

    经模块属性惰性取 _dbmod.get_db_safe（而非 from-import 直接绑定）
    ——monkeypatch database.get_db_safe 的既有测试缝继续生效。
    """
    db = _dbmod.get_db_safe()
    if db is None:
        return default
    try:
        row = db.query_one(
            "SELECT value FROM system_settings WHERE key=?", (key,))
        if row:
            return json.loads(row["value"])
    except Exception as exc:  # noqa: BLE001
        log.debug("system_settings 读取失败 %s: %s", key, exc)
    return default


def kv_set(key: str, value: Any) -> None:
    """写 system_settings 表（JSON 值，UPSERT）。"""
    db = _dbmod.get_db_safe()
    if db is None:
        return
    try:
        db.sql(
            "INSERT INTO system_settings (key, value, updated_at)"
            " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), time.time()))
    except Exception as exc:  # noqa: BLE001
        log.warning("system_settings 写入失败 %s: %s", key, exc,
                    exc_info=True)
