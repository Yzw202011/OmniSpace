"""CuteMamen 专家插件基类与三级记忆 (规范 §5 生命周期钩子 / §3 memory/)

每个插件是一个自包含的、面向特定任务的"思考插件":
- 生命周期钩子: on_load / on_think / on_unload (内核按规范顺序调用)
- 三级记忆存档: working (工作) / episodic (情景) / semantic (语义),
  随 .CuteMamen 包的 memory/ 目录一起存档与还原
- 权重序列化: save_weights / load_weights (随包的 weights/ 目录存档)

内核 (kernel.CuteMamenKernel) 只做路由、生命周期管理与内存调度,
不包含任何专家计算 —— 全部思考由本模块的插件实现。
"""

import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

import numpy as np

# 当前实现的 CuteMamen 标准版本 (v1 → v2 迁移见 pkg.decode_manifest / migrate.py)
CURRENT_STANDARD_VERSION = "2.0.0"

# 三级记忆默认容量 (条数)
DEFAULT_WORKING_CAPACITY = 64
DEFAULT_EPISODIC_CAPACITY = 256
DEFAULT_SEMANTIC_CAPACITY = 512


# ═══════════════════════════════════════════════════════════════
# 三级记忆: working / episodic / semantic
# ═══════════════════════════════════════════════════════════════

class PluginMemory:
    """插件三级记忆存档 (CuteMamen §3 memory/)

    - working  (工作记忆): 当前会话的活跃状态, FIFO 容量上限, 最快淘汰
    - episodic (情景记忆): 带
    时间戳的事件流水 (on_think 的经过), deque 容量上限
    - semantic (语义记忆): 蒸馏后的 key → value 知识, LRU 容量上限,
      由 consolidate() 从情景记忆聚合或 remember() 显式写入

    archive()/restore() 输出 JSON 安全结构, 直接写入 .CuteMamen 包的
    memory/{working,episodic,semantic}.json。
    """

    LEVELS = ("working", "episodic", "semantic")

    def __init__(self,
                 working_capacity: int = DEFAULT_WORKING_CAPACITY,
                 episodic_capacity: int = DEFAULT_EPISODIC_CAPACITY,
                 semantic_capacity: int = DEFAULT_SEMANTIC_CAPACITY):
        self.working_capacity = working_capacity
        self.episodic_capacity = episodic_capacity
        self.semantic_capacity = semantic_capacity
        # working: 插入序 key → value (JSON 安全), 超容量淘汰最旧
        self.working: "OrderedDict[str, Any]" = OrderedDict()
        # episodic: {"t": 时间戳, "topic": 主题, "summary": 摘要}
        self.episodic: Deque[Dict] = deque(maxlen=episodic_capacity)
        # semantic: key → value, LRU
        self.semantic: "OrderedDict[str, Any]" = OrderedDict()

    # ── working ────────────────────────────────────────────
    def set(self, key: str, value: Any) -> None:
        """写入工作记忆 (覆盖同名键, 容量满时淘汰最旧; 值做 JSON 安全化)"""
        if key in self.working:
            self.working.pop(key)
        self.working[key] = _json_safe(value)
        while len(self.working) > self.working_capacity:
            self.working.popitem(last=False)

    def get(self, key: str, default: Any = None) -> Any:
        return self.working.get(key, default)

    def drop(self, key: str) -> None:
        self.working.pop(key, None)

    # ── episodic ───────────────────────────────────────────
    def record(self, topic: str, summary: Any = None, when: Optional[float] = None) -> Dict:
        """记录一条情景记忆 (on_think 被路由激活时由基类自动调用)"""
        event = {"t": when if when is not None else time.time(),
                 "topic": str(topic), "summary": _json_safe(summary)}
        self.episodic.append(event)
        return event

    def recent(self, n: int = 10) -> List[Dict]:
        return list(self.episodic)[-n:]

    # ── semantic ───────────────────────────────────────────
    def remember(self, key: str, value: Any) -> None:
        """写入语义记忆 (蒸馏知识), LRU 淘汰"""
        if key in self.semantic:
            self.semantic.pop(key)
        self.semantic[key] = _json_safe(value)
        while len(self.semantic) > self.semantic_capacity:
            self.semantic.popitem(last=False)

    def recall(self, key: str, default: Any = None) -> Any:
        if key not in self.semantic:
            return default
        self.semantic.move_to_end(key)  # LRU touch
        return self.semantic[key]

    def forget(self, key: str) -> None:
        self.semantic.pop(key, None)

    # ── 蒸馏: episodic → semantic ──────────────────────────
    def consolidate(self) -> Dict[str, int]:
        """情景记忆 → 语义记忆蒸馏: 按 topic 聚合计数写入语义层"""
        counts: Dict[str, int] = {}
        for ev in self.episodic:
            counts[ev["topic"]] = counts.get(ev["topic"], 0) + 1
        for topic, n in counts.items():
            self.remember(f"topic:{topic}", {"activations": n})
        return counts

    # ── 存档 / 还原 (JSON 安全) ─────────────────────────────
    def archive(self) -> Dict[str, Any]:
        return {
            "working": [{"key": k, "value": v} for k, v in self.working.items()],
            "episodic": list(self.episodic),
            "semantic": [{"key": k, "value": v} for k, v in self.semantic.items()],
            "capacities": {
                "working": self.working_capacity,
                "episodic": self.episodic_capacity,
                "semantic": self.semantic_capacity,
            },
        }

    def restore(self, data: Dict[str, Any]) -> None:
        self.working = OrderedDict()
        for item in data.get("working", []):
            self.working[item["key"]] = item.get("value")
        cap = data.get("capacities", {})
        self.working_capacity = int(cap.get("working", self.working_capacity))
        episodic = [dict(e) for e in data.get("episodic", [])]
        self.episodic_capacity = int(cap.get("episodic", self.episodic_capacity))
        self.episodic = deque(episodic[-self.episodic_capacity:],
                              maxlen=self.episodic_capacity)
        self.semantic = OrderedDict()
        for item in data.get("semantic", []):
            self.semantic[item["key"]] = item.get("value")
        self.semantic_capacity = int(cap.get("semantic", self.semantic_capacity))
        while len(self.semantic) > self.semantic_capacity:
            self.semantic.popitem(last=False)

    def stats(self) -> Dict[str, Any]:
        return {
            "working": len(self.working),
            "episodic": len(self.episodic),
            "semantic": len(self.semantic),
        }

    def footprint_bytes(self) -> int:
        """粗估记忆占用 (以 archive 后的 JSON 行数×平均行长近似)"""
        import json
        try:
            return len(json.dumps(self.archive(), ensure_ascii=False,
                                  default=str).encode("utf-8"))
        except (TypeError, ValueError):
            return 0


def _json_safe(value: Any) -> Any:
    """numpy 数组/标量 → JSON 安全结构 (三级记忆存档用)"""
    if isinstance(value, np.ndarray):
        return {"__ndarray__": value.tolist()}
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


# ═══════════════════════════════════════════════════════════════
# 插件上下文: on_load / on_think 收到的内核门面
# ═══════════════════════════════════════════════════════════════

@dataclass
class PluginContext:
    """内核提供给插件的受限门面 (事件总线 + 工作记忆 + 元信息)"""
    bus: Any                      # EventBus (event_bus.py)
    working_memory: Any           # WorkingMemory (kernel.py)
    kernel_version: str
    dim: int
    plugin_name: str = ""

    def emit(self, topic: str, payload: Any) -> None:
        """插件 → 事件总线发布 (插件间通信)"""
        self.bus.publish(topic, payload)


# ═══════════════════════════════════════════════════════════════
# 专家插件基类
# ═══════════════════════════════════════════════════════════════

class ExpertPlugin:
    """CuteMamen 专家插件基类 (思考插件)

    生命周期钩子 (规范 §5, 内核按顺序调用):
        on_load(ctx)        插件加载完成 — 初始化资源
        on_think(event, ctx) 事件到达被路由激活 — 核心计算
        on_unload()         插件卸载 — 释放资源 (记忆由内核存档)

    子类只需实现钩子与权重序列化; 三级记忆由基类托管:
    on_think 每次激活自动记录一条情景记忆。
    """

    BASE_MODEL = "generic"        # manifest.base_model, 加载注册表按它分发
    CAPABILITY = ""               # 人类可读能力描述 (子类覆盖)

    def __init__(self, name: str, *, route: Optional[str] = None,
                 capability: str = "", author: str = "DistributedFormer",
                 version: str = "0.1.0",
                 memory: Optional[PluginMemory] = None):
        self.name = name
        # 路由主题: 内核按 route 把事件路由给本插件 (默认 = 插件名)
        self.route = route or name
        self.capability = capability or self.CAPABILITY
        self.author = author
        self.version = version
        self.memory = memory or PluginMemory()
        self.loaded = False
        self.think_count = 0
        self.last_used = 0.0
        self.loaded_at = 0.0

    # ── 从包构建 (pkg.load_pkg 入口, 子类按需覆盖) ───────────
    @classmethod
    def from_pkg(cls, manifest: Dict[str, Any]) -> "ExpertPlugin":
        """按解码后清单构造插件实例 (load_weights 随后注入权重)"""
        return cls(manifest["name"],
                   route=manifest.get("route"),
                   capability=manifest.get("capability", ""),
                   author=manifest.get("author", "DistributedFormer"),
                   version=manifest.get("version", "0.1.0"))

    # ── 生命周期钩子 (CuteMamen §5) ─────────────────────────
    def on_load(self, ctx: PluginContext) -> None:
        """加载完成: 初始化资源 (默认标记已加载)"""
        self.loaded = True
        self.loaded_at = time.time()

    def on_think(self, event: Dict[str, Any],
                 ctx: PluginContext) -> Optional[Dict[str, Any]]:
        """核心计算: 事件到达、插件被路由激活。

        event 为统一内部格式 (解码器产出):
            {"topic": 路由主题, "data": 原始数据, ...内核注入的元信息}
        返回结果字典 (将发布到事件总线 topic "plugin.<name>.output"),
        无输出返回 None。基类实现只记录情景记忆, 不做计算。
        """
        self.think_count += 1
        self.last_used = time.time()
        self.memory.record(event.get("topic", self.route),
                           summary=_event_summary(event))
        return None

    def on_unload(self) -> None:
        """卸载: 释放资源 (权重与记忆由内核随包存档, 默认无需做事)"""
        self.loaded = False

    # ── 权重序列化 (.CuteMamen weights/) ────────────────────
    def save_weights(self) -> Dict[str, np.ndarray]:
        """插件权重 → npz 字典 (无权重插件返回空字典)"""
        return {}

    def load_weights(self, weights: Dict[str, np.ndarray],
                     manifest: Dict[str, Any]) -> None:
        """从 npz 字典还原权重 (manifest 携带构造所需的扩展字段)"""

    # ── 清单与占用 ──────────────────────────────────────────
    def build_manifest(self, **extra: Any) -> Dict[str, Any]:
        """按规范 §4 生成 manifest (子类可扩展 extra 字段)"""
        from .pkg import CORE_VERSION
        manifest: Dict[str, Any] = {
            "standard_version": CURRENT_STANDARD_VERSION,
            "name": self.name,
            "base_model": self.BASE_MODEL,
            "capability": self.capability,
            "author": self.author,
            "version": self.version,
            "route": self.route,
            "lifecycle": ["on_load", "on_think", "on_unload"],
            "memory_budget": int(self.memory.footprint_bytes()),
            "min_core_version": "0.8.7",
            "memory_levels": list(PluginMemory.LEVELS),
            "core_version": CORE_VERSION,
            "format": "CuteMamen",
        }
        manifest.update(extra)
        return manifest

    def footprint_mb(self) -> float:
        """内存占用估算 (MB): 权重 + 三级记忆 (内存预算淘汰依据)"""
        weights = self.save_weights()
        w_bytes = sum(int(a.nbytes) for a in weights.values() if hasattr(a, "nbytes"))
        m_bytes = self.memory.footprint_bytes()
        return round((w_bytes + m_bytes) / (1024 * 1024), 6)

    def stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "base_model": self.BASE_MODEL,
            "route": self.route,
            "loaded": self.loaded,
            "think_count": self.think_count,
            "footprint_mb": self.footprint_mb(),
            "memory": self.memory.stats(),
        }


def _event_summary(event: Dict[str, Any]) -> Any:
    """情景记忆摘要: 只留 topic 与数据类型/形状, 不存大对象"""
    data = event.get("data")
    if data is None:
        return None
    if isinstance(data, np.ndarray):
        return {"shape": list(data.shape)}
    if isinstance(data, str):
        return data[:64]
    if isinstance(data, dict):
        return {"keys": list(data)[:8]}
    return str(type(data).__name__)
