# 本项目仅供学习使用，商业授权请+Q 3559331368
"""数据修剪服务（批5，2026-09-18 用户拍板 30 天保留）。

四张无界增长表 + logs/ 增长目录按期归档/清理：
- learning_logs（36K 行）/ behavior_logs / schedule_history / knowledge_meta
- logs/events/（30 天自清策略已在位）/ logs/eval/ / logs/usage/
  logs/e2e_txt_upload/（测试工件无清理）

启动时后台跑一次 + 每 24h 重复；行级 DELETE（SQLite WAL 无需 VACUUM
——空间复用由后续插入消化；极端膨胀时可手动 VACUUM）。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("omnispace.services.data_prune")

RETENTION_DAYS = 30
_TABLES: list[tuple[str, str]] = [
    # (表名, 时间列名——Unix 秒)
    ("learning_logs", "ts"),
    ("behavior_logs", "timestamp"),
    ("schedule_history", "ts"),
]
_LOG_DIRS = ["logs/eval", "logs/usage", "logs/e2e_txt_upload"]
_LOG_RETENTION_DAYS = 7  # 日志/工件目录更激进（7 天）


def prune_once(root: Path) -> dict[str, int]:
    """跑一轮修剪，返回 {位置: 删行数/文件数}。"""
    stats: dict[str, int] = {}
    db_path = root / "data" / "omnispace.db"
    if db_path.is_file():
        cutoff = time.time() - RETENTION_DAYS * 86400
        try:
            db = sqlite3.connect(str(db_path))
            for table, col in _TABLES:
                try:
                    cur = db.execute(
                        f"DELETE FROM {table} WHERE {col} < ?",
                        (cutoff,))
                    stats[table] = cur.rowcount
                    db.commit()
                except sqlite3.OperationalError as exc:
                    # 表不存在等——跳过不炸
                    log.debug("修剪 %s 跳过: %s", table, exc)
                    stats[table] = -1
            db.close()
        except Exception:  # noqa: BLE001 - DB 不可用时跳过本轮
            log.warning("数据修剪 DB 阶段失败（本轮跳过）", exc_info=True)
            stats["db"] = -1

    # logs/ 增长目录（文件级：超 7 天删）
    log_cutoff = time.time() - _LOG_RETENTION_DAYS * 86400
    for d in _LOG_DIRS:
        dir_path = root / d
        if not dir_path.is_dir():
            continue
        n = 0
        for f in dir_path.rglob("*"):
            if f.is_file() and f.stat().st_mtime < log_cutoff:
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
        if n:
            stats[d] = n

    total = sum(v for v in stats.values() if v > 0)
    if total:
        log.info("数据修剪完成: %s（合计 %d 条/件）", stats, total)
    return stats


def start_background_prune(root: Path) -> None:
    """启动后台修剪线程（24h 间隔，daemon）。"""
    import threading

    def _loop() -> None:
        while True:
            prune_once(root)
            time.sleep(24 * 3600)

    t = threading.Thread(target=_loop, daemon=True, name="data-prune")
    t.start()
    log.info("数据修剪后台线程已启动（%d 天保留，24h 间隔）",
                RETENTION_DAYS)
