"""功能级互斥锁（规格 §6.1 大模型功能互斥状态机）。

四种重量级 AI 功能互斥：dialog / paint / video_gen / training。
同一时刻仅允许一类在运行，其余被阻断并返回 40007。

非大模型功能（分镜表编辑、资产浏览、项目设置、历史查看、模型管理、性能监控）
始终可用，不受本锁约束。

这是 v2.1 中间件层实现（替代已废弃的 v1.0 backend/core/feature_lock.py）。
使用进程内内存状态，单机本地运行（规格 §14 约束1：不上云）。
"""
from __future__ import annotations

import asyncio
import logging
import time

from .error_handler import ApiError

log = logging.getLogger("omnispace.feature_lock")

# ── 互斥规则：每个功能阻断其余三类 ──────────────────────────────
_VALID_FEATURES = ("dialog", "paint", "video_gen", "training")

_BLOCKED: dict[str, list[str]] = {
    "dialog": ["paint", "video_gen", "training"],
    "paint": ["dialog", "video_gen", "training"],
    "video_gen": ["dialog", "paint", "training"],
    "training": ["dialog", "paint", "video_gen"],
}

_BLOCK_MESSAGES: dict[str, str] = {
    "dialog": "当前正在使用对话功能，请结束对话后再使用此功能",
    "paint": "绘画进行中，其他AI功能暂不可用",
    "video_gen": "视频生成中，请等待完成后再切换",
    "training": "训练进行中，其他AI功能暂不可用",
}


class FeatureLockManager:
    """功能级互斥管理器（进程内单例，异步安全）。

    同一功能可重入（允许多次 acquire），跨功能互斥。
    持有者信息用于返回友好的阻断提示。
    """

    _instance: FeatureLockManager | None = None

    def __init__(self) -> None:
        self._holder: str | None = None
        self._acquired_at: float = 0.0
        self._holder_task_id: str | None = None
        # 同功能重入计数（审计 P1-1 修复，2026-08-29）：此前同功能
        # 第二次 acquire 成功后，先完成者的 release 会把锁整个清空，
        # 另一任务仍在运行时跨功能互斥即告失效（视频双任务并发生成
        # 实证路径）。release 递减，归零才真正放锁。
        # 兼容注：lora_training_service 的同步降级路径直写 `_holder`
        #（不经本计数）——release 时 max(0, …) 钳制，终态与旧行为一致。
        self._hold_count = 0
        self._lock = asyncio.Lock()
        # 最近一次用户功能活动时间（acquire/release 均刷新），
        # 供调度器空闲显存回收判定；进程启动即开始计空闲。
        self._last_activity_at: float = time.time()

    @classmethod
    def instance(cls) -> FeatureLockManager:
        """获取单例（无需 await，单线程事件循环下安全）。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def active_feature(self) -> str | None:
        return self._holder

    @property
    def idle_seconds(self) -> float:
        """距最近一次功能活动的秒数（有锁持有时为 0）。"""
        if self._holder is not None:
            return 0.0
        return max(0.0, time.time() - self._last_activity_at)

    def get_block_reason(self, feature: str) -> str | None:
        """如果 feature 被阻断，返回阻断原因消息；否则返回 None。"""
        if self._holder is None or self._holder == feature:
            return None
        if feature in _BLOCKED.get(self._holder, []):
            return _BLOCK_MESSAGES.get(
                self._holder, f"{self._holder} 进行中，请稍后再试"
            )
        return None

    async def acquire(self, feature: str,
                      task_id: str | None = None) -> bool:
        """尝试获取功能锁，成功返回 True，被阻断返回 False。

        同一功能可重入：当 _holder == feature 时直接成功。
        """
        if feature not in _VALID_FEATURES:
            raise ApiError(40010, f"无效的 feature：{feature}",
                           detail={"feature": feature,
                                   "valid": list(_VALID_FEATURES)})
        async with self._lock:
            if self._holder is not None and self._holder != feature:
                return False
            if self._holder == feature:
                self._hold_count += 1
            else:
                self._holder = feature
                self._hold_count = 1
                self._acquired_at = time.time()
            self._holder_task_id = task_id
            self._last_activity_at = self._acquired_at
            log.info("功能锁获取: %s (task=%s, 重入=%d)",
                     feature, task_id, self._hold_count)
            return True

    async def release(self, feature: str) -> None:
        """释放功能锁（仅当当前持有者与 feature 一致时）。

        计数式释放（审计 P1-1）：同功能多次 acquire 须等额 release，
        计数归零才放锁——先完成的请求不再提前瓦解互斥。
        """
        async with self._lock:
            if self._holder == feature:
                held = time.time() - self._acquired_at
                self._hold_count = max(0, self._hold_count - 1)
                if self._hold_count == 0:
                    log.info("功能锁释放: %s (持有 %.1fs)", feature, held)
                    self._holder = None
                    self._holder_task_id = None
                    self._last_activity_at = time.time()
                else:
                    log.info("功能锁递减: %s (剩余重入=%d)",
                             feature, self._hold_count)

    def status(self) -> dict:
        """返回当前互斥状态快照。"""
        return {
            "active_feature": self._holder,
            "held_seconds": round(time.time() - self._acquired_at, 1)
            if self._holder else 0,
            "task_id": self._holder_task_id,
        }


def get_feature_lock() -> FeatureLockManager:
    """获取功能锁单例。"""
    return FeatureLockManager.instance()


async def acquire_or_raise(feature: str,
                           task_id: str | None = None) -> FeatureLockManager:
    """获取功能锁；被阻断时抛 ApiError(40007)。

    供 API 端点使用：
        lock = await acquire_or_raise("dialog")
        try:
            ... do work ...
        finally:
            await lock.release("dialog")

    审计 P0-3：GPU 温度 >= 临界值（90°C）时热保护暂停生效——
    一切新重量级任务在锁入口统一拒绝（20004），进行中的任务由
    调度器 ALL_TENSE 强制卸载兜底中断（规格 §4.1.3 / TASK-016）。
    """
    # 热保护检查（惰性 import 避免 middleware->services 循环依赖）
    try:
        from ..services.thermal_guard import get_thermal_guard
        guard = get_thermal_guard()
        if guard.is_paused():
            status = guard.get_status()
            raise ApiError(
                20004,
                f"GPU 温度过高（{status['last_temp_celsius']:.0f}°C），"
                f"已强制暂停生成任务，请等待散热后重试",
                detail={"feature": feature,
                        "temp_celsius": status["last_temp_celsius"],
                        "critical_threshold": status["critical_threshold"]})
    except ApiError:
        raise
    except Exception:  # noqa: BLE001 - 热保护探测失败不阻断正常流程
        pass

    mgr = get_feature_lock()
    success = await mgr.acquire(feature, task_id=task_id)
    if not success:
        reason = mgr.get_block_reason(feature) or "当前已有其他AI功能运行中"
        raise ApiError(40007, reason,
                       detail={"active_feature": mgr.active_feature,
                               "feature": feature})
    return mgr
