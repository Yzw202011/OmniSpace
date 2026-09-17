"""CuteMamen 插件标准落地 (v0.7.2) — 规范 v0.1.0 包格式 / v2 清单

通用固定内核 (路由器 + 工作记忆 + 插件注册表) 与 .CuteMamen 专家
插件包 (on_load/on_think/on_unload 生命周期钩子、三级记忆存档、
事件总线通信、内存预算淘汰、LoRA/Adapter 兼容桥接)。
v0.7.0 的 .dfpkg 模态面存档是本标准的首个特例 (face_bridge.FacePlugin)。
"""

from .bridge import LoRAAdapter, LoRABridgePlugin, apply_lora, lora_from_weight
from .event_bus import EventBus
from .face_bridge import FacePlugin
from .kernel import CubeGPTKernel, CuteMamenKernel, WorkingMemory
from .migrate import main as migrate_main
from .pkg import (
    check_core_version,
    decode_manifest,
    load_pkg,
    read_manifest,
    save_pkg,
)
from .plugin import (
    CURRENT_STANDARD_VERSION,
    ExpertPlugin,
    PluginContext,
    PluginMemory,
)
from .rust_coding import RustCodingPlugin

__all__ = [
    "CURRENT_STANDARD_VERSION",
    "ExpertPlugin",
    "PluginContext",
    "PluginMemory",
    "EventBus",
    "check_core_version",
    "decode_manifest",
    "load_pkg",
    "read_manifest",
    "save_pkg",
    "LoRAAdapter",
    "LoRABridgePlugin",
    "apply_lora",
    "lora_from_weight",
    "FacePlugin",
    "RustCodingPlugin",
    "CubeGPTKernel",
    "CuteMamenKernel",
    "WorkingMemory",
    "migrate_main",
]
