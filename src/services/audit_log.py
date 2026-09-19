"""敏感操作审计日志（批5 P18，2026-09-19）：谁在什么时候动了什么。

大白话：删除/导出/改关键设置/连云端/激活解绑——这些「动了就回不去或
影响面大」的操作全部留痕，设置页可查（近 200 条）。埋点失败绝不阻断
主操作（审计是观测，不是闸门）。

表 audit_log 由 database.py 建表；本模块只提供写/读/清。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..data.database import get_db_safe

log = logging.getLogger("omnispace.audit")

_WRITE_LOCK = threading.Lock()
MAX_KEEP = 2000  # 超出自动修剪（保留最新）


def log_audit(module: str, action: str, target: str = "",
              result: str = "ok", detail: str = "") -> None:
    """记一条审计（任何异常只警告不抛——不阻断主操作）。"""
    if not action:
        return
    try:
        db = get_db_safe()
        if db is None:
            return
        with _WRITE_LOCK:
            db.insert("audit_log", {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "module": module[:32], "action": action[:64],
                "target": target[:200], "result": result[:16],
                "detail": str(detail)[:500]})
            # 修剪（写路径顺带，低频操作无性能顾虑）
            db.sql("DELETE FROM audit_log WHERE id NOT IN ("
                   "SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)",
                   (MAX_KEEP,))
    except Exception as exc:  # noqa: BLE001 - 审计失败不阻断业务
        log.warning("审计写入失败: %s", exc)


def list_audit(limit: int = 200, module: str = "") -> list[dict[str, Any]]:
    db = get_db_safe()
    if db is None:
        return []
    where = "WHERE module=?" if module else ""
    params: tuple = (module,) if module else ()
    rows = db.query(
        f"SELECT id, ts, module, action, target, result, detail"
        f" FROM audit_log {where} ORDER BY id DESC LIMIT ?",
        (*params, max(1, min(1000, limit))))
    return [dict(r) for r in rows]


def clear_audit() -> int:
    db = get_db_safe()
    if db is None:
        return 0
    return db.sql("DELETE FROM audit_log")
