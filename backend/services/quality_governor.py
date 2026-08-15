"""OmniSpace AI v2.3 在途质量总督（文档B §4.1.2，F-10）。

规格：GPU 利用率 >95% 持续 10s → 降低**在途**生成任务的质量参数。
持续越线判定在 scheduler/analyzer.py（BottleneckAnalyzer.analyze），
旗标置位/清除在 scheduler tick（__init__.py）；本模块仅承载
线程安全的旗标与降级系数，供生成循环每 step 查询：

- 绘画（paint_engine callback_on_step_end）：命中时置 diffusers
  pipe._interrupt=True 提前停止，解码当前 latents 返回并在结果
  元数据标注 quality_reduced/requested_steps/actual_steps；
- 视频 Ken Burns 为 CPU 密集（非 GPU），不接；
- 训练任务已有自身 OOM 保护，不接。

约束：默认不触发（无监测数据时 should_reduce()=False）；
消费方中断机制失败时不得影响正常生成。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger("omnispace.services.quality_governor")

# 降级系数：命中降参时目标步数 ≈ 原步数 × steps_factor（如 30→20），
# 供调用方换算目标步数；在途中断以"命中即提前停止"为主动作。
DEFAULT_STEPS_FACTOR = 0.67


class QualityGovernor:
    """在途降参旗标（线程安全单例）。

    set_reduce() 由调度周期（scheduler tick）按 analyzer 持续越线
    判定同步；should_reduce() 由生成循环每 step 轮询。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reduce = False
        self.steps_factor: float = DEFAULT_STEPS_FACTOR

    def should_reduce(self) -> bool:
        """当前是否应对在途生成任务降参（默认 False，无监测不触发）。"""
        with self._lock:
            return self._reduce

    def set_reduce(self, reduce: bool) -> None:
        """置位/清除降参旗标；状态翻转时记日志供观测。"""
        reduce = bool(reduce)
        with self._lock:
            changed = reduce != self._reduce
            self._reduce = reduce
        if changed:
            if reduce:
                log.info("GPU 利用率持续越临界线，在途生成任务进入降参"
                         "（steps_factor=%.2f）", self.steps_factor)
            else:
                log.info("GPU 利用率恢复正常，在途降参旗标已清除")


# ── 模块级单例 ──────────────────────────────────────────────────
_governor_instance: Optional[QualityGovernor] = None
_governor_lock = threading.Lock()


def get_quality_governor() -> QualityGovernor:
    """获取 QualityGovernor 全局单例（线程安全双重检查）。"""
    global _governor_instance
    if _governor_instance is None:
        with _governor_lock:
            if _governor_instance is None:
                _governor_instance = QualityGovernor()
    return _governor_instance
