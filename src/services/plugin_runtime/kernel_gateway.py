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

log = logging.getLogger("omnispace.services.plugin_runtime.kernel_gateway")

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


def _merge_instances(kernel: Any) -> None:
    """合流补丁（2026-09-17 用户拍板③=合流为一）：单实例双面服务。

    补丁内核 hot_load：OSP PluginRuntime 已加载的插件直接注入内核
    plugins 表——不再重复装载、记忆/状态天然互通（同一 Python 对象）。
    OSP 侧不补丁（ensure_loaded 有复杂状态机，侵入风险高）；内核先载
    OSP 后 invoke 的场景（罕见：工厂件 OSP 启动即种子登记）OSP 会
    自行装载一份，后续内核 think 时发现 OSP 实例并替换——最终收敛
    为单实例。沙箱档（user_source）不经此路（子进程执行）。
    """
    from .registry import get_plugin_runtime

    original_hot_load = kernel.hot_load

    def merged_hot_load(name: str) -> Any:
        rt = get_plugin_runtime()
        entry = getattr(rt, "_registry", {}).get(name)
        if entry is not None and getattr(entry, "instance", None) is not None:
            if name not in kernel.plugins:
                kernel.plugins[name] = entry.instance
                log.info("内核复用 OSP 实例: %s（合流）", name)
            return entry.instance
        return original_hot_load(name)

    kernel.hot_load = merged_hot_load  # type: ignore[method-assign]


def _gate_kernel_think(kernel: Any) -> None:
    """think 网关补丁（2026-09-17 安全盾接线）：内核路由事件先过安全门禁。

    与 registry.invoke 共用同一 SecurityMonitor 单例（指纹基线/审计
    环形互通）；拦截时抛 PluginRuntimeError，由 API 层统一翻译信封。
    """
    from . import security_gate
    from .registry import PluginRuntimeError

    original_think = kernel.think

    def gated_think(event: dict[str, Any]) -> Any:
        # 经模块属性调用（保持可 monkeypatch，测试拦截路径用）
        ok, outcome, reason = security_gate.gate_kernel_event(event)
        if not ok:
            code = ("PLUGIN_SECURITY_REVIEW" if outcome == "review"
                    else "PLUGIN_SECURITY_DENIED")
            topic = str(event.get("topic", ""))
            raise PluginRuntimeError(
                code,
                f"内核 think 被安全门禁拦截（{outcome}）：{topic}——{reason}",
                "调用命中高危/逃逸规则被挂起；核查插件来源，或经 config "
                "plugins.security_gate 关闭安全门禁后排查")
        return original_think(event)

    kernel.think = gated_think  # type: ignore[method-assign]


def get_plugin_kernel() -> Any:
    """获取内核单例（懒初始化 + 首扫 plugin/ 目录 + 实例合流）。"""
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
                    log.info("内核插件发现: %s", sorted(discovered))
                except Exception:  # noqa: BLE001 - 发现失败不拦端点
                    log.warning("内核插件目录扫描失败", exc_info=True)
                _merge_instances(kernel)  # 合流：单实例双面服务
                _gate_kernel_think(kernel)  # 安全盾：think 路由前置门禁
                _kernel = kernel
    return _kernel


def reset_plugin_kernel() -> None:
    """复位内核单例（测试用）。"""
    global _kernel
    with _kernel_lock:
        _kernel = None


def register_user_pkg(pkg_path: Any) -> bool:
    """把用户导入的纯数据包登记进内核（think 路由即可达）。

    失败不拦导入主流程（OSP invoke 仍可用），仅告警。
    """
    kernel = get_plugin_kernel()
    try:
        kernel.register_pkg(str(pkg_path))
        return True
    except Exception:  # noqa: BLE001 - 内核登记失败不拦导入
        log.warning("用户包内核登记失败: %s", pkg_path, exc_info=True)
        return False


def unregister_plugin(name: str) -> None:
    """从内核移除插件登记（删除插件时同步收尾）。"""
    global _kernel
    with _kernel_lock:
        kernel = _kernel
    if kernel is None:
        return
    kernel.registry.pop(name, None)
    kernel.plugins.pop(name, None)
    for topic, target in list(kernel.route_index.items()):
        if target == name:
            kernel.route_index.pop(topic, None)
    for topic, target in list(kernel.routes.items()):
        if target == name:
            kernel.routes.pop(topic, None)
