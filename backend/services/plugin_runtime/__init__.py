"""插件运行时（OSP v1，插件系统 P1 2026-09-16）。

对外门面：get_plugin_runtime() 单例 + 基类再导出（插件标准入口
omnispace.plugin 由 loader 注入 sys.modules，指向本包 base 模块）。

组件分工：
- base.py     宿主基类（ExpertPlugin/PluginContext/PluginMemory）
- bridge.py   事件桥（工作线程 emit → 日志 + WS 中枢，线程安全）
- loader.py   源码模块加载 + CuteMamen 包内存直读 + 帧落盘
- registry.py 登记表 + 生命周期 + invoke 通道（三道运行闸）

方案真源：docs/插件系统方案-2026-09-16.md（v1.2，两轮深验在案）。
"""
from __future__ import annotations

from .base import ExpertPlugin, PluginContext, PluginMemory
from .registry import (
                       MIN_FREE_RAM_GB,
                       OUTPUT_ROOT,
                       PluginRuntime,
                       PluginRuntimeError,
                       get_plugin_runtime,
)

__all__ = [
    "ExpertPlugin", "PluginContext", "PluginMemory",
    "PluginRuntime", "PluginRuntimeError", "get_plugin_runtime",
    "MIN_FREE_RAM_GB", "OUTPUT_ROOT",
]
