"""OmniSpace 插件运行时 · 宿主基类（OSP v1，插件系统 P1 2026-09-16）。

本模块是进程内插件的**宿主标准**（标准名 `omnispace.plugin`）：
插件源码写 `from omnispace.plugin import ExpertPlugin, PluginContext`，
由 loader 在加载前把本模块注入 sys.modules（见 loader._ensure_host_module）。

设计决策（方案 v1.2，两轮深验在案 docs/插件系统方案-2026-09-16.md）：
- 基类从 PR#2 插件（plugin/video_making.py）的真实用法反推最小接口，
  POC 实证完整生命周期可跑通（on_load → on_think → on_unload）；
- 命名与 skills/（第一方技能目录）对齐：将来 skills/ 可平移进本体系，
  P1 冻结不动 skills/（H3 主力链路冻结令）；
- **信任模型（诚实声明）**：进程内插件与后端同权限（登记表是治理
  便利不是沙箱）。真防线 = PR 逐行审查 + 发行包零第三方插件
  （make_dist 白名单制）+ 用户自装档走子进程隔离（P3+）；
- 内存直读 CuteMamen 包，不落盘解压（tar 路径穿越面归零）；
- 本文件 py310/py312 双兼容（发行运行时 py310.11 / 开发主链 py312），
  禁用 3.11+ 语法。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

# ── 插件记忆（三级：working 运行态 / episodic 情节 / semantic 语义） ──

MEMORY_LEVELS = ("working", "episodic", "semantic")


class PluginMemory:
    """线程安全的三级命名空间记忆。

    插件侧只调 set/remember/consolidate（两参签名，来自 PR#2 插件用法）；
    snapshot/restore 供宿主在包加载/存档时搬运（CuteMamen memory/*.json）。
    P1 为内存态：包里的记忆读入，但不在运行中回写盘（存档回写属 P2+）。
    """

    def __init__(self) -> None:
        self._ns: dict[str, dict[str, Any]] = {lv: {} for lv in MEMORY_LEVELS}
        self._lock = threading.RLock()

    def set(self, key: str, value: Any, level: str = "working") -> None:
        """写入运行态（默认 working 级）。"""
        with self._lock:
            self._ns.get(level, self._ns["working"])[key] = value

    def remember(self, key: str, value: Any) -> None:
        """写入长期记忆（episodic 级）。"""
        with self._lock:
            self._ns["episodic"][key] = value

    def get(self, key: str, level: str = "working",
            default: Any = None) -> Any:
        with self._lock:
            return self._ns.get(level, {}).get(key, default)

    def consolidate(self) -> None:
        """情节级 → 语义级合并（P1 简化：同键后写覆盖，跨级不丢）。"""
        with self._lock:
            self._ns["semantic"].update(self._ns["episodic"])

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """三级深拷贝快照（存档/展示用）。"""
        import copy
        with self._lock:
            return copy.deepcopy(self._ns)

    def restore(self, data: dict[str, Any]) -> None:
        """从包记忆恢复（容忍半包：逐级逐键，坏键跳过）。"""
        if not isinstance(data, dict):
            return
        with self._lock:
            for level in MEMORY_LEVELS:
                incoming = data.get(level)
                if isinstance(incoming, dict):
                    self._ns[level].update(incoming)


class PluginContext:
    """插件运行上下文：emit 事件（线程安全，任意工作线程可调）。

    emit 回调由运行时注入（bridge 提供：日志 + WS 中枢广播）；
    未注入时静默降级为无操作——插件在无宿主环境（如单测）也能跑。
    """

    def __init__(self,
                 emit_fn: Callable[[str, dict[str, Any]], None] | None = None,
                 plugin_name: str = "") -> None:
        self._emit_fn = emit_fn
        self.plugin_name = plugin_name

    def emit(self, topic: str, payload: dict[str, Any]) -> None:
        """发布事件：只广播摘要与统计，禁止往总线塞帧本体（大数组）。"""
        fn = self._emit_fn
        if fn is None:
            return
        try:
            fn(topic, payload)
        except Exception:  # noqa: BLE001 - 事件失败不连累插件主流程
            pass


class ExpertPlugin:
    """插件基类：加载/思考/卸载三生命周期。

    子类约定（对齐 PR#2 插件的真实形态）：
    - ``__init__(name, *, route=None, **kwargs)`` 透传 super()；
    - ``on_think(event, ctx)`` 收 ``{"topic": ..., "data": spec}``，
      返回 JSON 可序列化的 dict（帧本体等大对象由宿主经专用键搬运）；
    - stats()/build_manifest() 返回展示用元数据。
    """

    BASE_MODEL = "unknown"
    CAPABILITY = ""

    def __init__(self, name: str, *, route: str | None = None,
                 **kwargs: Any) -> None:
        self.name = name
        self.route = route or "default"
        self.memory = PluginMemory()
        # 宿主附加位（运行时填写，插件只读）
        self.runtime_meta: dict[str, Any] = {}

    def on_load(self, ctx: PluginContext) -> None:
        """加载完成钩子（默认无操作）。"""

    def on_think(self, event: dict[str, Any],
                 ctx: PluginContext | None) -> dict[str, Any] | None:
        """执行一次插件任务（默认无操作，子类实现）。"""
        return None

    def on_unload(self) -> None:
        """卸载前钩子（默认无操作）。"""

    def stats(self) -> dict[str, Any]:
        """运行统计（子类可扩展）。"""
        return {"name": self.name, "route": self.route,
                "base_model": self.BASE_MODEL}

    def build_manifest(self, **extra: Any) -> dict[str, Any]:
        """能力清单（子类可扩展；extra 由运行时补充来源/信任级）。"""
        manifest: dict[str, Any] = {
            "name": self.name,
            "base_model": self.BASE_MODEL,
            "capability": self.CAPABILITY,
        }
        manifest.update(extra)
        return manifest
