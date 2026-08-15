"""OmniSpace AI v2.1 内存管理器（规格 §5.1 内存管理）。

管理系统内存（RAM），提供不活跃内存检测、压缩和释放能力。
当内存压力时，压缩或卸载不活跃的模型缓存。
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import psutil

from ..config import THRESHOLDS, CACHE_COMPRESSION

logger = logging.getLogger("omnispace.engines.memory")


def _try_import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_lz4_block = _try_import("lz4.block")


@dataclass
class MemoryBlock:
    """内存块记录。"""
    key: str
    size_mb: float
    last_access: float = field(default_factory=time.time)
    compressed: bool = False
    data: Any = None


class MemoryManager:
    """内存管理器——不活跃检测 + 压缩 + 释放。"""

    def __init__(self) -> None:
        self._blocks: OrderedDict[str, MemoryBlock] = OrderedDict()
        self._total_tracked_mb: float = 0.0
        self._lock = threading.Lock()

        self._compression_enabled = CACHE_COMPRESSION == "lz4" and _lz4_block is not None
        if not self._compression_enabled and CACHE_COMPRESSION == "lz4":
            logger.warning("lz4 不可用，内存压缩已禁用")

        # 获取系统内存
        vm = psutil.virtual_memory()
        self._total_ram_gb = vm.total / (1024 ** 3)

        logger.info(
            "内存管理器初始化: 总 RAM=%.1fGB, 压缩=%s",
            self._total_ram_gb,
            self._compression_enabled,
        )

    # ── 内存块管理 ──────────────────────────────────────────────

    def register(self, key: str, data: Any, size_mb: float) -> None:
        """注册一个内存块。

        Args:
            key: 块标识（如 model_id）
            data: 数据引用
            size_mb: 数据大小（MB）
        """
        with self._lock:
            if key in self._blocks:
                self._total_tracked_mb -= self._blocks[key].size_mb
            self._blocks[key] = MemoryBlock(key=key, size_mb=size_mb, data=data)
            self._blocks.move_to_end(key)
            self._total_tracked_mb += size_mb
            logger.debug("内存注册: %s (%.1fMB)", key, size_mb)

    def access(self, key: str) -> Any:
        """访问内存块（更新访问时间）。"""
        with self._lock:
            block = self._blocks.get(key)
            if block is None:
                return None
            block.last_access = time.time()
            self._blocks.move_to_end(key)
            return block.data

    def unregister(self, key: str) -> bool:
        """注销内存块。"""
        with self._lock:
            block = self._blocks.pop(key, None)
            if block is None:
                return False
            self._total_tracked_mb -= block.size_mb
            return True

    # ── 不活跃检测 ──────────────────────────────────────────────

    def get_inactive(self, idle_threshold_seconds: float = 300) -> List[str]:
        """获取不活跃的内存块列表。

        规格 §5.1: 检测超过指定时间未访问的内存块，
        这些块是压缩/释放的候选对象。

        Args:
            idle_threshold_seconds: 空闲阈值（秒），默认 5 分钟

        Returns:
            不活跃的块标识列表（按空闲时间降序）
        """
        now = time.time()
        with self._lock:
            inactive = [
                (key, block)
                for key, block in self._blocks.items()
                if (now - block.last_access) > idle_threshold_seconds
            ]
            # 按空闲时间降序排序
            inactive.sort(key=lambda x: x[1].last_access)

        result = [key for key, _ in inactive]
        if result:
            logger.debug("检测到 %d 个不活跃内存块", len(result))
        return result

    # ── 压缩 ────────────────────────────────────────────────────

    def compress_inactive(self, algorithm: str = "lz4", idle_threshold_seconds: float = 300) -> int:
        """压缩不活跃的内存块。

        规格 §5.1 MEMORY_PRESSURE 模式:
          使用指定算法压缩不活跃的内存块以释放空间。

        Args:
            algorithm: 压缩算法 ('lz4' | 'zstd' | 'none')
            idle_threshold_seconds: 空闲阈值（秒）

        Returns:
            被压缩的块数
        """
        if not self._compression_enabled or algorithm == "none":
            logger.debug("压缩未启用 (algorithm=%s)", algorithm)
            return 0

        inactive_keys = self.get_inactive(idle_threshold_seconds)
        count = 0

        with self._lock:
            for key in inactive_keys:
                block = self._blocks.get(key)
                if block is None or block.compressed:
                    continue
                try:
                    import pickle
                    raw = pickle.dumps(block.data)
                    compressed = _lz4_block.compress(raw)
                    block.data = compressed
                    block.compressed = True
                    count += 1
                except Exception as e:
                    logger.warning("压缩失败 %s: %s", key, e)

        if count > 0:
            logger.info("压缩 %d 个不活跃内存块 (算法=%s)", count, algorithm)
        return count

    # ── 释放 ────────────────────────────────────────────────────

    def release(self, keys: Optional[List[str]] = None, idle_threshold_seconds: float = 600) -> int:
        """释放内存块。

        规格 §5.1: 释放指定或不活跃超过阈值的内存块。
        释放后数据不可恢复（需重新从磁盘加载）。

        Args:
            keys: 指定释放的块标识列表（None 则释放所有不活跃块）
            idle_threshold_seconds: 空闲阈值（秒），仅在 keys=None 时使用

        Returns:
            释放的块数
        """
        if keys is None:
            keys = self.get_inactive(idle_threshold_seconds)

        count = 0
        with self._lock:
            for key in keys:
                block = self._blocks.pop(key, None)
                if block is not None:
                    self._total_tracked_mb -= block.size_mb
                    block.data = None  # 释放引用
                    count += 1

        if count > 0:
            logger.info("释放 %d 个内存块", count)

        # 触发 Python 垃圾回收
        if count > 5:
            import gc
            collected = gc.collect()
            logger.debug("GC 回收: %d 对象", collected)

        return count

    # ── 状态查询 ────────────────────────────────────────────────

    def get_status(self) -> dict:
        """返回内存管理状态。"""
        vm = psutil.virtual_memory()
        available_gb = vm.available / (1024 ** 3)
        total_gb = vm.total / (1024 ** 3)
        used_ratio = vm.used / vm.total

        with self._lock:
            tracked_count = len(self._blocks)
            compressed_count = sum(1 for b in self._blocks.values() if b.compressed)

        return {
            "total_ram_gb": round(total_gb, 2),
            "available_ram_gb": round(available_gb, 2),
            "used_ratio": round(used_ratio, 3),
            "tracked_blocks": tracked_count,
            "tracked_mb": round(self._total_tracked_mb, 1),
            "compressed_blocks": compressed_count,
            "compression_enabled": self._compression_enabled,
            "is_warning": available_gb / total_gb < THRESHOLDS["mem_warning_ratio"],
            "is_critical": available_gb / total_gb < THRESHOLDS["mem_critical_ratio"],
        }

    def get_available_gb(self) -> float:
        """返回可用内存（GB）。"""
        vm = psutil.virtual_memory()
        return vm.available / (1024 ** 3)


# ── 模块级单例 ──────────────────────────────────────────────────
_memory_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    """获取 MemoryManager 全局单例。"""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
    return _memory_manager
