"""OmniSpace AI v2.3 在途质量总督（文档B §4.1.2，F-10；P2 分级降参）。

规格：GPU 利用率 >95% 持续越线 → 降低**在途**生成任务的质量参数。
P2 分级降参把单档（持续 10s）拆成两档：
  - 持续 5s（level=1）→ 轻度降参，目标步数 × MILD_STEPS_FACTOR(0.67)
  - 持续 15s（level=2）→ 深度降档，目标步数 × DEEP_STEPS_FACTOR(0.4)
持续越线分级判定在 scheduler/analyzer.py（BottleneckAnalyzer.analyze，
输出 last_gpu_util_level 0/1/2），旗标同步在 scheduler tick
（__init__.py）；本模块仅承载线程安全的等级旗标与降级系数，供
生成循环每 step 查询：

- 绘画（paint_engine callback_on_step_end）：命中时置 diffusers
  pipe._interrupt=True 提前停止，解码当前 latents 返回并在结果
  元数据标注 quality_reduced/requested_steps/actual_steps；
- 视频 Ken Burns 为 CPU 密集（非 GPU），不接；
- 训练任务已有自身 OOM 保护，不接。

约束：默认不触发（无监测数据时 reduce_level=0）；消费方中断机制
失败时不得影响正常生成。
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("omnispace.services.quality_governor")

# 降级系数：命中轻度降参时目标步数 ≈ 原步数 × MILD_STEPS_FACTOR（如 30→20），
# 深度降档时 × DEEP_STEPS_FACTOR（如 30→12）；供调用方换算目标步数，
# 在途中断以"命中即提前停止"为主动作。
DEFAULT_STEPS_FACTOR = 0.67  # 轻度降参（level=1，与文档B §4.1.2 基准一致）
DEEP_STEPS_FACTOR = 0.40     # 深度降档（level=2，P2 新增）


class QualityGovernor:
    """在途分级降参旗标（线程安全单例）。

    set_level() 由调度周期（scheduler tick）按 analyzer.last_gpu_util_level
    （0/1/2）同步；should_reduce() 由生成循环每 step 轮询（level≥1 即命中）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._level: int = 0  # 0=正常 1=轻度降参 2=深度降档
        self.steps_factor: float = DEFAULT_STEPS_FACTOR

    def should_reduce(self) -> bool:
        """当前是否应对在途生成任务降参（level≥1，默认 False，无监测不触发）。"""
        with self._lock:
            return self._level >= 1

    @property
    def reduce_level(self) -> int:
        """当前降参等级（0/1/2）。"""
        with self._lock:
            return self._level

    def set_reduce(self, reduce: bool) -> None:
        """兼容旧单档布尔旗标：True→轻度降参，False→正常。"""
        self.set_level(1 if reduce else 0)

    def set_level(self, level: int) -> None:
        """同步分级降参等级；状态翻转时更新降级系数并记日志供观测。"""
        level = int(level)
        with self._lock:
            changed = level != self._level
            self._level = level
            # level 0（正常）与 1（轻度）均回落/停在默认系数，仅 ≥2 深度降档
            self.steps_factor = DEEP_STEPS_FACTOR if level >= 2 else DEFAULT_STEPS_FACTOR
        if changed:
            if level == 1:
                log.info("GPU 利用率持续越临界线，在途生成任务轻度降参"
                         "（steps×%.2f）", DEFAULT_STEPS_FACTOR)
            elif level >= 2:
                log.warning("GPU 利用率持续满载，在途生成任务深度降档"
                            "（steps×%.2f）", DEEP_STEPS_FACTOR)
            else:
                log.info("GPU 利用率恢复正常，在途降参旗标已清除")


# ── 模块级单例 ──────────────────────────────────────────────────
_governor_instance: QualityGovernor | None = None
_governor_lock = threading.Lock()


def get_quality_governor() -> QualityGovernor:
    """获取 QualityGovernor 全局单例（线程安全双重检查）。"""
    global _governor_instance
    if _governor_instance is None:
        with _governor_lock:
            if _governor_instance is None:
                _governor_instance = QualityGovernor()
    return _governor_instance
