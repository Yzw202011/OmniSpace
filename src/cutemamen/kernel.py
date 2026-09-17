"""CuteMamen 通用固定内核 (Nest) 与 CubeGPT 精简模型

CuteMamenKernel (规范 §1): 一个轻量级固定内核, 只做三件事 —
    1. 路由 (router): 把事件按主题路由给插件
    2. 工作记忆 (working memory): 固定容量的近期事件 + KV 堆注意力
    3. 内存调度 (plugin registry + memory budget): 插件挂载/卸载/
       随用随载热加载/内存预算 LRU 淘汰 (淘汰前自动存档 .CuteMamen)
内核不含任何专家计算 —— 所有思考由 ExpertPlugin 实现。

CubeGPTKernel (v0.7.2 任务"模型精简"): CubeGPT 的内核形态。
经典 CubeGPT 的四类职责中, "模态面皮层计算"整体外移为 FacePlugin
思考插件, 内核只保留**必要思考**:
    - 立方体棱路由 (邻面脉冲注入 = 事件路由)
    - KV 堆工作记忆 (注意力检索 + 写入)
    - 顶层输出头 (16 单元, 模式融合的最小读出)
    - 节律调制 (思考/抑制相位)
    - 生命周期与内存预算调度
API 与经典 CubeGPT 保持一致 (step / faces / export_face / ...)。
"""

import os
import time
from collections import deque
from typing import Any

import numpy as np

from .event_bus import EventBus
from .plugin import ExpertPlugin, PluginContext

# 默认插件存档目录 (卸载/淘汰时自动存档)
# 2026-09-17 勘误：旧值 "cutemamen_pkgs"=仓库内目录，运行时存档会写脏
# git 跟踪区（09-16 审计实锤 a/echo/x 三死档即此因）；迁 data/ 运行时
# 区，与 kernel_gateway 的 data/plugins/pkgs 落位一致
DEFAULT_PKG_DIR = "data/plugins/pkgs"

# .CuteMamen 插件标准落地目录 (v0.8.5): 思考插件以独立包文件形式
# 放在 ./plugin/<Name>.CuteMamen, 内核 discover_plugins() 扫描注册,
# think() 按路由主题随用随载热加载 —— 宿主代码无需 import 插件模块
DEFAULT_PLUGIN_DIR = "plugin"


# ═══════════════════════════════════════════════════════════════
# 内核工作记忆 (固定容量)
# ═══════════════════════════════════════════════════════════════

class WorkingMemory:
    """内核工作记忆: 最近事件流水 + KV 堆注意力 (复用已验证 KVStack)"""

    def __init__(self, capacity: int = 10000, dim: int = 16,
                 events_capacity: int = 256):
        from ..core.distributedformer import KVStack
        self.kv_stack = KVStack(capacity=capacity, dim=dim)
        self.dim = dim
        self.recent_events: deque[dict] = deque(maxlen=events_capacity)

    def remember_event(self, event: dict[str, Any]) -> None:
        self.recent_events.append({
            "t": time.time(),
            "topic": event.get("topic"),
        })

    def retrieve(self, query: np.ndarray, top_k: int = 3) -> np.ndarray:
        return self.kv_stack.retrieve(query, top_k=top_k)

    def push(self, entry_id: str, key: np.ndarray, value: np.ndarray) -> None:
        self.kv_stack.push(entry_id, key, value)

    def stats(self) -> dict[str, Any]:
        kv = self.kv_stack.get_stats()
        return {"recent_events": len(self.recent_events), "kv": kv}


# ═══════════════════════════════════════════════════════════════
# 通用固定内核
# ═══════════════════════════════════════════════════════════════

class CuteMamenKernel:
    """CuteMamen 通用固定内核 (Nest): 路由器 + 工作记忆 + 插件注册表

    职责边界 (规范 §1): 只有被路由激活的插件消耗算力, 空闲专家零成本;
    内核对插件版本无感知 (解码器层集中兼容, 见 pkg.decode_manifest)。
    """

    def __init__(self, dim: int = 16, working_memory_capacity: int = 10000,
                 memory_budget_mb: float | None = None,
                 pkg_dir: str = DEFAULT_PKG_DIR):
        self.dim = dim
        self.memory_budget_mb = memory_budget_mb
        self.pkg_dir = pkg_dir

        # 插件注册表: 已加载 (name → plugin) + 已注册未加载 (name → pkg 路径)
        self.plugins: dict[str, ExpertPlugin] = {}
        self.registry: dict[str, str] = {}
        # 路由主题索引 (route → name): 已注册未加载的包按路由主题
        # 随用随载 (topic 不等于插件名时也能热加载)
        self.route_index: dict[str, str] = {}

        # 路由表: 主题 → 插件名 (mount 时自动登记 plugin.route)
        self.routes: dict[str, str] = {}

        # 工作记忆 + 事件总线
        self.working_memory = WorkingMemory(capacity=working_memory_capacity,
                                            dim=dim)
        self.bus = EventBus()
        self._ctx = PluginContext(bus=self.bus,
                                  working_memory=self.working_memory,
                                  kernel_version=_kernel_version(),
                                  dim=dim)
        self.total_thinks = 0

    # ── 生命周期管理 ────────────────────────────────────────
    def mount(self, plugin: ExpertPlugin, route: str | None = None) -> dict:
        """挂载插件: on_load → 登记路由 → 广播 plugin.loaded"""
        if plugin.name in self.plugins:
            raise ValueError(f"插件 {plugin.name!r} 已挂载")
        plugin.route = route or plugin.route
        plugin.on_load(self._ctx_for(plugin))
        self.plugins[plugin.name] = plugin
        self.routes[plugin.route] = plugin.name
        self.registry.pop(plugin.name, None)
        self.bus.publish("plugin.loaded", plugin.build_manifest())
        self._enforce_budget(protect=plugin.name)
        return plugin.stats()

    def unmount(self, name: str, pkg_path: str | None = None) -> str:
        """卸载插件: on_unload (蒸馏记忆) → 存档 .CuteMamen (可恢复) → 广播"""
        plugin = self._require(name)
        if pkg_path is None:
            os.makedirs(self.pkg_dir, exist_ok=True)
            pkg_path = os.path.join(self.pkg_dir, f"{name}.CuteMamen")
        # 先 on_unload (情景记忆蒸馏到语义层), 存档才能带上蒸馏结果
        plugin.on_unload()
        from .pkg import save_pkg
        save_pkg(plugin, pkg_path)
        self._remove(plugin)
        self.registry[name] = pkg_path
        self.route_index[plugin.route] = name  # 卸载后仍可按路由主题热加载
        self.bus.publish("plugin.unloaded",
                         {"name": name, "pkg": pkg_path})
        return pkg_path

    def register_pkg(self, path: str, name: str | None = None) -> dict:
        """注册 .CuteMamen / .dfpkg 到随用随载注册表 (只记路径, 不加载)"""
        from .pkg import check_core_version, read_manifest
        manifest = read_manifest(path)
        check_core_version(manifest.get("min_core_version", "0.7.0"),
                           _kernel_version())
        key = name or manifest["name"]
        self.registry[key] = str(path)
        route = manifest.get("route")
        if route and route != key:
            self.route_index[route] = key
        return manifest

    def discover_plugins(self,
                         plugin_dir: str = DEFAULT_PLUGIN_DIR) -> dict[str, dict]:
        """.CuteMamen 插件标准落地 (v0.8.5): 扫描插件目录并注册全部包

        标准布局: ./plugin/<Name>.CuteMamen (思考插件以独立包文件交付,
        内核按 base_model 注册表解码, 宿主无需 import 插件模块)。
        只注册不加载 (随用随载), think() 路由命中时才热加载。
        返回 {插件名: manifest}。
        """
        from .pkg import CUTEMAMEN_SUFFIX, DFPKG_SUFFIX
        discovered: dict[str, dict] = {}
        if not os.path.isdir(plugin_dir):
            return discovered
        for fn in sorted(os.listdir(plugin_dir)):
            path = os.path.join(plugin_dir, fn)
            if not os.path.isfile(path) or \
                    not fn.endswith((CUTEMAMEN_SUFFIX, DFPKG_SUFFIX)):
                continue
            manifest = self.register_pkg(path)
            discovered[manifest["name"]] = manifest
        return discovered

    def hot_load(self, name: str) -> ExpertPlugin:
        """从注册表热加载插件 (随用随载)"""
        path = self.registry.get(name)
        if not path:
            raise KeyError(f"插件 {name!r} 无已注册的 pkg, 已注册: "
                           f"{sorted(self.registry)}")
        if name in self.plugins:  # 已加载, 幂等
            return self.plugins[name]
        from .pkg import load_pkg
        plugin, manifest = load_pkg(path)
        if plugin.name != name:
            plugin.name = name
            plugin.route = plugin.route or name
        return self.mount(plugin)

    # ── 路由与思考 ──────────────────────────────────────────
    def think(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """通用内核入口: 路由一个事件 → 目标插件 on_think → 结果发布总线

        event: {"topic": 主题, "data": 数据, ...元信息}
        未路由的事件发布到 kernel.unrouted (不报错, 总线可观察)。
        """
        self.total_thinks += 1
        topic = event.get("topic")
        self.working_memory.remember_event(event)
        self.bus.publish("kernel.think", {"topic": topic})

        plugin = self._resolve(topic)
        if plugin is None:
            self.bus.publish("kernel.unrouted", {"topic": topic})
            return []
        result = self._dispatch(plugin, event)
        self._enforce_budget(protect=plugin.name)
        return [result] if result is not None else []

    def _resolve(self, topic: str | None) -> ExpertPlugin | None:
        """路由器: 主题 → 插件 (未加载但已注册 → 热加载)

        卸载会移除路由表项, 查找顺序:
            1. 已挂载路由表 (mount 时登记)
            2. 已注册包的路由主题索引 route_index (topic = plugin.route)
            3. 注册表按名回退 (路由主题默认等于插件名)
        → 随用随载。
        """
        if topic is None:
            return None
        name = self.routes.get(topic)
        if name is None:
            name = self.route_index.get(topic)
        if name is None and topic in self.registry:
            name = topic
        if name is None:
            return None
        if name not in self.plugins and name in self.registry:
            self.hot_load(name)
            name = self.routes.get(topic, name)
        return self.plugins.get(name)

    def _dispatch(self, plugin: ExpertPlugin,
                  event: dict[str, Any]) -> dict[str, Any] | None:
        result = plugin.on_think(event, self._ctx_for(plugin))
        self.bus.publish(f"plugin.{plugin.name}.output",
                         {"result": result} if result is not None else None)
        return result

    def _ctx_for(self, plugin: ExpertPlugin) -> PluginContext:
        return PluginContext(bus=self.bus,
                             working_memory=self.working_memory,
                             kernel_version=_kernel_version(),
                             dim=self.dim,
                             plugin_name=plugin.name)

    # ── 内存预算淘汰 ────────────────────────────────────────
    def total_footprint_mb(self) -> float:
        return round(sum(p.footprint_mb() for p in self.plugins.values()), 6)

    def _enforce_budget(self, protect: str | None = None) -> list[str]:
        """内存预算 LRU 淘汰: 超预算时逐个存档并卸载最久未用插件

        protect 指向本轮刚被激活的插件 (绝不淘汰正在思考的专家)。
        淘汰前自动存档 .CuteMamen (注册表可热加载恢复)。
        """
        evicted: list[str] = []
        if self.memory_budget_mb is None:
            return evicted
        while (self.total_footprint_mb() > self.memory_budget_mb
               and len(self.plugins) > 1):
            candidates = [p for p in self.plugins.values()
                          if p.name != protect]
            if not candidates:
                break
            victim = min(candidates, key=lambda p: p.last_used)
            pkg_path = self.unmount(victim.name)  # 存档 + on_unload + 广播
            self.bus.publish("plugin.evicted",
                             {"name": victim.name, "pkg": pkg_path,
                              "budget_mb": self.memory_budget_mb})
            evicted.append(victim.name)
        return evicted

    # ── 查询 ────────────────────────────────────────────────
    def _require(self, name: str) -> ExpertPlugin:
        if name not in self.plugins:
            raise KeyError(f"插件 {name!r} 未挂载 (已挂载: "
                           f"{sorted(self.plugins)}, 已注册: "
                           f"{sorted(self.registry)})")
        return self.plugins[name]

    def _remove(self, plugin: ExpertPlugin) -> None:
        self.plugins.pop(plugin.name, None)
        # 只清除指向该插件的路由
        for topic in [t for t, n in self.routes.items() if n == plugin.name]:
            del self.routes[topic]

    def list_plugins(self) -> dict[str, list[str]]:
        return {"loaded": list(self.plugins),
                "registered": [n for n in self.registry
                               if n not in self.plugins]}

    def stats(self) -> dict[str, Any]:
        return {
            "kernel": "CuteMamen",
            "dim": self.dim,
            "total_thinks": self.total_thinks,
            "memory_budget_mb": self.memory_budget_mb,
            "footprint_mb": self.total_footprint_mb(),
            "plugins": {n: p.stats() for n, p in self.plugins.items()},
            "registered": [n for n in self.registry
                           if n not in self.plugins],
            "working_memory": self.working_memory.stats(),
            "bus": self.bus.stats(),
        }


def _kernel_version() -> str:
    import src as _pkg
    return _pkg.__version__


# ═══════════════════════════════════════════════════════════════
# CubeGPTKernel: 模型精简到只有必要思考
# ═══════════════════════════════════════════════════════════════

class CubeGPTKernel(CuteMamenKernel):
    """CubeGPT 精简形态: 必要思考留在内核, 其余思考全部由插件实现

    经典 CubeGPT 拆分对照:
    | 经典职责            | 去向                                   |
    |---------------------|----------------------------------------|
    | 模态面皮层计算       | FacePlugin 思考插件 (可卸载/热加载/淘汰) |
    | 立方体棱 (邻面注入)  | 内核路由 (必要思考)                     |
    | KV 堆注意力         | 内核工作记忆 (必要思考)                 |
    | 输出头 (16 单元)    | 内核最小读出 (必要思考)                 |
    | 节律调制            | 内核 (必要思考)                         |
    | STDP 学习协调       | 内核生命周期职责                        |
    | 内存预算/随用随载    | 内核调度 (新增)                         |

    API 与经典 CubeGPT 兼容: step / faces / ring / kv_stack /
    export_face / import_face / unload_face / register_face_pkg /
    list_faces / get_network_stats / reset_state ...
    """

    CUBE_RING = ("numeric", "text", "timeseries", "image")

    def __init__(self, depth: int = 2, dim: int = 16,
                 modalities: list[str] | None = None,
                 kv_capacity: int = 10000,
                 memory_budget_mb: float | None = None,
                 training_mode: bool = False):
        super().__init__(dim=dim, working_memory_capacity=kv_capacity,
                         memory_budget_mb=memory_budget_mb)
        self.depth = depth
        self.training_mode = training_mode
        self._last_input = np.zeros(dim)

        # 必要思考 1: 顶层输出头 (16 单元最小读出)
        from ..core.distributedformer import OutputModule
        self.output_module = OutputModule(dim=dim)

        # 必要思考 2: 节律 (思考 80 步 + 抑制 40 步)
        self.global_modulation = 1.0
        self.cycle_phase = 0
        self.think_phase = 80
        self.inhibit_phase = 40
        self.cycle_length = 120
        self.total_steps = 0
        self.learning_enabled = True

        # 思考插件: 每个模态面一个 FacePlugin (真正的皮层计算在这里)
        for m in (modalities if modalities is not None else self.CUBE_RING):
            self.mount_face(m)

    # ── 经典 CubeGPT 兼容视图 ───────────────────────────────
    @property
    def kv_stack(self):
        return self.working_memory.kv_stack

    @property
    def faces(self) -> dict[str, Any]:
        from .face_bridge import FacePlugin
        return {p.modality: p.face for p in self.plugins.values()
                if isinstance(p, FacePlugin) and p.face is not None}

    @property
    def ring(self) -> list[str]:
        return [m for m in self.CUBE_RING if m in self.faces]

    @property
    def _face_registry(self) -> dict[str, str]:
        # v0.7.0 随用随载注册表兼容视图: {模态: pkg 路径}
        from .face_bridge import FacePlugin
        reg = {}
        for name, path in self.registry.items():
            if name in self.plugins and \
                    not isinstance(self.plugins.get(name), FacePlugin):
                continue
            reg[name] = path
        return reg

    @property
    def _all_units(self) -> list[Any]:
        units = []
        for face in self.faces.values():
            units.extend(face.get_units())
        units.extend(self.output_module.units)
        return units

    @property
    def _units_map(self) -> dict[str, Any]:
        return {u.unit_id: u for u in self._all_units}

    # ── 模态面插件挂载 ──────────────────────────────────────
    def mount_face(self, modality: str, face: Any | None = None,
                   depth: int | None = None) -> "Any":
        """挂载一个模态面思考插件 (face=None 时新建 CubeFace)"""
        from .face_bridge import FacePlugin
        plugin = FacePlugin(modality, depth=depth or self.depth, dim=self.dim,
                            face=face)
        self.mount(plugin)  # on_load → 路由登记 → 预算检查
        return plugin

    def export_face(self, modality: str, path: str,
                    **manifest_kwargs: Any) -> dict:
        """模态面 → .dfpkg 存档 (v0.7.0 API, 权重逐位可复现)"""
        from ..core import face_pkg
        return face_pkg.export_face(self, modality, path, **manifest_kwargs)

    def import_face(self, path: str, modality: str | None = None) -> dict:
        """导入 .dfpkg / .CuteMamen 面存档 → 挂载为思考插件 (替换语义)"""
        from ..core import face_pkg
        from ..core.distributedformer import CubeFace
        raw = face_pkg.read_manifest(path)
        if raw.get("format") == "dfpkg":
            manifest = raw
            face_mod = modality or manifest.get("modality") or manifest["name"]
            depth = int(manifest["depth"])
            dim = int(manifest["dim"])
            if dim != self.dim:
                raise ValueError(f"pkg dim={dim} 与模型 dim={self.dim} 不一致")
            _, params, connections, state = face_pkg._read_members(path)
            face = CubeFace(face_mod, depth=depth, dim=dim)
            face_pkg._arrays_to_face(face, params, connections, state)
            plugin = self._make_face_plugin(face_mod, face)
        else:
            plugin, manifest = _load_face_plugin(path, modality)
        self._replace_face_plugin(plugin)
        return manifest

    def _make_face_plugin(self, modality: str, face: Any) -> Any:
        from .face_bridge import FacePlugin
        return FacePlugin(modality, depth=self.depth, dim=self.dim, face=face)

    def _replace_face_plugin(self, plugin: Any) -> None:
        """经典替换语义: 同名面直接替换 (不存档), 再挂载新插件"""
        old = self.plugins.get(plugin.name)
        if old is not None:
            self._remove(old)
        self.registry.pop(plugin.name, None)
        self.mount(plugin)

    def unload_face(self, modality: str,
                    pkg_path: str | None = None) -> str:
        """卸载模态面 (默认先存档保证可恢复, v0.7.0 API)"""
        if modality not in self.faces:
            raise KeyError(f"模态面 {modality!r} 未加载 (已加载: "
                           f"{list(self.faces)}, 已注册: "
                           f"{sorted(self._face_registry)})")
        if pkg_path is None:
            os.makedirs("face_pkgs", exist_ok=True)
            pkg_path = os.path.join("face_pkgs", f"{modality}.dfpkg")
        # v0.7.0 兼容: 面卸载默认存 .dfpkg (经典格式)
        from ..core import face_pkg
        face_pkg.export_face(self, modality, pkg_path)
        plugin = self.plugins[modality]
        plugin.on_unload()
        self._remove(plugin)
        self.registry[modality] = pkg_path
        self.bus.publish("plugin.unloaded",
                         {"name": modality, "pkg": pkg_path})
        return pkg_path

    def load_face(self, modality: str,
                  pkg_path: str | None = None) -> dict:
        """从 pkg 热加载模态面 (v0.7.0 API)"""
        path = pkg_path or self._face_registry.get(modality)
        if not path:
            raise KeyError(f"模态 {modality!r} 无已注册的 pkg")
        return self.import_face(path, modality)

    def register_face_pkg(self, path: str,
                          modality: str | None = None) -> dict:
        """注册面 pkg 到随用随载注册表 (v0.7.0 API)"""
        manifest = self.register_pkg(path, name=modality)
        return manifest

    def list_faces(self) -> dict[str, list[str]]:
        loaded = list(self.faces)
        return {"loaded": loaded,
                "registered": [m for m in self._face_registry
                               if m not in loaded]}

    # ── 必要思考: 单步执行 ──────────────────────────────────
    def get_global_modulation(self) -> float:
        if self.cycle_phase < self.think_phase:
            progress = self.cycle_phase / self.think_phase
            return 1.0 - 0.5 * progress
        progress = (self.cycle_phase - self.think_phase) / self.inhibit_phase
        return 0.5 - 0.4 * progress

    def step(self, inputs: dict[str, Any] | None) -> list[Any]:
        """单步: 只做路由/记忆/读出/节律, 皮层计算全部在插件里

        与经典 CubeGPT.step 同构:
        0. 节律相位推进
        1. 未知模态 + 已注册 → 路由器热加载 (随用随载)
        2. KV 注意力检索 (以上一拍融合输入为查询)
        3. 路由各面输入到 FacePlugin.on_think (无输入面消费侧向脉冲)
        4. 立方体棱路由: 本面 outgoing → 邻面下一拍 inbox
        5. 融合 → 输出头; 6. KV 写入; 7. STDP; 8. 预算淘汰
        """
        from .face_bridge import FacePlugin

        self.total_steps += 1
        self.cycle_phase = (self.cycle_phase + 1) % self.cycle_length
        self.global_modulation = self.get_global_modulation()

        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict):
            raise TypeError("step() 需要 {模态: 数据} 字典, 收到 "
                            f"{type(inputs).__name__}")
        # 1. 路由器: 已注册未加载的模态现场热加载
        unknown = set(inputs) - set(self.faces)
        for m in list(unknown):
            if m in self._face_registry:
                self.load_face(m)
                unknown.discard(m)
        if unknown:
            raise KeyError(f"未知输入模态 {sorted(unknown)}, 已启用: "
                           f"{list(self.faces)}, 已注册待载: "
                           f"{sorted(set(self._face_registry) - set(self.faces))}")

        # 2. KV 工作记忆注意力检索
        attn = self.kv_stack.retrieve(self._last_input)

        # 3. 路由到各面思考插件 (持续思考: 无输入面消费侧向脉冲)
        results: dict[str, dict | None] = {}
        for name in self.ring:
            plugin = self.plugins.get(name)
            if not isinstance(plugin, FacePlugin):
                continue
            face = plugin.face
            has_input = inputs.get(name) is not None
            if not has_input and not np.any(face.inbox):
                results[name] = None
                continue
            event = {
                "topic": name,
                "data": inputs.get(name),
                "modulation": self.global_modulation,
                "attn": attn,
                "lateral": face.inbox,
                "training_mode": self.training_mode,
            }
            results[name] = self._dispatch(plugin, event)

        # 4. 立方体棱路由: 本面 outgoing → 邻面下一拍 inbox
        #    (未思考的面复用上一拍脉冲聚合, 与经典 CubeGPT 行为一致)
        ring = self.ring
        outgoing_all: dict[str, np.ndarray] = {}
        for name in ring:
            res = results.get(name)
            outgoing_all[name] = (
                res["outgoing"] if res is not None
                else self.plugins[name].face.collect_outgoing())
        for i, name in enumerate(ring):
            nxt = ring[(i + 1) % len(ring)]
            self.plugins[nxt].face.inbox = outgoing_all[name]

        # 5. 融合各面输出 → 输出头 (内核最小读出)
        fused = np.zeros(self.dim)
        for name in ring:
            fused += 0.5 * outgoing_all[name]
        head_input = np.tanh(fused)
        output_spikes = self.output_module.step(
            head_input, self.global_modulation, attn)
        self._last_input = head_input

        # 6. KV 工作记忆写入 (非训练模式)
        if not self.training_mode:
            for i, unit in enumerate(self.output_module.units):
                if unit.spike_count > 0:
                    self.kv_stack.push(
                        f"output_{i}_{self.total_steps}",
                        unit.state, unit.state)

        # 7. STDP 学习协调 (生命周期职责)
        if self.learning_enabled:
            self._apply_stdp_to_all()

        # 8. 内存预算淘汰 (绝不淘汰本轮激活的面)
        self._enforce_budget(protect=None)
        self.total_thinks += 1
        return output_spikes

    # think = step (CuteMamen 通用入口别名)
    def think(self, event: Any) -> Any:
        if isinstance(event, dict) and "topic" not in event and \
                all(k in self.CUBE_RING for k in event):
            return self.step(event)  # {模态: 数据} → CubeGPT 语义
        return super().think(event)

    # ── 学习 / 统计 / 重置 ──────────────────────────────────
    def enable_learning(self, enabled: bool = True) -> None:
        self.learning_enabled = enabled
        for u in self._all_units:
            u.stdp_enabled = enabled

    def _apply_stdp_to_all(self) -> None:
        now = time.time()
        units_map = self._units_map
        for unit in self._all_units:
            if unit.spike_times:
                unit.apply_stdp(now, units_map)

    def get_stdp_stats(self) -> dict[str, Any]:
        units = self._all_units
        total_ltp = sum(u.ltp_count for u in units)
        total_ltd = sum(u.ltd_count for u in units)
        total_change = sum(u.total_weight_change for u in units)
        return {"learning_enabled": self.learning_enabled,
                "total_ltp": total_ltp, "total_ltd": total_ltd,
                "total_weight_change": float(total_change),
                "avg_weight_change": float(
                    total_change / max(1, total_ltp + total_ltd))}

    def get_output_pattern(self) -> np.ndarray:
        return self.output_module.get_pattern()

    def get_network_stats(self) -> dict[str, Any]:
        units = self._all_units
        faces = self.faces
        return {
            "model": "CubeGPTKernel",
            "depth": self.depth,
            "faces": list(faces),
            "total_units": len(units),
            "total_params": len(units) * 16,
            "total_spikes": sum(u.spike_count for u in units),
            "cycle_phase": self.cycle_phase,
            "global_modulation": float(self.global_modulation),
            "faces_stats": {
                name: {
                    "cortex_units": len(face.cortex._all_units_cache),
                    "total_spikes": sum(u.spike_count
                                        for u in face.get_units()),
                    "last_step_spikes": len(face.last_spikes),
                }
                for name, face in faces.items()
            },
            "kv_stats": self.kv_stack.get_stats(),
            "total_steps": self.total_steps,
            "stdp": self.get_stdp_stats(),
            # CuteMamen 内核扩展统计
            "cutemamen": {
                "loaded_plugins": list(self.plugins),
                "registered": [n for n in self.registry
                               if n not in self.plugins],
                "footprint_mb": self.total_footprint_mb(),
                "memory_budget_mb": self.memory_budget_mb,
                "bus": self.bus.stats(),
            },
        }

    def reset_state(self) -> None:
        for face in self.faces.values():
            face.reset_state()
        self.output_module.reset_state()
        self.cycle_phase = 0
        self.global_modulation = 1.0
        self.total_steps = 0
        self._last_input = np.zeros(self.dim)


def _load_face_plugin(path: str, modality: str):
    """加载 .CuteMamen 面存档 → (FacePlugin, manifest)"""
    from .pkg import load_pkg
    plugin, manifest = load_pkg(path)
    if modality and plugin.modality != modality:
        plugin.modality = modality
        plugin.route = modality
        plugin.name = modality
    return plugin, manifest
