"""OmniSpace AI v2.1 VRAM 管理器（规格 §5.1 GPU 显存管理）。

跟踪显存分配/释放，提供临时缓存和强制卸载能力。
当 CUDA/torch 不可用时退化为纯记账模式。

**定位标注（显存调度机制批5，2026-09-10）**：本模块为**遥测兼容层**
——track_alloc/track_free 仅服务 get_usage 遥测展示，无任何决策消费方；
显存调度的决策真源 = services/inference/gpu_budget（物理读数+台账+
预留单账本，docs/显存调度机制方案-2026-09-10.md §3.1）。新代码禁止
基于本模块做装载/驱逐/准入判定（四本账并存的历史病灶之一）。
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

from ..config import GPU_PRIMARY_DEVICE, THRESHOLDS

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
    # 分配归属卡（批1 多卡地基 2026-09-05；缺省=主卡，旧调用方不变）
    device: int = GPU_PRIMARY_DEVICE


class VramManager:
    """VRAM 管理器——跟踪显存分配/释放，提供缓存与卸载。"""

    def __init__(self) -> None:
        self._allocations: dict[str, VramAllocation] = {}
        self._total_allocated_mb: float = 0.0
        # 按卡记账（批1 多卡地基 2026-09-05）：device → 记账 MB。
        # 单卡机器只有主卡桶，数值与旧全局口径一致。
        self._device_allocated_mb: dict[int, float] = {}
        self._lock = threading.Lock()
        self._cuda_available = _torch is not None and _torch.cuda.is_available()
        # 探测降级记录（P1-05 吞错治理：探测失败显式记录并经 get_usage 暴露，
        # 不再静默吞掉——显存守门人必须可观测）
        self._degraded_probes: dict[str, str] = {}

        # 获取总显存（批1：逐卡探测全部设备；主卡总量仍存
        # _vram_total_mb 兼容旧消费方，单卡机器数值不变）
        self._vram_totals: dict[int, float] = {}
        self._vram_total_mb: float = 0.0
        if self._cuda_available:
            try:
                for _i in range(_torch.cuda.device_count()):
                    try:
                        self._vram_totals[_i] = _torch.cuda.get_device_properties(
                            _i).total_memory / (1024 * 1024)
                    except Exception as exc:
                        self._degraded_probes[f"vram_total_mb[{_i}]"] = str(exc)
                        logger.warning("CUDA 卡 %d 总显存探测失败: %s", _i, exc)
            except Exception as exc:
                # 外层失败沿用历史降级键 vram_total_mb（P1-05 契约键名，
                # 遥测诚实性哨兵锚定）：设备数都探不到=总显存不可知
                self._degraded_probes["vram_total_mb"] = str(exc)
                logger.warning(
                    "CUDA 设备数探测失败（总显存记 0，显存门禁将退化为不可用）: %s",
                    exc)
            self._vram_total_mb = self._vram_totals.get(GPU_PRIMARY_DEVICE, 0.0)

        logger.info(
            "VRAM 管理器初始化: CUDA=%s, 总显存=%.0fMB, 卡数=%d, 降级探测=%s",
            self._cuda_available,
            self._vram_total_mb,
            len(self._vram_totals),
            list(self._degraded_probes) or "无",
        )

    # ── 分配/释放跟踪 ──────────────────────────────────────────

    def track_alloc(self, key: str, size_mb: float, temporary: bool = False,
                    device: int | None = None) -> None:
        """跟踪一次显存分配。

        Args:
            key: 分配标识（如 model_id 或 tensor name）
            size_mb: 分配大小（MB）
            temporary: 是否为临时分配（可被优先回收）
            device: 归属卡索引（None=主卡；批1 多卡地基）
        """
        dev = GPU_PRIMARY_DEVICE if device is None else int(device)
        with self._lock:
            # 审计 09-10 P1-20：同 key 重复登记按「替换」语义先扣旧值——
            # 此前直接覆盖条目但总额照加，旧 size 永久泄漏在账本里
            old = self._allocations.get(key)
            if old is not None:
                self._total_allocated_mb -= old.size_mb
                self._device_allocated_mb[old.device] = max(
                    0.0,
                    self._device_allocated_mb.get(old.device, 0.0)
                    - old.size_mb)
                logger.warning(
                    "VRAM 分配重复登记（按替换扣旧值）: %s 旧=%.1fMB 新=%.1fMB",
                    key, old.size_mb, size_mb)
            self._allocations[key] = VramAllocation(
                key=key, size_mb=size_mb, temporary=temporary, device=dev
            )
            self._total_allocated_mb += size_mb
            self._device_allocated_mb[dev] = (
                self._device_allocated_mb.get(dev, 0.0) + size_mb)
            logger.debug(
                "VRAM 分配: %s (%.1fMB, 卡=%d, 总计 %.1fMB)",
                key,
                size_mb,
                dev,
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
            self._device_allocated_mb[alloc.device] = max(
                0.0,
                self._device_allocated_mb.get(alloc.device, 0.0)
                - alloc.size_mb)
            logger.debug(
                "VRAM 释放: %s (%.1fMB, 卡=%d, 剩余 %.1fMB)",
                key,
                alloc.size_mb,
                alloc.device,
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

    def reset_bookkeeping(self, except_keys: list[str] | None = None,
                          device: int | None = None) -> int:
        """仅重置显存记账并 empty_cache，不卸载模型（审计 R3-BE4 重命名）。

        本方法只清理内部 _allocations 记账并调用 torch.cuda.empty_cache，
        不持有也不释放任何模型引用张量。真正的模型强制卸载见
        services/scheduler/dispatcher.py 的 force_unload
        （真实接线 ModelManager.unload_model）。

        Args:
            except_keys: 保留的分配标识列表
            device: 只重置该卡的记账（None=全部卡；批1 多卡地基）

        Returns:
            记账中清除的显存总量（MB）
        """
        keep = set(except_keys or [])
        freed_mb = 0.0

        def _owned(alloc: VramAllocation) -> bool:
            return (alloc.key not in keep
                    and (device is None or alloc.device == int(device)))

        with self._lock:
            # 先清除临时分配记账
            temp_keys = [k for k, v in self._allocations.items()
                         if v.temporary and _owned(v)]
            for key in temp_keys:
                alloc = self._allocations.pop(key)
                freed_mb += alloc.size_mb
                self._total_allocated_mb -= alloc.size_mb
                self._device_allocated_mb[alloc.device] = max(
                    0.0,
                    self._device_allocated_mb.get(alloc.device, 0.0)
                    - alloc.size_mb)

            # 如果还不够，清除非临时分配记账
            if freed_mb == 0:
                non_temp_keys = [k for k, v in self._allocations.items()
                                 if _owned(v)]
                for key in non_temp_keys:
                    alloc = self._allocations.pop(key)
                    freed_mb += alloc.size_mb
                    self._total_allocated_mb -= alloc.size_mb
                    self._device_allocated_mb[alloc.device] = max(
                        0.0,
                        self._device_allocated_mb.get(alloc.device, 0.0)
                        - alloc.size_mb)

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
            logger.warning("重置显存记账: %.1fMB (保留: %s, 卡=%s)",
                           freed_mb, list(keep), "全部" if device is None else device)
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

    def get_usage(self, device: int | None = None) -> dict:
        """返回显存使用情况（device=None 为主卡，口径与历史一致）。

        P1-05：额外携带 degraded_probes（探测失败的项与最后错误），
        上层可据此判断读数是真实探测还是纯记账退化值。
        批1 多卡地基：附带 devices 全卡分解列表（附加键，旧消费方无感）。
        """
        dev = GPU_PRIMARY_DEVICE if device is None else int(device)
        # 获取实际显存使用（如果 CUDA 可用）
        total_mb = self._vram_totals.get(dev, self._vram_total_mb if dev == GPU_PRIMARY_DEVICE else 0.0)
        actual_used_mb = self._device_allocated_mb.get(dev, 0.0)
        actual_free_mb = 0.0

        if self._cuda_available:
            try:
                mem = _torch.cuda.memory_allocated(dev) / (1024 * 1024)
                actual_used_mb = max(actual_used_mb, mem)
                actual_free_mb = total_mb - actual_used_mb
            except Exception as exc:
                self._degraded_probes["memory_allocated"] = str(exc)
                logger.warning(
                    "CUDA 实际用量探测失败，使用量为纯记账值（可能低估）: %s", exc)

        usage_ratio = actual_used_mb / total_mb if total_mb > 0 else 0.0

        # 全卡分解（单卡机器恰一个元素，数值与主卡口径一致）
        devices: list[dict] = []
        for idx in sorted(set(self._vram_totals)
                          | set(self._device_allocated_mb)):
            try:
                t = self._vram_totals.get(idx, 0.0)
                u = self._device_allocated_mb.get(idx, 0.0)
                if self._cuda_available:
                    try:
                        u = max(u, _torch.cuda.memory_allocated(idx) / (1024 * 1024))
                    except Exception:  # noqa: BLE001 - 单卡探测失败按记账值
                        logger.debug("get_usage: 降级忽略", exc_info=True)
                devices.append({
                    "index": idx,
                    "total_mb": t,
                    "used_mb": u,
                    "free_mb": max(0.0, t - u) if t > 0 else 0.0,
                    "usage_ratio": (u / t) if t > 0 else 0.0,
                })
            except Exception:  # noqa: BLE001 - 单卡分解失败不阻断
                continue

        return {
            "total_mb": total_mb,
            "used_mb": actual_used_mb,
            "free_mb": actual_free_mb,
            "usage_ratio": usage_ratio,
            "tracked_allocations": len(self._allocations),
            "is_critical": usage_ratio >= THRESHOLDS["gpu_vram_critical"],
            "should_force_unload": usage_ratio >= THRESHOLDS["gpu_vram_force_unload"],
            "degraded_probes": dict(self._degraded_probes),
            "device": dev,
            "devices": devices,
        }

    def get_device_usage(self, device: int) -> dict:
        """单卡显存快照（批1 多卡地基；shape 与 get_usage 一致）。"""
        return self.get_usage(int(device))

    def get_available_mb(self, device: int | None = None) -> float:
        """返回可用显存（MB）（device=None 为主卡，口径与历史一致）。"""
        dev = GPU_PRIMARY_DEVICE if device is None else int(device)
        if self._cuda_available:
            try:
                free = _torch.cuda.mem_get_info(dev)[0] / (1024 * 1024)
                return free
            except Exception as exc:
                self._degraded_probes["mem_get_info"] = str(exc)
                logger.warning(
                    "CUDA 空闲显存探测失败，退化为记账差值（可能不准）: %s", exc)
        total_mb = self._vram_totals.get(dev, self._vram_total_mb if dev == GPU_PRIMARY_DEVICE else 0.0)
        return max(0.0, total_mb - self._device_allocated_mb.get(dev, 0.0))

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
# 本项目仅供学习使用，商业授权请+Q 3559331368
