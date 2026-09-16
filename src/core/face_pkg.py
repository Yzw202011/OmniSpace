"""模态面 pkg 存档 (.dfpkg) — v0.7.0 模态面独立化

把 CubeGPT 的单个模态面 (CubeFace) 打包为独立、自包含的 .dfpkg 存档,
支持自由导入导出与随用随载热加载。包格式遵循 CuteMamen 插件规范 v0.1.0
的精神: 单个压缩包, manifest.json 清单 + weights 权重 + memory 记忆状态。

.dfpkg 内部结构 (tar.gz):
    <name>.dfpkg/
        manifest.json        ← 清单 (名称/版本/模态/规模/兼容性)
        weights/params.npz   ← 全部单元的 16 标量参数
        weights/connections.json ← 小世界网络连接 (STDP 更新对象)
        memory/state.npz     ← 单元状态 (state/fatigue/refractory/计数)

关键设计:
- 单元对象是权重与状态的唯一真源 (FractalLayer 每步双向同步 vec 数组),
  直接从 SpikingUnit 读取; _receptive 感受野投影由 layer_id 的 crc32
  种子确定复现, 无需存档。
- 导入时按相同构造顺序重建单元 (unit_id 由 modality/layer_id 决定,
  确定性), 再逐参数覆盖, 保证权重与状态逐位还原。
- min_core_version 检查: 内核版本过旧时拒绝加载 (CuteMamen §11)。
"""

import io
import json
import os
import tarfile
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

from .distributedformer import (
    SUPPORTED_MODALITIES,
    CubeFace,
    SpikingUnit,
)

DFPKG_SUFFIX = ".dfpkg"
MANIFEST_PATH = "manifest.json"
PARAMS_PATH = "weights/params.npz"
CONNECTIONS_PATH = "weights/connections.json"
STATE_PATH = "memory/state.npz"

# 本 pkg 格式的版本
PKG_FORMAT_VERSION = "0.1.0"

# SpikingUnit 的 16 个标量参数 (顺序固定, 单独存列)
UNIT_PARAM_NAMES = (
    "w_in", "b_in", "w_state", "w_out", "b_out",
    "w_attn", "b_attn", "decay", "gain", "w_global",
    "threshold", "refractory_period", "fatigue_rate",
    "recovery_rate", "spontaneous_rate", "w_lateral",
)

MODALITY_CAPABILITY = {
    "numeric": "数值信号编码与脉冲计算",
    "text": "文本编码与语义脉冲计算",
    "timeseries": "时间序列编码与趋势脉冲计算",
    "image": "图像编码与视觉脉冲计算",
}


def _version_tuple(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in v.split(".")[:3])


def check_core_version(min_core_version: str,
                       core_version: str) -> None:
    """CuteMamen §11: 内核版本过旧则拒绝加载"""
    if _version_tuple(min_core_version) > _version_tuple(core_version):
        raise ValueError(
            f"pkg 要求内核 ≥ {min_core_version}, 当前内核 {core_version}, 拒绝加载"
        )


# ── 序列化 ────────────────────────────────────────────────

def _collect_units(face: CubeFace) -> List[SpikingUnit]:
    """确定性顺序: 端口 16 单元 → 皮层全部单元 (缓存序)"""
    return list(face.port.units) + list(face.cortex._all_units_cache)


def _face_to_arrays(face: CubeFace) -> Tuple[Dict, Dict, Dict]:
    """CubeFace → (params npz 字典, connections json, state npz 字典)"""
    units = _collect_units(face)
    n = len(units)
    params: Dict[str, np.ndarray] = {}
    for p in UNIT_PARAM_NAMES:
        params[f"{p}"] = np.array([getattr(u, p) for u in units], dtype=np.float64)
    params["dim"] = np.array([face.dim])
    params["n_port_units"] = np.array([len(face.port.units)])
    params["n_cortex_units"] = np.array([len(face.cortex._all_units_cache)])

    connections = {
        u.unit_id: {t: float(w) for t, w in u.outgoing.items()}
        for u in units if u.outgoing
    }

    state = {
        "state": np.stack([u.state for u in units]),
        "fatigue": np.array([u.fatigue for u in units]),
        "refractory": np.array([u.refractory for u in units], dtype=np.int32),
        "spike_count": np.array([u.spike_count for u in units], dtype=np.int64),
        "ltp_count": np.array([u.ltp_count for u in units], dtype=np.int64),
        "ltd_count": np.array([u.ltd_count for u in units], dtype=np.int64),
        "total_weight_change": np.array([u.total_weight_change for u in units]),
        "inbox": np.array(face.inbox),
    }
    return params, connections, state


def _arrays_to_face(face: CubeFace, params: Dict, connections: Dict,
                    state: Dict) -> None:
    """逐参数覆盖到 (新建的) CubeFace 上"""
    units = _collect_units(face)
    n_params = int(params["n_port_units"][0]) + int(params["n_cortex_units"][0])
    if len(units) != n_params:
        raise ValueError(
            f"pkg 单元数 {n_params} 与目标面单元数 {len(units)} 不一致 "
            f"(depth/dim 不匹配?)"
        )
    for i, u in enumerate(units):
        for p in UNIT_PARAM_NAMES:
            setattr(u, p, float(params[p][i]))
        # 连接完全覆盖 (小世界连接是随机构建的, pkg 里的是训练后的真值)
        u.outgoing = dict(connections.get(u.unit_id, {}))
        u.state = state["state"][i].copy()
        u.fatigue = float(state["fatigue"][i])
        u.refractory = int(state["refractory"][i])
        u.spike_count = int(state["spike_count"][i])
        u.ltp_count = int(state["ltp_count"][i])
        u.ltd_count = int(state["ltd_count"][i])
        u.total_weight_change = float(state["total_weight_change"][i])
    face.inbox = state["inbox"].copy()
    # 皮层 vec 数组从单元重建 (状态/权重全部回到向量化计算路径)
    face.cortex._build_vec_arrays()


def build_manifest(face: CubeFace, *, author: str = "DistributedFormer",
                   capability: Optional[str] = None,
                   tags: Optional[List[str]] = None,
                   version: str = PKG_FORMAT_VERSION,
                   min_core_version: str = "0.7.0") -> Dict:
    """按 CuteMamen 规范 §4 生成模态面清单"""
    units = _collect_units(face)
    n_params = len(units) * 16
    footprint_mb = round(n_params * 8 / (1024 * 1024), 4)  # float64
    return {
        "name": face.modality,
        "version": version,
        "author": author,
        "capability": capability or MODALITY_CAPABILITY.get(
            face.modality, f"{face.modality} 模态脉冲计算"),
        "tags": tags or ["face", face.modality, "cubegpt"],
        "modality": face.modality,
        "input_schema": {"type": face.modality,
                         "description": f"{face.modality} 原始数据"},
        "output_schema": {"type": "spikes",
                          "description": "皮层脉冲消息与激活模式"},
        "memory_footprint_mb": footprint_mb,
        "lifecycle": {"on_load": "native", "on_unload": "native",
                      "on_think": "native"},
        "dependencies": [],
        "min_core_version": min_core_version,
        # v0.7.2: CuteMamen 规范字段 (base_model → FacePlugin 桥接)
        "standard_version": "2.0.0",
        "base_model": "cubegpt.face",
        # DistributedFormer 扩展字段
        "format": "dfpkg",
        "format_version": PKG_FORMAT_VERSION,
        "depth": face.depth,
        "dim": face.dim,
        "port_units": len(face.port.units),
        "cortex_units": len(face.cortex._all_units_cache),
        "units": len(units),
        "params": n_params,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def export_face(gpt, modality: str, path: str, **manifest_kwargs) -> Dict:
    """把 gpt 的一个模态面导出为 .dfpkg 存档, 返回清单"""
    if modality not in gpt.faces:
        raise KeyError(
            f"模态面 {modality!r} 未加载 (已加载: {list(gpt.faces)}, "
            f"已注册: {list(getattr(gpt, '_face_registry', {}))})"
        )
    face = gpt.faces[modality]
    manifest = build_manifest(face, **manifest_kwargs)

    params, connections, state = _face_to_arrays(face)

    path = str(path)
    if not path.endswith(DFPKG_SUFFIX):
        path += DFPKG_SUFFIX
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        _add_bytes(tar, MANIFEST_PATH,
                   json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        buf = io.BytesIO()
        np.savez(buf, **params)
        _add_bytes(tar, PARAMS_PATH, buf.getvalue())
        _add_bytes(tar, CONNECTIONS_PATH,
                   json.dumps(connections, ensure_ascii=False).encode())
        buf = io.BytesIO()
        np.savez(buf, **state)
        _add_bytes(tar, STATE_PATH, buf.getvalue())
    return manifest


def _add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def read_manifest(path: str) -> Dict:
    """只读清单 (不加载权重, 注册表/预检用)"""
    with tarfile.open(str(path), "r:gz") as tar:
        member = _find_member(tar, MANIFEST_PATH)
        return json.loads(tar.extractfile(member).read().decode())


def _find_member(tar: tarfile.TarFile, name: str) -> tarfile.TarInfo:
    for member in tar.getmembers():
        if not member.isdir() and (
                member.name == name
                or member.name.endswith("/" + name)):
            return member
    raise ValueError(f"pkg 中缺少 {name}")


def _read_members(path: str) -> Tuple[Dict, Dict, Dict, Dict]:
    """读取 .dfpkg 全部内容 → (manifest, params, connections, state)"""
    with tarfile.open(str(path), "r:gz") as tar:
        manifest = json.loads(
            tar.extractfile(_find_member(tar, MANIFEST_PATH)).read().decode())
        if manifest.get("format") not in (None, "dfpkg"):
            raise ValueError(f"不支持的 pkg 格式: {manifest.get('format')}")
        params = dict(np.load(io.BytesIO(
            tar.extractfile(_find_member(tar, PARAMS_PATH)).read())))
        connections = json.loads(
            tar.extractfile(_find_member(tar, CONNECTIONS_PATH)).read().decode())
        state = dict(np.load(io.BytesIO(
            tar.extractfile(_find_member(tar, STATE_PATH)).read())))
    return manifest, params, connections, state


def import_face(gpt, path: str, modality: Optional[str] = None) -> Dict:
    """把 .dfpkg 导入 gpt: 替换同模态面或新增模态面, 返回清单

    modality=None 时用清单里的模态名; 显式传入可把一个面导入为另一种
    模态 (权重保留, 编码器换成目标模态)。
    """
    import src as _pkg
    manifest, params, connections, state = _read_members(path)
    check_core_version(manifest.get("min_core_version", "0.7.0"),
                       _pkg.__version__)
    face_mod = modality or manifest.get("modality") or manifest.get("name")
    if face_mod not in SUPPORTED_MODALITIES:
        raise ValueError(
            f"pkg 模态 {face_mod!r} 不受支持, 可选: {SUPPORTED_MODALITIES}")
    if modality and modality != manifest.get("modality"):
        manifest = dict(manifest, modality=modality, reimported_from=manifest.get("modality"))

    depth = int(manifest["depth"])
    dim = int(manifest["dim"])
    if dim != gpt.dim:
        raise ValueError(f"pkg dim={dim} 与模型 dim={gpt.dim} 不一致")

    face = CubeFace(face_mod, depth=depth, dim=dim)
    _arrays_to_face(face, params, connections, state)

    gpt.faces[face_mod] = face
    gpt._rebuild_ring()
    gpt._build_units_map()
    gpt._face_registry.pop(face_mod, None)
    return manifest


def register_pkg(gpt, path: str, modality: Optional[str] = None) -> Dict:
    """注册 pkg 路径到随用随载注册表 (不加载权重)"""
    import src as _pkg
    manifest = read_manifest(path)
    check_core_version(manifest.get("min_core_version", "0.7.0"),
                       _pkg.__version__)
    face_mod = modality or manifest.get("modality") or manifest.get("name")
    gpt._face_registry[face_mod] = str(path)
    return manifest
