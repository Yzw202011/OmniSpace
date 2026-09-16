"""CuteMamen 插件包 (.CuteMamen) 格式: 存档 / 加载 / manifest 解码器

包格式 (规范 §3, 单个 tar.gz):

    <name>.CuteMamen/
        manifest.json              ← 清单 (规范 §4)
        weights/weights.npz        ← 插件权重 (plugin.save_weights)
        memory/working.json        ← 三级记忆: 工作记忆
        memory/episodic.json       ← 三级记忆: 情景记忆
        memory/semantic.json       ← 三级记忆: 语义记忆

解码器层集中兼容 (规范 §2.1): 所有向前/向后版本适配集中在
decode_manifest — v1 的 model_type→base_model、on_init→on_load、
废弃 legacy_mode 删除; 未知字段一律保留 (前向兼容)。内核与插件
永远只看到解码后的统一内部格式。

v0.7.0 的 .dfpkg 模态面存档是本格式的首个特例 (manifest.format
== "dfpkg"), load_pkg 会自动分流到 FacePlugin 桥接。
"""

import io
import json
import os
import tarfile
import time
from typing import Any

import numpy as np

from .plugin import ExpertPlugin, PluginMemory

# .CuteMamen 后缀 (v0.7.0 模态面特例后缀)
CUTEMAMEN_SUFFIX = ".CuteMamen"
DFPKG_SUFFIX = ".dfpkg"

MANIFEST_PATH = "manifest.json"
WEIGHTS_PATH = "weights/weights.npz"
WORKING_PATH = "memory/working.json"
EPISODIC_PATH = "memory/episodic.json"
SEMANTIC_PATH = "memory/semantic.json"

# 内核版本 (min_core_version 检查用); 延迟读包版本避免循环导入
CORE_VERSION = "0.8.7"

# ── v1 → v2 解码规则 (规范 §2 / COMPATIBILITY.md §4) ──────────
V1_RENAMED_FIELDS = {"model_type": "base_model"}
V1_REMOVED_FIELDS = ("legacy_mode",)
V1_UNMAPPABLE_FIELDS = ("pipeline_config",)
V1_RENAMED_HOOKS = {"on_init": "on_load"}
REQUIRED_HOOKS = ("on_load", "on_think", "on_unload")


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except ValueError:
        return (0,)


def check_core_version(min_core_version: str,
                       core_version: str = CORE_VERSION) -> None:
    """规范 §4: 内核版本过旧则拒绝加载"""
    if _version_tuple(min_core_version) > _version_tuple(core_version):
        raise ValueError(
            f"插件要求内核 ≥ {min_core_version}, 当前内核 {core_version}, 拒绝加载"
        )


# ═══════════════════════════════════════════════════════════════
# 解码器: v1 → v2 集中适配 (规范 §2.1)
# ═══════════════════════════════════════════════════════════════

def decode_manifest(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """原始 manifest → 统一内部格式, 返回 (解码后清单, 变更日志)

    - v1 字段重命名: model_type → base_model
    - v1 废弃字段删除: legacy_mode
    - v1 无法映射字段: 保留原样并记日志 (migrate --strict 下报错)
    - 生命周期钩子重命名: on_init → on_load; 补缺失的 on_unload 桩
    - 未知字段一律保留不丢弃 (前向兼容)
    """
    changes: list[str] = []
    manifest = dict(raw)  # 未知字段全部保留

    # 字段重命名
    for old, new in V1_RENAMED_FIELDS.items():
        if old in manifest and new not in manifest:
            manifest[new] = manifest.pop(old)
            changes.append(f'renamed field "{old}" → "{new}"')

    # 废弃字段删除
    for field in V1_REMOVED_FIELDS:
        if field in manifest:
            manifest.pop(field)
            changes.append(f'removed deprecated field "{field}"')

    # 无法映射的 v1 字段: 保留 + 记日志
    for field in V1_UNMAPPABLE_FIELDS:
        if field in manifest:
            changes.append(f'field "{field}" has no v2 equivalent')

    # 生命周期钩子
    lifecycle = manifest.get("lifecycle")
    if isinstance(lifecycle, dict):
        for old, new in V1_RENAMED_HOOKS.items():
            if old in lifecycle and new not in lifecycle:
                lifecycle[new] = lifecycle.pop(old)
                changes.append(f'renamed hook "{old}" → "{new}"')
        for hook in REQUIRED_HOOKS:
            if hook not in lifecycle:
                lifecycle[hook] = "stub"
                changes.append(f'added missing hook "{hook}" (stub)')
        manifest["lifecycle"] = lifecycle
    elif lifecycle is None:
        changes.append('added lifecycle hooks (on_load/on_think/on_unload)')
        manifest["lifecycle"] = list(REQUIRED_HOOKS)

    # 标准版本
    std = manifest.get("standard_version")
    if std is None:
        changes.append('standard_version missing, assumed v1')
        std = "1.0.0"
    manifest["standard_version"] = str(std)
    manifest["_decoded"] = True
    return manifest, changes


def is_legacy_manifest(manifest: dict[str, Any]) -> bool:
    """是否 v1 清单 (standard_version 主版本 < 2 或缺失)"""
    std = str(manifest.get("standard_version", "1.0.0"))
    return _version_tuple(std) < (2,)


# ═══════════════════════════════════════════════════════════════
# 存档: plugin → .CuteMamen
# ═══════════════════════════════════════════════════════════════

def save_pkg(plugin: ExpertPlugin, path: str,
             source_py: str | None = None,
             **manifest_extra: Any) -> dict[str, Any]:
    """把插件存档为 .CuteMamen 包 (manifest + weights + 三级记忆), 返回清单

    source_py（v2.1，可选）：插件源码文本——内嵌为 source/plugin.py，
    供宿主单文件导入新类型插件（开发者分发推荐带此条目）。
    """
    manifest = plugin.build_manifest(**manifest_extra)
    manifest["memory_footprint_mb"] = plugin.footprint_mb()
    manifest["archived_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    weights = plugin.save_weights()
    memory = plugin.memory.archive()

    path = str(path)
    if not path.endswith((CUTEMAMEN_SUFFIX, DFPKG_SUFFIX)):
        path += CUTEMAMEN_SUFFIX
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        _add_bytes(tar, MANIFEST_PATH,
                   json.dumps(manifest, ensure_ascii=False, indent=2,
                              default=str).encode())
        buf = io.BytesIO()
        if weights:
            np.savez(buf, **weights)
        _add_bytes(tar, WEIGHTS_PATH, buf.getvalue())
        _add_json(tar, WORKING_PATH, {"entries": memory["working"],
                                      "capacities": memory["capacities"]})
        _add_json(tar, EPISODIC_PATH, {"events": memory["episodic"],
                                       "capacity": memory["capacities"]["episodic"]})
        _add_json(tar, SEMANTIC_PATH, {"entries": memory["semantic"],
                                       "capacity": memory["capacities"]["semantic"]})
        if source_py:
            _add_bytes(tar, "source/plugin.py",
                       source_py.encode("utf-8"))
    return manifest


# ═══════════════════════════════════════════════════════════════
# 加载: .CuteMamen → plugin
# ═══════════════════════════════════════════════════════════════

def read_raw_manifest(path: str) -> dict[str, Any]:
    """只读原始清单 (不经解码器, 特例分流用)"""
    with tarfile.open(str(path), "r:gz") as tar:
        member = _find_member(tar, MANIFEST_PATH)
        return json.loads(tar.extractfile(member).read().decode())


def read_manifest(path: str) -> dict[str, Any]:
    """只读清单 (注册表 / 预检用, 不加载权重); 已过解码器"""
    manifest, _ = decode_manifest(read_raw_manifest(path))
    return manifest


def load_pkg(path: str,
             plugin_cls: type | None = None) -> tuple[ExpertPlugin, dict[str, Any]]:
    """加载 .CuteMamen 包 → (插件实例, 解码后清单)

    plugin_cls=None 时按 manifest.base_model 从注册表分发:
        "cubegpt.face"  → FacePlugin (v0.7.0 .dfpkg 模态面 pkg 的通用化)
        "lora.adapter"  → LoRABridgePlugin
        "video.making"  → VideoMakingPlugin (v0.8.7 轻量视频生成内核)
    .dfpkg 包 (manifest.format == "dfpkg") 自动分流到 FacePlugin。
    """
    # v0.7.0 .dfpkg 特例: 成员结构不同, 走 face_pkg 原生读取
    raw_manifest = read_raw_manifest(path)
    if raw_manifest.get("format") == "dfpkg":
        from .face_bridge import FacePlugin
        manifest, _ = decode_manifest(raw_manifest)
        check_core_version(manifest.get("min_core_version", "0.7.0"))
        plugin = FacePlugin.from_pkg(manifest)
        from ..core import face_pkg
        _, params, connections, state = face_pkg._read_members(path)
        plugin.load_weights_dfpkg(params, connections, state)
        return plugin, manifest

    manifest, params, memory = _read_members(path)
    if plugin_cls is None:
        plugin_cls = _resolve_class(manifest)
    check_core_version(manifest.get("min_core_version", "0.7.0"))

    plugin = plugin_cls.from_pkg(manifest)
    plugin.load_weights(params, manifest)
    plugin.memory.restore(memory)
    plugin.memory.remember("pkg:origin", os.path.basename(str(path)))
    return plugin, manifest


# 按 base_model 分发的原生插件注册表 (规范 §6 集成路径的宿主侧)
def native_registry() -> dict[str, type]:
    from .bridge import LoRABridgePlugin
    from .face_bridge import FacePlugin
    from .rust_coding import RustCodingPlugin
    from .video_making import VideoMakingPlugin
    return {
        "cubegpt.face": FacePlugin,
        "lora.adapter": LoRABridgePlugin,
        "rust.coding": RustCodingPlugin,
        "video.making": VideoMakingPlugin,
    }


def _resolve_class(manifest: dict[str, Any]) -> type:
    fmt = manifest.get("format")
    if fmt == "dfpkg":
        from .face_bridge import FacePlugin
        return FacePlugin
    base = manifest.get("base_model", "")
    registry = native_registry()
    if base in registry:
        return registry[base]
    raise ValueError(
        f"无法解析 base_model {base!r} 对应的插件类; "
        f"原生注册表: {sorted(registry)}, 或显式传入 plugin_cls"
    )


# ═══════════════════════════════════════════════════════════════
# 内部: tar 成员读写
# ═══════════════════════════════════════════════════════════════

def _read_members(path: str) -> tuple[dict, dict, dict]:
    """读取包全部内容 → (解码后 manifest, weights npz, memory archive)"""
    with tarfile.open(str(path), "r:gz") as tar:
        raw = json.loads(
            tar.extractfile(_find_member(tar, MANIFEST_PATH)).read().decode())
        manifest, _ = decode_manifest(raw)

        weights: dict[str, np.ndarray] = {}
        member = _optional_member(tar, WEIGHTS_PATH)
        if member is not None:
            data = tar.extractfile(member).read()
            if len(data) > 0:
                # allow_pickle=False：npy/npz 安全体，对齐 OSP loader 同款
                weights = dict(np.load(io.BytesIO(data), allow_pickle=False))

        working = _read_json(tar, WORKING_PATH, {})
        episodic = _read_json(tar, EPISODIC_PATH, {})
        semantic = _read_json(tar, SEMANTIC_PATH, {})
    memory = {
        "working": working.get("entries", []),
        "episodic": episodic.get("events", []),
        "semantic": semantic.get("entries", []),
        "capacities": {
            "working": working.get("capacities", {}).get("working",
                          PluginMemory().working_capacity),
            "episodic": episodic.get("capacity",
                          PluginMemory().episodic_capacity),
            "semantic": semantic.get("capacity",
                          PluginMemory().semantic_capacity),
        },
    }
    return manifest, weights, memory


def _find_member(tar: tarfile.TarFile, name: str) -> tarfile.TarInfo:
    for member in tar.getmembers():
        if not member.isdir() and (member.name == name
                                   or member.name.endswith("/" + name)):
            return member
    raise ValueError(f"包中缺少 {name}")


def _optional_member(tar: tarfile.TarFile, name: str) -> tarfile.TarInfo | None:
    for member in tar.getmembers():
        if not member.isdir() and (member.name == name
                                   or member.name.endswith("/" + name)):
            return member
    return None


def _read_json(tar: tarfile.TarFile, name: str, default: Any) -> Any:
    member = _optional_member(tar, name)
    if member is None:
        return default
    return json.loads(tar.extractfile(member).read().decode())


def _add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def _add_json(tar: tarfile.TarFile, arcname: str, obj: Any) -> None:
    _add_bytes(tar, arcname,
               json.dumps(obj, ensure_ascii=False, default=str).encode())
