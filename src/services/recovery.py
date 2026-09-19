"""批5 P20（2026-09-19）：启动清扫挂死任务 + 每日自动备份。

- 启动清扫：断电/崩溃后 DB 里的 generating/pending 状态是上一进程的
  内存态遗骸（队列已空永远不会有人完成它）——统一标记「已中断（可
  重试）」，杜绝僵尸任务卡界面卡队列。只改状态不删数据。
- 每日自动备份：首次启动后 10 分钟 + 每 24h 检查，距上次自动备份
  超 20h 即跑一轮（命名 omnispace_auto_<ts>.db——落入 P11
  backups_keep 保留白名单）；只备数据库热备（快），设置快照由
  手动备份承担。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

from ..config import DATA_DIR, DB_PATH
from ..data.database import get_db_safe

log = logging.getLogger("omnispace.recovery")

AUTO_BACKUP_DIR = DATA_DIR / "backups"
_MARKER = AUTO_BACKUP_DIR / ".last_auto_backup"
_CHECK_INTERVAL_S = 24 * 3600
_MIN_GAP_S = 20 * 3600


def sweep_stale_tasks() -> dict[str, int]:
    """批5 P20：清扫上一进程遗留的挂死任务状态（启动期调用）。"""
    db = get_db_safe()
    if db is None:
        return {}
    swept: dict[str, int] = {}
    interrupted = "已中断（应用退出/断电），可重试"
    # 关键帧 generating→error（版本落库行）
    n = db.sql("UPDATE keyframes SET status='error', error=?"
               " WHERE status='generating'", (interrupted,))
    if n:
        swept["keyframes"] = n
    # 视频任务 pending/generating→error（video_tasks 无 error 列——错误
    # 详情在内存镜像随进程消失，清扫只改状态；「可重试」由前端 error 态呈现）
    n = db.sql("UPDATE video_tasks SET status='error'"
               " WHERE status IN ('pending','generating')")
    if n:
        swept["video_tasks"] = n
    # 写作章节 generating→error
    n = db.sql("UPDATE novel_chapters SET status='error', error=?"
               " WHERE status='generating'", (interrupted,))
    if n:
        swept["novel_chapters"] = n
    # 训练任务 running/training→error（learn/style 两表）
    for table in ("train_tasks", "style_tasks"):
        try:
            n = db.sql(f"UPDATE {table} SET status='error'"
                       " WHERE status IN ('running','training')")
            if n:
                swept[table] = n
        except Exception:  # noqa: BLE001 - 表缺列（老库）跳过
            continue
    if swept:
        log.warning("P20 启动清扫：上一进程挂死任务已标记中断 %s", swept)
    return swept


def _auto_backup_once() -> bool:
    """跑一次数据库热备（omnispace_auto_ 前缀）；True=已备份。"""
    try:
        AUTO_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dest = AUTO_BACKUP_DIR / f"omnispace_auto_{time.strftime('%Y%m%d_%H%M%S')}.db"
        src = sqlite3.connect(str(DB_PATH))
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        _MARKER.write_text(str(time.time()), encoding="utf-8")
        log.info("P20 自动备份完成: %s", dest.name)
        return True
    except Exception as exc:  # noqa: BLE001 - 备份失败不阻断任何事
        log.warning("P20 自动备份失败: %s", exc, exc_info=True)
        return False


def _loop() -> None:
    # 首轮延迟 10 分钟（避开启动高负载窗口）
    time.sleep(600)
    while True:
        try:
            last = 0.0
            if _MARKER.is_file():
                last = float(_MARKER.read_text(encoding="utf-8").strip() or 0)
            if time.time() - last >= _MIN_GAP_S:
                _auto_backup_once()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_CHECK_INTERVAL_S)


_started = False


def start_recovery_service() -> dict[str, int]:
    """入口（main lifespan 调）：清扫一次 + 起自动备份线程（幂等）。"""
    global _started
    swept = sweep_stale_tasks()
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True,
                         name="auto-backup").start()
    return swept
