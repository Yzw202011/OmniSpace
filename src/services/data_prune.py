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
_TABLES: list[tuple[str, str, str]] = [
    # (表名, 时间列名, 时间格式——unix=REAL Unix 秒 / iso=TEXT ISO8601)
    ("learning_logs", "ts", "unix"),
    ("behavior_logs", "timestamp", "unix"),
    # P1-09 修复（2026-10-24）：ts 列是 TEXT ISO8601——此前按 Unix 秒
    # float 比较，SQLite 跨存储类中 TEXT 恒大于数值=谓词永假（实测
    # 4550 行零删）。改为 julianday() 归一比较。
    ("schedule_history", "ts", "iso"),
]
_LOG_DIRS = ["logs/eval", "logs/usage", "logs/e2e_txt_upload",
             "logs/events-test", "logs/style_matrix", "logs/four_models",
             "logs/comic6", "logs/_face_analysis", "data/logs",
             "data/tmp_framecheck", "data/audio",
             "data/comfyui/input"]
_LOG_RETENTION_DAYS = 7  # 日志/工件目录（7 天；TTS+ComfyUI input 同档）

# M18 修复（2026-10-24）：ComfyUI output 段产物目录——h3_chains 7.2G
# 实测（每次链跑留一目录，永不清理）；成片已在 video_tasks 具名保存，
# 段产物=中间件（uuid 版本化目录），保留 3 天供排障后即清
_OUTPUT_DIRS = ["data/comfyui/output/h3_chains",
                "data/comfyui/output/face_refine",
                "data/comfyui/output/paint",
                "data/comfyui/output/dlssnr"]
_OUTPUT_RETENTION_DAYS = 3  # 段产物比日志更激进（3 天）


def prune_once(root: Path) -> dict[str, int]:
    """跑一轮修剪，返回 {位置: 删行数/文件数}。"""
    stats: dict[str, int] = {}
    db_path = root / "data" / "omnispace.db"
    if db_path.is_file():
        cutoff = time.time() - RETENTION_DAYS * 86400
        # P1-09：ISO8601 列的等价 cutoff（与 Unix cutoff 同时刻）
        import datetime as _dt
        cutoff_iso = _dt.datetime.fromtimestamp(
            cutoff, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            db = sqlite3.connect(str(db_path))
            for table, col, fmt in _TABLES:
                try:
                    if fmt == "iso":
                        # TEXT ISO8601 → julianday 归一到天比较
                        cur = db.execute(
                            f"DELETE FROM {table} WHERE "
                            f"julianday({col}) < julianday(?)",
                            (cutoff_iso,))
                    else:
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

    # M18（2026-10-24）：ComfyUI output 段产物目录级清理（3 天保留）
    # h3_chains 每次链跑留一个 uuid 目录（实测 34 目录 7.2G 永不清理）
    # 成片已在 video_tasks 具名保存，段产物=纯中间件
    import shutil
    output_cutoff = time.time() - _OUTPUT_RETENTION_DAYS * 86400
    for d in _OUTPUT_DIRS:
        dir_path = root / d
        if not dir_path.is_dir():
            continue
        n = 0
        for sub in dir_path.iterdir():
            if sub.is_dir() and sub.stat().st_mtime < output_cutoff:
                try:
                    shutil.rmtree(sub, ignore_errors=True)
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
            # R7-AI1 根修（2026-09-20）：循环体包异常续命——原先裸奔，
            # 文件竞态上抛即线程死亡且零日志（30 天保留策略静默失效）。
            try:
                prune_once(root)
            except Exception:  # noqa: BLE001
                log.warning("数据修剪执行异常（下轮重试）", exc_info=True)
            time.sleep(24 * 3600)

    t = threading.Thread(target=_loop, daemon=True, name="data-prune")
    t.start()
    log.info("数据修剪后台线程已启动（%d 天保留，24h 间隔）",
                RETENTION_DAYS)
