"""OmniSpace AI v2.1 决策引擎模块（规格 §5.1 决策层）。

带滞回控制（hysteresis_seconds=30s），避免在阈值边缘频繁切换模式。
当分析器推荐的模式与当前模式不同时，需持续 30 秒后才会实际切换。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ...config import HYSTERESIS_SECONDS
from ...data.models import SynergyMode

if TYPE_CHECKING:
    from .dispatcher import TaskDispatcher

logger = logging.getLogger("omnispace.scheduler.decision")


class DecisionEngine:
    """决策引擎——带滞回控制的模式切换。"""

    def __init__(self) -> None:
        self._current_mode: SynergyMode = SynergyMode.GPU_PRIMARY
        self._candidate_mode: SynergyMode | None = None
        self._candidate_since: float = 0.0
        self._last_switch_time: float = 0.0
        # 最近一次切换的滞回等待时长（毫秒），供调度历史学习引擎落库
        self._last_switch_wait_ms: int = 0
        # 最近一次切换的触发源：hysteresis 滞回确认 / emergency 紧急直通
        self._last_switch_trigger: str = ""

    @property
    def current_mode(self) -> SynergyMode:
        """当前实际生效的模式。"""
        return self._current_mode

    @property
    def last_switch_wait_ms(self) -> int:
        """最近一次模式切换的滞回等待时长（毫秒）。"""
        return self._last_switch_wait_ms

    @property
    def last_switch_trigger(self) -> str:
        """最近一次模式切换的触发源。"""
        return self._last_switch_trigger

    def evaluate(self, recommended: SynergyMode) -> SynergyMode:
        """评估推荐模式，应用滞回控制后返回应生效的模式。

        滞回逻辑:
          - 推荐模式与当前模式相同时：清除候选，返回当前模式
          - 推荐模式与当前不同时：记录候选 + 时间戳
            - 若候选持续时间 >= HYSTERESIS_SECONDS：提交切换
            - 否则：维持当前模式

        特殊: ALL_TENSE（全面紧张）是紧急模式，立即切换不经滞回。

        Args:
            recommended: 分析器推荐的模式

        Returns:
            实际应生效的模式
        """
        now = time.time()

        # 推荐模式与当前相同，清除候选
        if recommended == self._current_mode:
            self._candidate_mode = None
            return self._current_mode

        # 紧急模式：全面紧张时立即切换
        if recommended == SynergyMode.ALL_TENSE:
            self._current_mode = recommended
            self._candidate_mode = None
            self._last_switch_time = now
            self._last_switch_wait_ms = 0
            self._last_switch_trigger = "emergency"
            logger.warning("紧急切换到 ALL_TENSE 模式（跳过滞回）")
            return self._current_mode

        # 记录或更新候选
        if self._candidate_mode != recommended:
            self._candidate_mode = recommended
            self._candidate_since = now
            logger.debug(
                "候选模式: %s（等待 %d 秒滞回确认）",
                recommended.value,
                HYSTERESIS_SECONDS,
            )
            return self._current_mode

        # 候选已持续足够时间，提交切换
        elapsed = now - self._candidate_since
        if elapsed >= HYSTERESIS_SECONDS:
            old_mode = self._current_mode
            self._current_mode = recommended
            self._candidate_mode = None
            self._last_switch_time = now
            self._last_switch_wait_ms = int(elapsed * 1000)
            self._last_switch_trigger = "hysteresis"
            logger.info(
                "模式切换: %s -> %s（滞回 %d 秒确认）",
                old_mode.value,
                recommended.value,
                HYSTERESIS_SECONDS,
            )
            return self._current_mode

        # 仍在滞回等待中
        return self._current_mode

    def execute_strategy(
        self,
        mode: SynergyMode,
        dispatcher: TaskDispatcher,
    ) -> None:
        """执行对应模式的调度策略（规格 §5.1 策略表）。

        各模式策略:
          - GPU_PRIMARY:     GPU 全速，CPU 待命
          - CPU_ASSIST:      GPU 负载迁移部分层到 CPU
          - GPU_ASSIST_CPU:  CPU 为主力，GPU 辅助
          - MEMORY_PRESSURE: 压缩不活跃的模型缓存
          - ALL_TENSE:       全面降级（精度 + 卸载）
          - ALL_IDLE:        预加载常用模型

        Args:
            mode: 目标协同模式
            dispatcher: 任务分发器实例
        """
        strategy_map = {
            SynergyMode.GPU_PRIMARY: self._strategy_gpu_primary,
            SynergyMode.CPU_ASSIST: self._strategy_cpu_assist,
            SynergyMode.GPU_ASSIST_CPU: self._strategy_gpu_assist_cpu,
            SynergyMode.MEMORY_PRESSURE: self._strategy_memory_pressure,
            SynergyMode.ALL_TENSE: self._strategy_all_tense,
            SynergyMode.ALL_IDLE: self._strategy_all_idle,
        }
        strategy_fn = strategy_map.get(mode)
        if strategy_fn:
            strategy_fn(dispatcher)

    # ── 各模式策略实现 ──────────────────────────────────────────

    def _strategy_gpu_primary(self, dispatcher: TaskDispatcher) -> None:
        """GPU 主力：全速运行，确保模型在显存中。"""
        logger.info("策略[GPU_PRIMARY]: GPU 全速运行")
        dispatcher.preload(["dialog", "paint"])

    def _strategy_cpu_assist(self, dispatcher: TaskDispatcher) -> None:
        """CPU 辅助：将 GPU 部分层迁移到 CPU。

        80% 档两态（显存调度机制批2，2026-09-10，方案 §3.4）：
          - 生成期（功能锁持有）：migrate_to_cpu 因 2026-08-22 事故
            保护被整体跳过（dispatcher 持锁即 return）——此前
            0.80~0.90 区间在生成期是「空调区」（唯一配置动作被跳
            过，只剩 90% 守卫兜底）。现在至少做零成本动作：大白话
            事件 + 账本供需帧 + 日志升级，在途任务工作集绝不动
            （90% resource_guard 与 ALL_TENSE 兜底不变）。
          - 非生成期：维持原行为（offload 标记 + 缓存释放）。
        """
        logger.info("策略[CPU_ASSIST]: 迁移部分层到 CPU")
        if dispatcher._feature_lock_active():
            logger.warning(
                "显存预警（≥80%%）生成期：在途任务不动，仅记录供需帧"
                "（90%% 守卫与 ALL_TENSE 兜底不变）")
            try:
                from ..event_log import log_event
                from ..inference.gpu_budget import get_gpu_budget

                snap = get_gpu_budget().snapshot(0)
                log_event(
                    "system", "gpu_vram_warning_active",
                    f"显存占用超八成（空闲 {snap.free_gb:.1f}GB / "
                    f"共 {snap.total_gb:.1f}GB），正在跑的任务不受影响，"
                    "系统持续盯着，快满时会自动腾地方",
                    level="warning",
                    detail=(f"snapshot={snap!r}（生成期零成本动作："
                            "不 offload 不卸载，仅记录）"))
            except Exception as exc:  # noqa: BLE001 - 记录失败不影响调度
                logger.debug("生成期供需帧记录跳过: %s", exc)
            return
        # 迁移推理管线的后处理层到 CPU（原行为）
        dispatcher.migrate_to_cpu(layers=["vae_decode", "postprocess"])

    def _strategy_gpu_assist_cpu(self, dispatcher: TaskDispatcher) -> None:
        """GPU 受限：CPU 为主力，GPU 仅处理关键层。"""
        logger.info("策略[GPU_ASSIST_CPU]: CPU 为主力")
        dispatcher.migrate_to_cpu(layers=["attention", "ffn", "vae"])
        dispatcher.migrate_to_gpu(tasks=["embedding"])  # 仅 embedding 留在 GPU

    def _strategy_memory_pressure(self, dispatcher: TaskDispatcher) -> None:
        """内存压力：压缩不活跃的模型缓存。"""
        logger.info("策略[MEMORY_PRESSURE]: 压缩模型缓存")
        dispatcher.degrade(precision="int4")
        dispatcher.compress_cache()

    def _strategy_all_tense(self, dispatcher: TaskDispatcher) -> None:
        """全面紧张：强制降级 + 卸载非关键模型。"""
        logger.warning("策略[ALL_TENSE]: 强制降级")
        dispatcher.degrade(precision="int4")
        dispatcher.force_unload(except_features=["dialog"])  # 仅保留对话

    def _strategy_all_idle(self, dispatcher: TaskDispatcher) -> None:
        """全部空闲：按 ML 预测预加载下一个可能使用的功能。

        不再盲预加载全部功能（16GB 显卡上 8B 对话模型一载即占满显存，
        闲置时形同泄漏）。仅在预测概率超过阈值时预加载单一功能；
        无使用历史（冷启动）时不预加载，保持显存空闲。

        批5 登记簿联动（方案 §3.6）：云任务不占本地 GPU 利用率但系统
        非空闲——云任务在跑时跳过预加载（用户正用云端，预判抢装本地
        大模型只会挤占云任务完成后的本地接力窗口）。
        """
        try:
            from ..inference.gpu_budget import get_busy_registry

            if get_busy_registry().is_busy():
                logger.info(
                    "策略[ALL_IDLE]: 有任务登记在跑（含云端），跳过预加载")
                return
        except Exception as exc:  # noqa: BLE001 - 登记簿不可用按原策略
            logger.debug("忙碌登记簿检查跳过: %s", exc)
        try:
            from ..model_manager import get_model_manager
            pred = get_model_manager().predictor.predict_next()
        except Exception as exc:  # noqa: BLE001 - 预测不可用不阻断调度
            logger.debug("策略[ALL_IDLE]: 预测器不可用，跳过预加载: %s", exc)
            return
        nxt = str(pred.get("next_feature") or "")
        # 仅重度 GPU 功能值得预加载；browser/behavior 学习为后台轻量任务
        if pred.get("preload") and nxt in ("dialog", "paint", "video_gen", "manga"):
            logger.info(
                "策略[ALL_IDLE]: 预测预加载 %s (p=%.2f, engine=%s)",
                nxt, float(pred.get("probability", 0.0)), pred.get("engine"),
            )
            dispatcher.preload([nxt])
        else:
            logger.info("策略[ALL_IDLE]: 无可靠预测，保持显存空闲（不预加载）")
# 本项目仅供学习使用，商业授权请+Q 3559331368
