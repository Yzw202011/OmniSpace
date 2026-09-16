"""CuteMamen 内核宿主网关（内核接线，2026-09-16 拍板）。

内核是插件系统的"总管家"形态：按主题路由事件、维护工作记忆、
内存预算 LRU 淘汰（淘汰前自动存档）。本网关把它以进程内单例接入产品：

- pkg_dir 指向 ``data/plugins/pkgs``（运行时目录——内核的自动存档
  绝不写仓库 tracked 的 cutemamen_pkgs/，测试侧另有 conftest 隔离）；
- 首次获取时 discover_plugins() 扫描 ``plugin/*.CuteMamen`` 登记
  随用随载（think 路由命中才热加载）；
- 内核生命周期事件（plugin.loaded/unloaded/evicted）桥接到
  PluginEventBridge（日志 + WS 广播）。

内核为纯 numpy 轻量计算（零 GPU），内存预算默认 512MB 由内核
自带 LRU 执行；本网关不做模型加载，不参与显存调度。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from ...config import DATA_DIR
from .bridge import get_event_bridge

logger = logging.getLogger(__name__)

# 内核存档目录（data/ 为运行时区，git 不跟踪）
KERNEL_PKG_DIR = DATA_DIR / "plugins" / "pkgs"
# 插件包标准落地目录（仓库 tracked 的产品资产，discover 只读）
PLUGIN_DIR = "plugin"

_kernel: Any = None
_kernel_lock = threading.Lock()

# 桥接的内核生命周期主题（不含 plugin.<name>.output——输出可能带帧）
_BRIDGED_TOPICS = ("plugin.loaded", "plugin.unloaded", "plugin.evicted")


def _bridge_kernel_events(kernel: Any) -> None:
    """内核事件总线 → 宿主事件桥（日志 + WS 广播）。"""
    bridge = get_event_bridge()

    def _on_kernel_event(topic: str) -> Any:
        def handler(payload: Any) -> None:
            bridge.emit("kernel", topic,
                        payload if isinstance(payload, dict) else {})
        return handler

    for topic in _BRIDGED_TOPICS:
        kernel.bus.subscribe(topic, _on_kernel_event(topic))


def get_plugin_kernel() -> Any:
    """获取内核单例（懒初始化 + 首扫 plugin/ 目录）。"""
    global _kernel
    if _kernel is None:
        with _kernel_lock:
            if _kernel is None:
                from ...cutemamen.kernel import CuteMamenKernel
                kernel = CuteMamenKernel(
                    dim=16, memory_budget_mb=512.0,
                    pkg_dir=str(KERNEL_PKG_DIR))
                _bridge_kernel_events(kernel)
                try:
                    discovered = kernel.discover_plugins(PLUGIN_DIR)
                    logger.info("内核插件发现: %s", sorted(discovered))
                except Exception:  # noqa: BLE001 - 发现失败不拦端点
                    logger.warning("内核插件目录扫描失败", exc_info=True)
                _kernel = kernel
    return _kernel


def reset_plugin_kernel() -> None:
    """复位内核单例（测试用）。"""
    global _kernel
    with _kernel_lock:
        _kernel = None
