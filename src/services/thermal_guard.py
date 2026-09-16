"""OmniSpace AI v2.3.1 GPU 温度保护链（规格 §4.1.3 / TASK-016）。

硬件安全最高优先级。规格原文：
  - GPU 温度阈值：> 85°C 触发降频，> 90°C 强制暂停生成任务并弹出 UI 警告。
  - 降频策略：记录连续 3 次高温降频事件后，在当前会话内锁定最大 GPU
    利用率至 80%。

实现要点：
  1. 状态机 normal -> throttling(>=85) -> paused(>=90)，带 5°C 回差
     （降到 80°C 以下才恢复正常），防止阈值边缘状态抖动。
  2. 暂停语义：暂停期间 feature_lock 拒绝一切新重量级任务（40007/20004），
     进行中的任务由调度器 ALL_TENSE 强制卸载兜底中断（规格 TASK-016
     「触发阈值时中断 Pipeline」）；同时向 UI 广播 thermal_pause 警告。
  3. 降频语义：>=85°C 记一次连续高温事件，连续 3 次后本会话
     util_cap 锁定 0.80；状态经 /hardware 与 WS 遥测暴露给前端。
  4. 本模块只做状态判定与广播，不直接操作 CUDA/NVML 写接口
     （锁频需管理员权限，失败安全降级为纯状态记录）。

单例用法::

    from src.services.thermal_guard import get_thermal_guard
    guard = get_thermal_guard()
    guard.check(gpu_temp_celsius)   # 调度器每秒调用
"""
from __future__ import annotations

import logging
import threading
import time

from ..config import THRESHOLDS

log = logging.getLogger("omnispace.thermal_guard")

# ── 状态枚举 ────────────────────────────────────────────────────
STATE_NORMAL = "normal"          # 正常
STATE_THROTTLING = "throttling"  # 降频中（>= warning）
STATE_PAUSED = "paused"          # 已暂停生成（>= critical）

# 回差：温度降到 (warning - hysteresis) 以下才退出 throttling，
# 避免 84.9/85.1 来回抖动导致状态闪烁
_HYSTERESIS_C = 5.0

# 连续高温事件锁定阈值（规格 §4.1.3：连续3次降频后锁定利用率 80%）
_LOCK_AFTER_EVENTS = 3
_LOCKED_UTIL_CAP = 0.80


class ThermalGuard:
    """GPU 温度保护状态机（进程内单例，线程安全）。"""

    _instance: ThermalGuard | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = STATE_NORMAL
        self._consecutive_hot_events = 0     # 连续高温（>=warning）采样计数
        self._util_cap: float | None = None  # 会话级利用率上限（锁定后非 None）
        self._last_temp = 0.0
        self._state_since = time.time()
        self._pause_count = 0                # 累计进入暂停次数（会话统计）
        self._throttle_count = 0             # 累计进入降频次数（会话统计）

    @classmethod
    def instance(cls) -> ThermalGuard:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── 阈值读取 ────────────────────────────────────────────────

    @staticmethod
    def _warning_threshold() -> float:
        try:
            return float(THRESHOLDS.get("gpu_temp_warning", 85))
        except Exception:  # noqa: BLE001
            return 85.0

    @staticmethod
    def _critical_threshold() -> float:
        try:
            return float(THRESHOLDS.get("gpu_temp_critical", 90))
        except Exception:  # noqa: BLE001
            return 90.0

    # ── 主检查入口（调度器每秒调用）──────────────────────────────

    def check(self, temp_celsius: float) -> str:
        """按当前 GPU 温度推进状态机，返回当前状态。

        Args:
            temp_celsius: GPU 核心温度（°C），0 表示采集失败（不动作）
        """
        if temp_celsius <= 0:
            return self._state

        warn = self._warning_threshold()
        crit = self._critical_threshold()
        prev_state = self._state

        with self._lock:
            self._last_temp = temp_celsius

            if temp_celsius >= crit:
                # ── >= 90°C：强制暂停 ──────────────────────────
                self._consecutive_hot_events += 1
                if self._state != STATE_PAUSED:
                    self._state = STATE_PAUSED
                    self._state_since = time.time()
                    self._pause_count += 1
                    log.error(
                        "GPU 温度 %.0f°C >= 临界 %.0f°C：强制暂停生成任务",
                        temp_celsius, crit)
            elif temp_celsius >= warn:
                # ── >= 85°C：降频 ─────────────────────────────
                self._consecutive_hot_events += 1
                if self._state == STATE_NORMAL:
                    self._state = STATE_THROTTLING
                    self._state_since = time.time()
                    self._throttle_count += 1
                    log.warning(
                        "GPU 温度 %.0f°C >= 警戒 %.0f°C：进入降频策略",
                        temp_celsius, warn)
                # 连续 3 次高温 → 会话级锁定利用率 80%
                if (self._consecutive_hot_events >= _LOCK_AFTER_EVENTS
                        and self._util_cap is None):
                    self._util_cap = _LOCKED_UTIL_CAP
                    log.warning(
                        "连续 %d 次高温降频事件：本会话锁定最大 GPU 利用率至 %.0f%%",
                        self._consecutive_hot_events, _LOCKED_UTIL_CAP * 100)
            elif temp_celsius < warn - _HYSTERESIS_C:
                # ── 冷却恢复（带回差）──────────────────────────
                if self._state != STATE_NORMAL:
                    log.info("GPU 温度 %.0f°C 回落至安全区：恢复正常调度",
                             temp_celsius)
                self._state = STATE_NORMAL
                self._state_since = time.time()
                self._consecutive_hot_events = 0
                # 注意：_util_cap 按规格在「当前会话内」保持锁定，不随冷却解除
            # 回差带内（warn-5 <= t < warn）：维持原状态，计数不变

            new_state = self._state
            util_cap = self._util_cap
            hot_events = self._consecutive_hot_events

        # P2 事件化广播：仅在状态迁移时推送一次，不再非 normal 态下每 5s
        # 重复刷 WS（同状态持续期间的实时温度由 /hardware 遥测承载）。
        if new_state != prev_state:
            self._broadcast_state(new_state, temp_celsius, util_cap,
                                  hot_events)
        return new_state

    # ── 对外查询 ────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    def is_paused(self) -> bool:
        """是否处于暂停态（>=critical）：新生成任务必须被拒绝。"""
        return self._state == STATE_PAUSED

    def is_throttling(self) -> bool:
        """是否处于降频态（>=warning）。"""
        return self._state in (STATE_THROTTLING, STATE_PAUSED)

    def get_util_cap(self) -> float | None:
        """会话级 GPU 利用率上限（连续高温锁定后返回 0.80，否则 None）。"""
        return self._util_cap

    def get_status(self) -> dict:
        """热保护状态快照（供 /hardware 与遥测聚合）。"""
        with self._lock:
            return {
                "state": self._state,
                "paused": self._state == STATE_PAUSED,
                "throttling": self._state in (STATE_THROTTLING, STATE_PAUSED),
                "last_temp_celsius": self._last_temp,
                "warning_threshold": self._warning_threshold(),
                "critical_threshold": self._critical_threshold(),
                "consecutive_hot_events": self._consecutive_hot_events,
                "util_cap": self._util_cap,
                "session_pause_count": self._pause_count,
                "session_throttle_count": self._throttle_count,
                "state_since": self._state_since,
            }

    # ── 广播 ────────────────────────────────────────────────────

    def _broadcast_state(self, state: str, temp: float,
                         util_cap: float | None, hot_events: int) -> None:
        """向 UI 广播热保护状态迁移事件（P2 事件化：仅状态变化时调用一次）。

        附带事件时间戳 event_ts（迁移时刻），供前端展示；不代替 /hardware
        遥测的周期性温度更新。
        """
        try:
            from .ws_hub import get_ws_hub
            event = {
                STATE_PAUSED: "thermal_pause",
                STATE_THROTTLING: "thermal_throttle",
            }.get(state, "thermal_recovered")
            get_ws_hub().broadcast({
                "type": "status",
                "module": "system",
                "data": {
                    "event": event,
                    "temp_celsius": temp,
                    "state": state,
                    "util_cap": util_cap,
                    "consecutive_hot_events": hot_events,
                    "message": self._event_message(state, temp),
                    "event_ts": time.time(),
                },
            })
        except Exception as exc:  # noqa: BLE001 - 广播失败不影响保护逻辑
            log.debug("热保护广播失败（忽略）: %s", exc)

    @staticmethod
    def _event_message(state: str, temp: float) -> str:
        if state == STATE_PAUSED:
            return (f"GPU 温度过高（{temp:.0f}°C），已强制暂停生成任务，"
                    f"请等待散热后重试")
        if state == STATE_THROTTLING:
            return f"GPU 温度偏高（{temp:.0f}°C），已降低运行速度"
        return f"GPU 温度已恢复（{temp:.0f}°C）"


# ── 模块级单例 ──────────────────────────────────────────────────

def get_thermal_guard() -> ThermalGuard:
    """获取 ThermalGuard 全局单例。"""
    return ThermalGuard.instance()
# 本项目仅供学习使用，商业授权请+Q 3559331368
