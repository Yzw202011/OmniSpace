"""资源占用采样与巡检仪表化（P3-⑤）。

后台守护线程每 RESOURCE_SAMPLE_INTERVAL_S（30s）采集一次
RAM / VRAM / 磁盘快照，双路落地：
  - 有界环形缓冲（内存，默认最近 2 小时样本）：供
    GET /hardware/resource-samples 染出占用趋势（前端仪表）；
  - 追加持久化 JSONL（logs/usage/usage-YYYYMMDD.jsonl，
    按天切分，30 天保留）：超长趋势/事后复盘用。

巡检（patrol）：样本越过资源硬顶（RAM>85%、显存>90%、磁盘>90%）
时，仅在"越界状态翻转（未越界→越界）"时广播一条大白话事件，
不逐样本刷屏——与 ResourceGuard 动作线形成"采集→判定→告警"闭环，
避免事件日志被高频占用刷屏。

采集吞错治理（对齐 hardware._realtime_data 的诚实原则)：psutil 或
torch 不可用时对应字段置 None（available=False），绝不回填模拟读数。
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

log = logging.getLogger("omnispace.resource_sampler")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
USAGE_DIR = _PROJECT_ROOT / "logs" / "usage"

# 采样节奏与保留
RESOURCE_SAMPLE_INTERVAL_S = 30.0
_RING_BACKLOG_SECONDS = 2 * 3600          # 内存环形缓冲：最近 2 小时
USAGE_KEEP_DAYS = 30                       # JSONL 保留天数（每日本巡检清理）

# 巡检硬顶（与 ResourceGuard RAM≤85% / VRAM≤90% 对齐）
_PATROL_THRESHOLDS = {
    "ram_pct": 85.0,
    "vram_pct": 90.0,
    "disk_pct": 90.0,
}


def _snapshot() -> dict:
    """采集当前时刻资源快照（诚实返回，采集失败字段置 None）。"""
    ts = time.time()
    out = {"timestamp": ts, "ram_pct": None, "vram_pct": None,
           "disk_pct": None, "disk_free_gb": None}

    try:  # RAM
        import psutil
        vm = psutil.virtual_memory()
        out["ram_pct"] = round(vm.percent, 1)
    except Exception:  # noqa: BLE001
        pass

    try:  # 显存（torch CUDA）
        import torch  # type: ignore
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            out["vram_pct"] = round((total - free) / total * 100, 1)
    except Exception:  # noqa: BLE001
        pass

    try:  # 磁盘（项目根所在盘）
        du = shutil.disk_usage(str(_PROJECT_ROOT))
        out["disk_pct"] = round(du.used / du.total * 100, 1) if du.total else None
        out["disk_free_gb"] = round(du.free / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001
        pass

    return out


class ResourceSampler:
    """资源占用采样器与巡检器（进程内单例，守护线程驱动）。"""

    def __init__(self, interval_s: float = RESOURCE_SAMPLE_INTERVAL_S) -> None:
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # 最近 2 小时样本（约 240 条 × 30s）
        maxlen = max(1, int(_RING_BACKLOG_SECONDS / interval_s))
        self._samples: deque[dict[str, Any]] = deque(maxlen=maxlen)
        # 巡检越界状态（edge-triggered，防止逐样本刷屏）
        self._alarmed = {"ram_pct": False, "vram_pct": False, "disk_pct": False}

    # ── 生命周期 ──────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        USAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="resource-sampler")
        self._thread.start()
        log.info("资源占用采样器已启动（每 %.0fs 一条样本）", self._interval_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                samp = _snapshot()
                self._record(samp)
                self._patrol(samp)
            except Exception as exc:  # noqa: BLE001 - 采样异常不拖垮线程
                log.debug("资源采样失败: %s", exc)
            self._stop.wait(self._interval_s)

    # ── 采样落地 ──────────────────────────────────────────────

    def _record(self, samp: dict[str, Any]) -> None:
        with self._lock:
            self._samples.append(samp)
        # 异步落盘（append 单行 JSONL，每分钟一条级别，量极小）
        try:
            fh = USAGE_DIR / f"usage-{time.strftime('%Y%m%d', time.gmtime(samp['timestamp']))}.jsonl"
            with fh.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(samp, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响内存采样
            log.debug("资源样本落盘失败: %s", exc)

    def _patrol(self, samp: dict[str, Any]) -> None:
        """越界巡检：仅在未越界→越界翻转时广播一条事件。"""
        crossed = []
        for key, threshold in _PATROL_THRESHOLDS.items():
            val = samp.get(key)
            if val is None:
                continue
            over = val >= threshold
            if over and not self._alarmed[key]:
                self._alarmed[key] = True
                crossed.append((key, val, threshold))
            elif not over:
                self._alarmed[key] = False  # 回落复位，下次越界再报
        if not crossed:
            return
        try:
            from .event_log import log_event
            parts = "、".join(
                f"{_LABEL[k]}已达 {v:.0f}%" if k != "disk_pct"
                else f"磁盘占用已达 {v:.0f}%（接近写满）"
                for k, v, _ in crossed)
            log_event(
                "system", "resource_alarm",
                f"资源占用告警：{parts}。系统会在占用过高时自动释放闲置模型"
                "腾出空间；若持续高位，建议关闭一些未用功能",
                level="warning",
                detail={k: {"value": v, "threshold": t}
                        for k, v, t in crossed})
        except Exception:  # noqa: BLE001
            pass

    # ── 查询 ──────────────────────────────────────────────────

    def get_samples(self, limit: int | None = None) -> list[dict]:
        """取最近样本（时间升序）；limit 截取尾部。"""
        with self._lock:
            items = list(self._samples)
        if limit and limit > 0:
            items = items[-limit:]
        return items


_LABEL = {"ram_pct": "内存占用", "vram_pct": "显存占用", "disk_pct": "磁盘占用"}

# 进程内单例
_sampler: ResourceSampler | None = None
_sampler_lock = threading.Lock()


def get_resource_sampler() -> ResourceSampler:
    """获取 ResourceSampler 单例（首次访问即启动，便于测试）。"""
    global _sampler
    with _sampler_lock:
        if _sampler is None:
            _sampler = ResourceSampler()
            _sampler.start()
        return _sampler
# 本项目仅供学习使用，商业授权请+Q 3559331368
