"""OmniSpace AI v2.1 调度引擎包（规格 §5.1 智能调度系统）。

提供 SchedulerEngine 单例，整合硬件监控、瓶颈分析、决策引擎和任务分发。
规格 §5.1: 每秒采样一次硬件状态，按6种协同模式动态调度，30秒滞回控制。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from ...config import (
    IDLE_RECLAIM_SECONDS,
    SCHEDULER_INTERVAL_MS,
    SHALLOW_RECLAIM_SECONDS,
    THRESHOLDS,
)

log = logging.getLogger("omnispace.scheduler")


class SchedulerEngine:
    """调度引擎单例——整合监控、分析、决策、分发四个子系统。

    规格 §5.1: 每秒采样一次硬件状态，按6种协同模式动态调度。
    """

    _instance: SchedulerEngine | None = None
    _lock = threading.Lock()

    def __new__(cls) -> SchedulerEngine:
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True

        # 延迟导入避免循环依赖
        from .analyzer import BottleneckAnalyzer
        from .decision import DecisionEngine
        from .dispatcher import TaskDispatcher
        from .monitor import HardwareMonitor

        self.monitor = HardwareMonitor()
        self.analyzer = BottleneckAnalyzer()
        self.decision = DecisionEngine()
        self.dispatcher = TaskDispatcher()

        # 当前协同模式
        from ...data.models import SynergyMode
        self.current_mode: SynergyMode = SynergyMode.GPU_PRIMARY

        # 后台任务句柄
        self._task: asyncio.Task | None = None
        self._running = False

        # 磁盘 IO 繁忙状态（文档B §4.1：磁盘 IO >80% → 延迟非关键写入）
        self._disk_busy: bool = False
        self._disk_busy_percent: float = 0.0

        # 两级空闲显存回收防抖标记（P2：表层 60s / 深层 300s 各执行一次）
        self._shallow_reclaimed: bool = False
        self._idle_reclaimed: bool = False

    # ── 生命周期 ──────────────────────────────────────────────────

    async def start(self) -> None:
        """启动调度引擎后台循环（规格 §5.1: 1秒采样间隔）。"""
        if self._running:
            return
        self._running = True
        interval = SCHEDULER_INTERVAL_MS / 1000.0
        self._task = asyncio.create_task(self._loop(interval))
        log.info("调度引擎已启动（采样间隔 %.1fs）", interval)

    async def stop(self) -> None:
        """停止调度引擎后台循环。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("调度引擎已停止")

    async def _loop(self, interval: float) -> None:
        """后台调度循环：周期性调用 tick()。

        审计 P2-13 修复：tick() 内含 psutil.cpu_percent(interval=0.1)
        等阻塞采集（每次 ≥100ms），原实现在事件循环内同步执行，
        每秒卡住所有 API/WS 协程 ≥100ms。现改为 run_in_executor
        投递到线程池执行，事件循环不再被采集阻塞。
        """
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                await loop.run_in_executor(None, self.tick)
            except Exception as exc:  # noqa: BLE001 - 不让循环崩溃
                log.warning("调度周期异常: %s", exc)
            await asyncio.sleep(interval)

    # ── 调度核心 ──────────────────────────────────────────────────

    def tick(self) -> None:
        """执行一次调度周期：采集 -> 热保护 -> 分析 -> 决策 -> 分发 -> 历史落库。

        注意：本方法经 run_in_executor 在线程池执行（审计 P2-13），
        内部均为线程安全调用。
        """
        # 1. 采集硬件状态
        gpu = self.monitor.get_gpu()
        cpu = self.monitor.get_cpu()
        mem = self.monitor.get_memory()
        power = self.monitor.get_power()
        disk = self.monitor.get_disk_io()

        # 1.2 文档B §4.1：磁盘 IO >80% → 延迟非关键写入。
        # 当前架构无写队列，以状态旗标形式向调度状态/API 暴露，
        # 由写入方（日志/缓存落盘）读取后自行延迟；busy 期间每周期记 debug。
        disk_busy_limit = float(THRESHOLDS.get("disk_io_busy_critical", 0.80))
        disk_busy = float(disk.get("busy_percent", 0.0)) >= disk_busy_limit
        if disk_busy and not self._disk_busy:
            log.info("磁盘 IO 占用 %.0f%% ≥ %.0f%%，非关键写入进入延迟窗口",
                     float(disk.get("busy_percent", 0.0)) * 100,
                     disk_busy_limit * 100)
        self._disk_busy = disk_busy
        self._disk_busy_percent = float(disk.get("busy_percent", 0.0))

        # 1.5 审计 P0-3：GPU 温度保护链（规格 §4.1.3）——
        # >85°C 降频（连续3次锁定利用率80%），>90°C 强制暂停生成任务。
        # 每秒随调度周期检查，独立于协同模式（温度是硬件安全底线）。
        try:
            from ..thermal_guard import get_thermal_guard
            get_thermal_guard().check(float(gpu.get("temp_celsius", 0.0)))
        except Exception as exc:  # noqa: BLE001 - 热保护异常不阻断调度
            log.debug("温度保护检查跳过: %s", exc)

        # 1.7 资源硬限制守卫（用户裁定 2026-08-22）：RAM ≤85% /
        # VRAM ≤90%。超线卸载空闲模型（活动功能与共享小模型保留），
        # 无可卸时由既有 offload 链保质量（时间换空间）。独立于
        # 协同模式滞回，守卫做最后兜底。
        try:
            from ..resource_guard import get_resource_guard
            vram_ratio = 0.0
            if int(gpu.get("vram_total_mb", 0)) > 0:
                vram_ratio = (int(gpu.get("vram_used_mb", 0))
                              / int(gpu["vram_total_mb"]))
            get_resource_guard().check(
                float(mem.get("used_percent", 0.0)), vram_ratio)
        except Exception as exc:  # noqa: BLE001 - 守卫异常不阻断调度
            log.debug("资源守卫检查跳过: %s", exc)

        # 1.6 学习调度器周期评估（R2-B02 触发器接线 / R2-B03 自动微调，
        #     内部 30s 节流，异常不阻断调度主循环）
        try:
            from ..learning_scheduler import get_learning_scheduler
            get_learning_scheduler().tick()
        except Exception as exc:  # noqa: BLE001
            log.debug("学习调度周期评估跳过: %s", exc)

        # 2. 瓶颈分析
        new_mode = self.analyzer.analyze(gpu, cpu, mem, power)

        # 2.5 文档B §4.1.2（F-10）+ P2 分级降参：GPU 利用率 >95% 持续
        # 5s/15s → 在途生成任务轻度/深度降参。analyzer 分级判定命中时
        # 置位质量总督等级（0/1/2），恢复正常后清除；绘画生成循环每
        # step 查询并提前停止（paint_engine callback_on_step_end）。
        try:
            from ..quality_governor import get_quality_governor
            get_quality_governor().set_level(
                self.analyzer.last_gpu_util_level)
        except Exception as exc:  # noqa: BLE001 - 降参旗标同步不阻断调度
            log.debug("质量总督旗标同步跳过: %s", exc)

        # 2.8 空闲显存回收：无用户活动超阈值时卸载非常驻大模型。
        # 独立于协同模式——闲置不占显存是资源底线（尤其 16GB 档位
        # 8B 对话模型一载即占满，用户离开后必须主动归还）。
        try:
            self._reclaim_idle_vram()
        except Exception as exc:  # noqa: BLE001 - 回收异常不阻断调度
            log.debug("空闲显存回收跳过: %s", exc)

        # 3. 决策引擎（带滞回控制）
        applied_mode = self.decision.evaluate(new_mode)
        if applied_mode != self.current_mode:
            previous_mode = self.current_mode
            success = True
            try:
                self.decision.execute_strategy(applied_mode, self.dispatcher)
            except Exception as exc:  # noqa: BLE001 - 策略异常不阻断调度循环
                success = False
                log.warning("调度策略执行异常(%s): %s", applied_mode.value, exc)
            self.current_mode = applied_mode
            # 4. 协同调度历史学习（文档B 第四章引擎3）：
            #    每次模式切换决策落库，供成功率/等待回归与阈值自调整。
            try:
                from .history import get_schedule_history
                vram_total = int(gpu.get("vram_total_mb", 0))
                vram_free = max(0, vram_total - int(gpu.get("vram_used_mb", 0)))
                get_schedule_history().record(
                    mode_before=previous_mode.value,
                    mode_after=applied_mode.value,
                    trigger=self.decision.last_switch_trigger or "unknown",
                    vram_free_mb=vram_free,
                    wait_ms=self.decision.last_switch_wait_ms,
                    success=success,
                )
            except Exception as exc:  # noqa: BLE001 - 学习引擎不影响主链
                log.debug("调度历史记录跳过: %s", exc)

    def _reclaim_idle_vram(self) -> None:
        """两级空闲显存回收（P2 分级，原单档 300s 粒度粗）：

          - 表层 60s：释放 embed/aux/voice 小模型（体量小、快速可重载，
            空闲即归还，避免常驻空占显存）；
          - 深层 300s：卸载非常驻大模型（dialog/paint/video 等重模型）。

        活动期间（功能锁持有 / 空闲时长不足）重置两级回收标记并返回；
        每级每段空闲期最多执行一次真实卸载，避免逐 tick 重复扫描日志。
        """
        from ...middleware.feature_lock import get_feature_lock
        lock = get_feature_lock()
        if lock.active_feature is not None:
            self._shallow_reclaimed = False
            self._idle_reclaimed = False
            return
        idle_s = lock.idle_seconds
        # 跨模块共享小模型类别（embedding 检索 / voice 语音 / auxiliary
        # 辅助），与 model_manager._SHARED_KEEP_CATEGORIES 口径一致
        shared_small = {"embedding", "voice", "auxiliary"}
        from ..model_manager import get_model_manager
        mgr = get_model_manager()

        # ── 表层（60s）：释放 embed/aux/voice 小模型 ──
        if idle_s >= SHALLOW_RECLAIM_SECONDS and not self._shallow_reclaimed:
            self._shallow_reclaimed = True
            small = [e for e in mgr.get_loaded_models()
                     if (e.get("category") or "").strip().lower() in shared_small]
            if small:
                log.info("空闲 %.0fs 达表层阈值，释放 %d 个小模型（embed/aux）: %s",
                         idle_s, len(small), [e.get("model_id") for e in small])
                for entry in small:
                    try:
                        mgr.unload_model(entry["model_id"])
                    except Exception as exc:  # noqa: BLE001
                        log.warning("表层释放失败 (%s): %s",
                                    entry.get("model_id"), exc)

        # ── 深层（300s）：卸载非常驻大模型 ──
        if idle_s >= IDLE_RECLAIM_SECONDS and not self._idle_reclaimed:
            self._idle_reclaimed = True
            # 表层已回收小模型，深层聚焦剩余重模型（排除共享小模型）
            heavy = [e for e in mgr.get_loaded_models()
                     if (e.get("category") or "").strip().lower() not in shared_small]
            if heavy:
                log.warning("空闲 %.0fs 达深层阈值，回收 %d 个驻留大模型释放显存: %s",
                            idle_s, len(heavy), [e.get("model_id") for e in heavy])
                for entry in heavy:
                    try:
                        mgr.unload_model(entry["model_id"])
                    except Exception as exc:  # noqa: BLE001
                        log.warning("深层卸载失败 (%s): %s",
                                    entry.get("model_id"), exc)
                # 同步分发器预加载记账，防止状态页误报"已预载"
                self.dispatcher._preloaded.clear()
            # 池压缩（2026-08-23 显存锚定修复）：卸载后 empty_cache
            # 无法释放被 bge 等长驻小模型活跃块钉死的大 segment
            # （实测 loaded=0 仍锚定 15.3GB 物理）——bge 停靠 CPU →
            # 清池全段归还 → 回卡，物理显存才真正回到系统。
            # 台账为空同样执行（卸载已发生但池仍臃肿的场景）
            try:
                from ..model_manager import deflate_cuda_pool
                deflate_cuda_pool()
            except Exception as exc:  # noqa: BLE001
                log.debug("深回收后池压缩跳过: %s", exc)

    def get_state(self) -> dict:
        """返回当前调度状态快照（对齐 SchedulerState 模型全部字段）。

        属性映射：
          - active_model: dispatcher._preloaded 中第一个（当前活动模型）
          - cached_models: dispatcher._preloaded 全列表（缓存模型）
          - last_switch_ms: decision._last_switch_time 转毫秒
        """
        gpu = self.monitor.get_gpu()
        cpu = self.monitor.get_cpu()
        mem = self.monitor.get_memory()

        # 从 dispatcher 获取预加载模型列表（对齐属性名）
        preloaded = getattr(self.dispatcher, "_preloaded", []) or []
        active_model = preloaded[0] if preloaded else None
        cached_models = list(preloaded)

        # 从 decision 获取上次切换时间（对齐属性名）
        last_switch_time = getattr(self.decision, "_last_switch_time", 0.0) or 0.0
        last_switch_ms = int((time.time() - last_switch_time) * 1000) if last_switch_time > 0 else 0

        return {
            "mode": self.current_mode.value,
            # 审计 BK-027 修复：monitor 的键是 util_percent / usage_percent，
            # 原误读 util/usage_percent 导致 gpu_usage 恒为 0.0
            "gpu_usage": gpu.get("util_percent", 0.0),
            "cpu_usage": cpu.get("usage_percent", 0.0),
            "mem_available_gb": mem.get("available_gb", 0.0),
            "active_model": active_model,
            "cached_models": cached_models,
            "last_switch_ms": last_switch_ms,
            # 文档B §4.1 磁盘 IO 繁忙旗标（>80% 时写入方应延迟非关键写入）
            "disk_io_busy": self._disk_busy,
            "disk_io_busy_percent": self._disk_busy_percent,
        }


# ── 模块级单例 ──────────────────────────────────────────────────
_scheduler_instance: SchedulerEngine | None = None
_singleton_lock = threading.Lock()


def get_scheduler() -> SchedulerEngine:
    """获取 SchedulerEngine 全局单例。"""
    global _scheduler_instance
    if _scheduler_instance is None:
        with _singleton_lock:
            if _scheduler_instance is None:
                _scheduler_instance = SchedulerEngine()
    return _scheduler_instance
