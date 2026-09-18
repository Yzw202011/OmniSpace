"""OmniSpace AI v2.1 模型内存缓存模块（规格 §5.1 缓存管理）。

LRU 淘汰 + LZ4 压缩策略，三种缓存状态:
  - keep_in_ram: 常驻内存（不压缩）
  - compress_lz4: LZ4 压缩存储
  - full_unload: 完全卸载到磁盘

当 lz4 不可用时降级为不压缩。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import importlib
import logging
import time
from collections import OrderedDict
from typing import Any

from ...config import CACHE_COMPRESSION, CACHE_EVICTION

log = logging.getLogger("omnispace.model_manager.cache")


def _try_import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_lz4 = _try_import("lz4")
_lz4_block = _try_import("lz4.block")


# ── 缓存条目状态 ────────────────────────────────────────────────

class CacheState:
    """缓存条目的三种状态。"""
    KEEP_IN_RAM = "keep_in_ram"        # 常驻内存
    COMPRESS_LZ4 = "compress_lz4"      # LZ4 压缩
    FULL_UNLOAD = "full_unload"        # 完全卸载


class CacheEntry:
    """单个缓存条目。"""

    __slots__ = ("key", "data", "state", "size_bytes", "last_access", "access_count")

    def __init__(self, key: str, data: Any, size_bytes: int = 0) -> None:
        self.key = key
        self.data = data
        self.state = CacheState.KEEP_IN_RAM
        self.size_bytes = size_bytes
        self.last_access = time.time()
        self.access_count = 1

    def touch(self) -> None:
        """更新访问时间。"""
        self.last_access = time.time()
        self.access_count += 1


class ModelCache:
    """模型内存缓存——LRU 淘汰 + LZ4 压缩。"""

    def __init__(
        self,
        max_entries: int = 16,
        max_size_gb: float = 0.0,  # 0 = 自动（可用 RAM - 4GB）
    ) -> None:
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_entries = max_entries
        self._max_size_gb = max_size_gb
        self._current_size_bytes = 0
        self._compression_enabled = CACHE_COMPRESSION == "lz4" and _lz4_block is not None
        self._eviction_policy = CACHE_EVICTION

        if not self._compression_enabled and CACHE_COMPRESSION == "lz4":
            log.warning("lz4 不可用，缓存压缩已禁用")

        # 自动计算最大缓存大小
        if self._max_size_gb == 0:
            try:
                import psutil
                vm = psutil.virtual_memory()
                auto_gb = max(1.0, vm.available / (1024 ** 3) - 4.0)
                self._max_size_gb = auto_gb
                log.info("自动设置缓存上限: %.1f GB", auto_gb)
            except Exception:
                self._max_size_gb = 8.0  # 默认 8GB

    # ── 存取 ────────────────────────────────────────────────────

    def put(self, key: str, data: Any, size_bytes: int = 0) -> None:
        """存入缓存条目。

        如果键已存在则更新数据，新条目默认为 KEEP_IN_RAM 状态。
        超过上限时按 LRU 淘汰。

        Args:
            key: 缓存键（通常是 model_id）
            data: 缓存数据（模型权重、张量等）
            size_bytes: 数据大小（字节）
        """
        # 如果已存在，先移除旧的
        if key in self._cache:
            old_entry = self._cache.pop(key)
            self._current_size_bytes -= old_entry.size_bytes

        # 检查是否需要淘汰
        self._evict_if_needed(size_bytes)

        # 存入新条目
        entry = CacheEntry(key, data, size_bytes)
        self._cache[key] = entry
        self._cache.move_to_end(key)  # LRU: 最新访问放末尾
        self._current_size_bytes += size_bytes

        log.debug(
            "缓存存入: %s (%.2f MB, 总计 %.2f MB / %.1f GB)",
            key,
            size_bytes / (1024 * 1024),
            self._current_size_bytes / (1024 * 1024),
            self._max_size_gb,
        )

    def get(self, key: str) -> Any | None:
        """获取缓存条目，自动解压。

        LRU 语义: 访问后移到末尾（最近使用）。

        Args:
            key: 缓存键

        Returns:
            缓存数据，如果不存在或已卸载则返回 None
        """
        entry = self._cache.get(key)
        if entry is None:
            return None

        # 如果已卸载，需要重新加载
        if entry.state == CacheState.FULL_UNLOAD:
            log.debug("缓存已卸载，需重新加载: %s", key)
            return None

        # 如果已压缩，需要解压
        if entry.state == CacheState.COMPRESS_LZ4:
            entry.data = self._decompress(entry.data)
            entry.state = CacheState.KEEP_IN_RAM

        entry.touch()
        self._cache.move_to_end(key)
        return entry.data

    def remove(self, key: str) -> bool:
        """主动移除缓存条目。

        Returns:
            True 如果移除成功
        """
        if key in self._cache:
            entry = self._cache.pop(key)
            self._current_size_bytes -= entry.size_bytes
            log.debug("缓存移除: %s", key)
            return True
        return False

    # ── 压缩/卸载策略 ──────────────────────────────────────────

    def compress_inactive(self, idle_threshold_seconds: float = 300) -> int:
        """压缩超过指定时间未访问的缓存条目。

        规格 §5.1 MEMORY_PRESSURE 模式调用此方法。

        Args:
            idle_threshold_seconds: 空闲阈值（秒），默认 5 分钟

        Returns:
            被压缩的条目数
        """
        if not self._compression_enabled:
            log.debug("压缩未启用，跳过")
            return 0

        now = time.time()
        count = 0
        for entry in self._cache.values():
            if (
                entry.state == CacheState.KEEP_IN_RAM
                and (now - entry.last_access) > idle_threshold_seconds
            ):
                entry.data = self._compress(entry.data)
                entry.state = CacheState.COMPRESS_LZ4
                count += 1

        if count > 0:
            log.info("压缩 %d 个空闲缓存条目 (LZ4)", count)
        return count

    def full_unload(self, except_keys: list | None = None) -> int:
        """完全卸载缓存条目（释放到磁盘或丢弃）。

        规格 §5.1 ALL_TENSE 模式调用此方法。

        Args:
            except_keys: 保留的键列表

        Returns:
            被卸载的条目数
        """
        keep = set(except_keys or [])
        count = 0
        for entry in self._cache.values():
            if entry.key not in keep and entry.state != CacheState.FULL_UNLOAD:
                entry.data = None  # 释放数据引用
                entry.state = CacheState.FULL_UNLOAD
                self._current_size_bytes -= entry.size_bytes
                count += 1

        if count > 0:
            log.warning("完全卸载 %d 个缓存条目", count)
            # P2 防显存碎片：卸载后立即归还 CUDA 缓存块，不依赖外部 GC。
            # 权重为 CUDA 张量时置 None 仅释放引用，caching allocator 缓存块
            # 需 empty_cache 才归还驱动。
            self._release_cuda_cache()
        return count

    def _release_cuda_cache(self) -> None:
        """卸载后立即清空 CUDA 缓存（无 GPU / 无 torch 时静默跳过）。"""
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 - 缓存回收失败不影响功能
            log.debug("_release_cuda_cache: 降级忽略", exc_info=True)

    def keep_in_ram(self, key: str) -> None:
        """将指定条目标记为常驻内存（不被压缩/卸载）。"""
        entry = self._cache.get(key)
        if entry:
            entry.state = CacheState.KEEP_IN_RAM
            entry.touch()

    # ── 内部方法 ────────────────────────────────────────────────

    def _evict_if_needed(self, incoming_size: int) -> None:
        """LRU 淘汰：当加入新条目会超限时，淘汰最久未访问的条目。"""
        max_bytes = int(self._max_size_gb * (1024 ** 3))

        while (
            len(self._cache) >= self._max_entries
            or (self._current_size_bytes + incoming_size) > max_bytes
        ) and self._cache:
            # LRU: 从头部弹出（最久未访问）
            evicted_key, evicted_entry = self._cache.popitem(last=False)
            self._current_size_bytes -= evicted_entry.size_bytes
            log.debug("LRU 淘汰: %s (%.2f MB)", evicted_key, evicted_entry.size_bytes / (1024 * 1024))

    def _compress(self, data: Any) -> bytes:
        """使用 LZ4 压缩数据。"""
        if not self._compression_enabled or data is None:
            return data
        try:
            import pickle
            raw = pickle.dumps(data)
            return _lz4_block.compress(raw)
        except Exception as e:
            log.warning("压缩失败: %s", e)
            return data

    def _decompress(self, data: Any) -> Any:
        """使用 LZ4 解压数据。

        信任边界（审计 09-10 P2-4）：pickle 只解**本进程 _compress 自写**
        的状态条目（模型管理器内部数据），无外部输入进入缓存的通路。
        """
        if not self._compression_enabled or data is None:
            return data
        try:
            import pickle
            raw = _lz4_block.decompress(data)
            return pickle.loads(raw)
        except Exception as e:
            log.warning("解压失败: %s", e)
            return data

    # ── 状态查询 ────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """返回缓存统计信息。"""
        state_counts = {CacheState.KEEP_IN_RAM: 0, CacheState.COMPRESS_LZ4: 0, CacheState.FULL_UNLOAD: 0}
        for entry in self._cache.values():
            state_counts[entry.state] = state_counts.get(entry.state, 0) + 1

        return {
            "total_entries": len(self._cache),
            "max_entries": self._max_entries,
            "current_size_mb": round(self._current_size_bytes / (1024 * 1024), 2),
            "max_size_gb": self._max_size_gb,
            "compression_enabled": self._compression_enabled,
            "state_counts": state_counts,
            "keys": list(self._cache.keys()),
        }

    def clear(self) -> None:
        """清空所有缓存。"""
        self._cache.clear()
        self._current_size_bytes = 0
        log.info("缓存已清空")
