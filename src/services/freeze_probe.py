"""X5 静默死亡探针（2026-10-22 用户拍板实现）。

L-08 24h 浸泡实锤 2 次静默死亡 + 今晨 01:28-01:35 三连崩 exit 1/3
RAM 充足——死因未定位，缺一个常驻探针在死亡瞬间抓现场。

原理：后台线程每 30s 写心跳到 logs/.watchdog_beat；若主线程冻结
（GIL 死锁/原生扩展段错误前兆），心跳停止但探针仍在跑（独立线程
不受 GIL 阻塞的场景：time.sleep 释放 GIL）；探针检测到主日志>5min
无写入即触发 WER 抓取 + 事件落盘。

死亡后自愈：boot.py 看门狗拉起新进程时，新进程启动期读本文件写入
的 last_freeze_evidence 路径，将证据文件复制到 crash_forensics/。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("omnispace.services.freeze_probe")

_PROBE_INTERVAL_S = 30.0
_LOG_STALL_THRESHOLD_S = 300.0  # 5min 日志无写入=冻结疑似
_BACKEND_LOG = Path("logs/backend.log")
_EVIDENCE_DIR = Path("logs/crash_forensics")
_BEAT_FILE = Path("logs/.freeze_probe_beat")


def _log_last_write_ts() -> float:
    """backend.log 的 mtime（主线程活跃度的代理指标）。"""
    try:
        return _BACKEND_LOG.stat().st_mtime
    except OSError:
        return time.time()  # 文件不存在=不触发


def _grab_wer_evidence(reason: str) -> str | None:
    """抓 WER 事件+进程快照到证据文件。返回文件路径或 None。"""
    try:
        _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        ev = _EVIDENCE_DIR / f"freeze-{ts}.json"

        # WER 事件（最近 10 条）
        wer = []
        try:
            wer_cmd = (
                "Get-WinEvent -LogName Application -MaxEvents 50 | "
                "Where-Object {$_.ProviderName -like '*Windows Error*'"
                " -or $_.LevelDisplayName -eq 'Error'} | "
                "Select-Object -First 10 TimeCreated,ProviderName,Id,"
                "LevelDisplayName,Message | ConvertTo-Json")
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", wer_cmd],
                capture_output=True, text=True, timeout=30, creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
            if r.returncode == 0 and r.stdout.strip():
                wer = json.loads(r.stdout)
                if isinstance(wer, dict):
                    wer = [wer]
        except Exception:  # noqa: BLE001 - WER 抓不到不阻断
            pass

        # 进程快照
        procs = []
        try:
            r2 = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Process | Where-Object {$_.Name -like '*python*'"
                 " -or $_.Name -like '*OmniSpace*'} | "
                 "Select-Object Id,Name,WorkingSet64,CPU,"
                 "StartTime | ConvertTo-Json"],
                capture_output=True, text=True, timeout=15,
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if os.name == "nt" else 0))
            if r2.returncode == 0 and r2.stdout.strip():
                procs = json.loads(r2.stdout)
                if isinstance(procs, dict):
                    procs = [procs]
        except Exception:  # noqa: BLE001
            pass

        ev.write_text(json.dumps({
            "timestamp": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "reason": reason,
            "log_stalled_s": round(time.time() - _log_last_write_ts(), 1),
            "ram_percent": _ram_pct(),
            "wer_events": wer,
            "processes": procs,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        return str(ev)
    except Exception:  # noqa: BLE001 - 证据抓取失败不炸探针
        log.debug("freeze probe: 证据抓取失败", exc_info=True)
        return None


def _ram_pct() -> float:
    try:
        import psutil
        return psutil.virtual_memory().percent
    except Exception:  # noqa: BLE001
        return -1.0


def start_freeze_probe() -> None:
    """启动冻结探针线程（daemon，幂等）。main.py lifespan 调用。"""
    if getattr(start_freeze_probe, "_started", False):
        return
    start_freeze_probe._started = True  # type: ignore[attr-defined]

    def _loop() -> None:
        _last_stall_report = 0.0
        while True:
            time.sleep(_PROBE_INTERVAL_S)
            try:
                _BEAT_FILE.write_text(str(time.time()))
                stall = time.time() - _log_last_write_ts()
                if stall > _LOG_STALL_THRESHOLD_S:
                    # 冻结疑似：限频报告（每 10min 最多一次）
                    now = time.monotonic()
                    if now - _last_stall_report > 600:
                        _last_stall_report = now
                        log.warning(
                            "X5 冻结探针：主日志 %.0f 秒无写入（>%.0f 阈值），"
                            "抓取 WER 证据", stall, _LOG_STALL_THRESHOLD_S)
                        _grab_wer_evidence(f"log_stalled_{stall:.0f}s")
            except Exception:  # noqa: BLE001 - 探针自身异常不阻断
                pass

    t = threading.Thread(target=_loop, daemon=True,
                         name="freeze-probe")
    t.start()
    log.info("X5 冻结探针已启动（间隔 %.0fs，日志停写阈值 %.0fs）",
             _PROBE_INTERVAL_S, _LOG_STALL_THRESHOLD_S)
