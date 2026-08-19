"""OmniSpace AI v2.1 VRAM 管理器（规格 §5.1 GPU 显存管理）。

跟踪显存分配/释放，提供临时缓存和强制卸载能力。
当 CUDA/torch 不可用时退化为纯记账模式。
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from ..config import THRESHOLDS

logger = logging.getLogger("omnispace.engines.vram")


def _try_import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_torch = _try_import("torch")
_pynvml = _try_import("pynvml")


@dataclass
class VramAllocation:
    """单次显存分配记录。"""
    key: str
    size_mb: float
    allocated_at: float = field(default_factory=time.time)
    temporary: bool = False


class VramManager:
    """VRAM 管理器——跟踪显存分配/释放，提供缓存与卸载。"""

    def __init__(self) -> None:
        self._allocations: dict[str, VramAllocation] = {}
        self._total_allocated_mb: float = 0.0
        self._lock = threading.Lock()
        self._cuda_available = _torch is not None and _torch.cuda.is_available()
        # 探测降级记录（P1-05 吞错治理：探测失败显式记录并经 get_usage 暴露，
        # 不再静默吞掉——显存守门人必须可观测）
        self._degraded_probes: dict[str, str] = {}

        # 获取总显存
        self._vram_total_mb: float = 0.0
        if self._cuda_available:
            try:
                self._vram_total_mb = _torch.cuda.get_device_properties(0).total_memory / (1024 * 1024)
            except Exception as exc:
                self._degraded_probes["vram_total_mb"] = str(exc)
                logger.warning(
                    "CUDA 总显存探测失败（总显存记 0，显存门禁将退化为不可用）: %s", exc)

        logger.info(
            "VRAM 管理器初始化: CUDA=%s, 总显存=%.0fMB, 降级探测=%s",
            self._cuda_available,
            self._vram_total_mb,
            list(self._degraded_probes) or "无",
        )

    # ── 分配/释放跟踪 ──────────────────────────────────────────

    def track_alloc(self, key: str, size_mb: float, temporary: bool = False) -> None:
        """跟踪一次显存分配。

        Args:
            key: 分配标识（如 model_id 或 tensor name）
            size_mb: 分配大小（MB）
            temporary: 是否为临时分配（可被优先回收）
        """
        with self._lock:
            self._allocations[key] = VramAllocation(
                key=key, size_mb=size_mb, temporary=temporary
            )
            self._total_allocated_mb += size_mb
            logger.debug(
                "VRAM 分配: %s (%.1fMB, 总计 %.1fMB)",
                key,
                size_mb,
                self._total_allocated_mb,
            )

    def track_free(self, key: str) -> float:
        """跟踪一次显存释放。

        Args:
            key: 分配标识

        Returns:
            释放的大小（MB），如果 key 不存在返回 0
        """
        with self._lock:
            alloc = self._allocations.pop(key, None)
            if alloc is None:
                return 0.0
            self._total_allocated_mb -= alloc.size_mb
            logger.debug(
                "VRAM 释放: %s (%.1fMB, 剩余 %.1fMB)",
                key,
                alloc.size_mb,
                self._total_allocated_mb,
            )
            return alloc.size_mb

    # ── 临时缓存 ────────────────────────────────────────────────

    @contextmanager
    def temporary_cache(self, key: str, size_mb: float) -> Generator[None, None, None]:
        """临时缓存上下文管理器。

        用法:
            with vram_manager.temporary_cache("temp_tensor", 512):
                # 使用显存
                ...
            # 离开上下文后自动释放

        Args:
            key: 缓存标识
            size_mb: 缓存大小（MB）
        """
        self.track_alloc(key, size_mb, temporary=True)
        try:
            yield
        finally:
            self.track_free(key)

    # ── 记账重置 ────────────────────────────────────────────────

    def reset_bookkeeping(self, except_keys: list[str] | None = None) -> int:
        """仅重置显存记账并 empty_cache，不卸载模型（审计 R3-BE4 重命名）。

        本方法只清理内部 _allocations 记账并调用 torch.cuda.empty_cache，
        不持有也不释放任何模型引用张量。真正的模型强制卸载见
        services/scheduler/dispatcher.py 的 force_unload
        （真实接线 ModelManager.unload_model）。

        Args:
            except_keys: 保留的分配标识列表

        Returns:
            记账中清除的显存总量（MB）
        """
        keep = set(except_keys or [])
        freed_mb = 0.0

        with self._lock:
            # 先清除临时分配记账
            temp_keys = [k for k, v in self._allocations.items() if v.temporary and k not in keep]
            for key in temp_keys:
                alloc = self._allocations.pop(key)
                freed_mb += alloc.size_mb
                self._total_allocated_mb -= alloc.size_mb

            # 如果还不够，清除非临时分配记账
            if freed_mb == 0:
                non_temp_keys = [k for k in self._allocations if k not in keep]
                for key in non_temp_keys:
                    alloc = self._allocations.pop(key)
                    freed_mb += alloc.size_mb
                    self._total_allocated_mb -= alloc.size_mb

        # 如果 CUDA 可用，清空缓存
        if self._cuda_available and freed_mb > 0:
            try:
                _torch.cuda.empty_cache()
                logger.info("已清空 CUDA 缓存")
            except Exception as exc:
                self._degraded_probes["empty_cache"] = str(exc)
                logger.warning(
                    "CUDA 缓存清空失败（记账已重置，物理显存可能未真正回收）: %s", exc)

        if freed_mb > 0:
            logger.warning("重置显存记账: %.1fMB (保留: %s)", freed_mb, list(keep))
        return int(freed_mb)

    def force_unload(self, except_keys: list[str] | None = None) -> int:
        """已废弃别名：等价 reset_bookkeeping（审计 R3-BE4）。

        原名语义误导——本方法并不卸载模型，仅重置记账 + empty_cache。
        强制卸载模型请使用 services/scheduler/dispatcher.py 的
        force_unload（真实接线 ModelManager.unload_model）。
        """
        logger.warning(
            "DeprecationWarning: VramManager.force_unload 已更名为 "
            "reset_bookkeeping（仅重置记账，不卸载模型）；"
            "强制卸载模型见 services/scheduler/dispatcher.py force_unload")
        return self.reset_bookkeeping(except_keys=except_keys)

    # ── 状态查询 ────────────────────────────────────────────────

    def get_usage(self) -> dict:
        """返回显存使用情况。

        P1-05：额外携带 degraded_probes（探测失败的项与最后错误），
        上层可据此判断读数是真实探测还是纯记账退化值。
        """
        # 获取实际显存使用（如果 CUDA 可用）
        actual_used_mb = self._total_allocated_mb
        actual_free_mb = 0.0

        if self._cuda_available:
            try:
                mem = _torch.cuda.memory_allocated(0) / (1024 * 1024)
                actual_used_mb = max(actual_used_mb, mem)
                actual_free_mb = self._vram_total_mb - actual_used_mb
            except Exception as exc:
                self._degraded_probes["memory_allocated"] = str(exc)
                logger.warning(
                    "CUDA 实际用量探测失败，使用量为纯记账值（可能低估）: %s", exc)

        usage_ratio = actual_used_mb / self._vram_total_mb if self._vram_total_mb > 0 else 0.0

        return {
            "total_mb": self._vram_total_mb,
            "used_mb": actual_used_mb,
            "free_mb": actual_free_mb,
            "usage_ratio": usage_ratio,
            "tracked_allocations": len(self._allocations),
            "is_critical": usage_ratio >= THRESHOLDS["gpu_vram_critical"],
            "should_force_unload": usage_ratio >= THRESHOLDS["gpu_vram_force_unload"],
            "degraded_probes": dict(self._degraded_probes),
        }

    def get_available_mb(self) -> float:
        """返回可用显存（MB）。"""
        if self._cuda_available:
            try:
                free = _torch.cuda.mem_get_info(0)[0] / (1024 * 1024)
                return free
            except Exception as exc:
                self._degraded_probes["mem_get_info"] = str(exc)
                logger.warning(
                    "CUDA 空闲显存探测失败，退化为记账差值（可能不准）: %s", exc)
        return max(0, self._vram_total_mb - self._total_allocated_mb)

    def get_available_gb(self) -> float:
        """返回可用显存（GB）。"""
        return self.get_available_mb() / 1024.0


# ── 模块级单例 ──────────────────────────────────────────────────
_vram_manager: VramManager | None = None


def get_vram_manager() -> VramManager:
    """获取 VramManager 全局单例。"""
    global _vram_manager
    if _vram_manager is None:
        _vram_manager = VramManager()
    return _vram_manager
