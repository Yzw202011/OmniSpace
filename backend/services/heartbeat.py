"""后端心跳与崩溃取证（2026-09-01 日志机制方案 C）。

机制：运行期间每 30s 刷新 logs/.heartbeat（pid + 时间戳）；
正常关闭时删除。下次启动若发现残留心跳（且 pid 已死）→ 判定
上次为异常退出（崩溃/被杀/断电），写入 crash_detected 事件供
日志页与诊断包标记「上次异常退出时段」，排障时优先看该段。

心跳历史（2026-09-02 静默死亡取证补强）：每跳同步追加
logs/heartbeat_history.jsonl（pid+ts），start/stop/crash_detected
生命周期事件也入史——末跳文件会被新进程首跳覆盖（09-02 18:49
事故实证：历史证据随覆盖丢失），历史文件让「死亡窗口推定」
（末跳时间 → 看门狗重启首跳）有据可查。超 512KB 轮转保留
末 1000 行（≈8.3 小时心跳）。

永不抛错：取证失败不影响业务主链路。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from ..config import LOGS_DIR

logger = logging.getLogger("omnispace.heartbeat")

HEARTBEAT_FILE = LOGS_DIR / ".heartbeat"
HISTORY_FILE = LOGS_DIR / "heartbeat_history.jsonl"
INTERVAL_S = 30.0
_HISTORY_MAX_BYTES = 512 * 1024
_HISTORY_KEEP_LINES = 1000

_stop = threading.Event()
_thread: threading.Thread | None = None


def _append_history(event: dict) -> None:
    """追加一条心跳史（永不抛错）；超限轮转保留末段。"""
    try:
        event = {"pid": os.getpid(), "ts": time.time(), **event}
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        if HISTORY_FILE.stat().st_size > _HISTORY_MAX_BYTES:
            _rotate_history()
    except OSError as exc:  # noqa: BLE001
        logger.debug("心跳史追加失败（忽略）: %s", exc)


def _rotate_history() -> None:
    try:
        lines = HISTORY_FILE.read_text(
            encoding="utf-8", errors="replace").splitlines()
        kept = lines[-_HISTORY_KEEP_LINES:]
        HISTORY_FILE.write_text(
            "\n".join(kept) + "\n", encoding="utf-8")
        logger.info("心跳史轮转: 保留末 %d 行", len(kept))
    except OSError as exc:  # noqa: BLE001
        logger.debug("心跳史轮转失败（忽略）: %s", exc)


def _pid_alive(pid: int) -> bool:
    try:
        proc = __import__("psutil").Process(pid)
        return proc.is_running()
    except Exception:  # noqa: BLE001 - psutil 不可用/pid 无效 = 不在跑
        return False


def check_previous_crash() -> dict | None:
    """启动期检查：上次是否异常退出。返回崩溃信息 dict 或 None。"""
    try:
        if not HEARTBEAT_FILE.is_file():
            return None
        raw = HEARTBEAT_FILE.read_text(encoding="utf-8", errors="replace").strip()
        info = json.loads(raw) if raw else {}
        pid = int(info.get("pid") or 0)
        ts = float(info.get("ts") or 0.0)
        # pid 仍活着 = 双开被单实例拒绝的场景（非崩溃），不算
        if pid and _pid_alive(pid):
            return None
        result = {
            "pid": pid,
            "last_seen": ts,
            "stale_seconds": round(time.time() - ts, 1) if ts else None,
        }
        _append_history({"event": "crash_detected", **result})
        return result
    except Exception as exc:  # noqa: BLE001 - 取证解析失败按无崩溃处理
        logger.warning("心跳残留解析失败（按无崩溃处理）: %s", exc)
        return None


def _write() -> None:
    beat = {"pid": os.getpid(), "ts": time.time()}
    HEARTBEAT_FILE.write_text(json.dumps(beat), encoding="utf-8")
    _append_history({"event": "beat"})


def start() -> None:
    """启动心跳线程（并立即写一次）。"""
    global _thread
    if _thread is not None:
        return
    _stop.clear()
    _append_history({"event": "start"})
    _write()

    def _loop() -> None:
        while not _stop.wait(INTERVAL_S):
            try:
                _write()
            except OSError as exc:  # noqa: BLE001
                logger.warning("心跳写入失败（忽略）: %s", exc)

    _thread = threading.Thread(target=_loop, daemon=True, name="heartbeat")
    _thread.start()
    logger.info("心跳取证已启动: %s（每 %.0fs，历史 %s）",
                HEARTBEAT_FILE, INTERVAL_S, HISTORY_FILE)


def stop() -> None:
    """正常关闭：停线程 + 删除心跳（残留即代表异常退出）。"""
    _stop.set()
    _append_history({"event": "stop"})
    try:
        HEARTBEAT_FILE.unlink(missing_ok=True)
    except OSError:
        pass
# 本项目仅供学习使用，商业授权请+Q 3559331368
