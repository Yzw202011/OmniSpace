"""FacePlugin: CubeGPT 模态面 ↔ CuteMamen 专家插件桥接

v0.7.0 的 .dfpkg 模态面存档是 CuteMamen 插件标准的**首个特例**:
一个 CubeFace (输入端口 + 分形皮层) 被包装为一个思考插件 —
on_load 构建面 / on_think 驱动面皮层计算 / on_unload 由内核存档。
权重序列化完全复用 core.face_pkg 的逐位可复现格式
(16 标量参数 + STDP 后小世界连接 + 单元状态)。
"""

import json
from typing import Any, Dict, Optional

import numpy as np

from .plugin import ExpertPlugin, PluginContext

# 延迟导入 core 避免包级循环 (core 仅在 to_kernel 中反向引用本包)


class FacePlugin(ExpertPlugin):
    """模态面思考插件: 一个 CubeFace = 一个 CuteMamen 专家插件

    route = 模态名 (内核按模态路由输入到本插件)。
    事件格式 (内核注入):
        {"topic": 模态, "data": 原始数据, "modulation": 节律,
         "attn": KV 检索向量, "lateral": 环形棱邻面脉冲}
    返回: {"outgoing": 发往邻面的聚合向量, "spike_count": 脉冲数}
    """

    BASE_MODEL = "cubegpt.face"

    def __init__(self, modality: str, depth: int = 1, dim: int = 16,
                 face: Optional[Any] = None, **kwargs):
        kwargs.setdefault("route", modality)
        super().__init__(modality, **kwargs)
        self.modality = modality
        self.depth = depth
        self.dim = dim
        self.face = face  # CubeFace (on_load 时若为 None 则构建)

    # ── 生命周期 ────────────────────────────────────────────
    def on_load(self, ctx: PluginContext) -> None:
        from ..core.distributedformer import CubeFace
        if self.face is None:
            self.face = CubeFace(self.modality, depth=self.depth, dim=self.dim)
        self.memory.set("modality", self.modality)
        self.memory.set("depth", self.depth)
        super().on_load(ctx)

    def on_think(self, event: Dict[str, Any],
                 ctx: PluginContext) -> Optional[Dict[str, Any]]:
        """驱动模态面皮层计算 (真正的思考在这里, 不在内核)"""
        if self.face is None:
            self.on_load(ctx)
        super().on_think(event, ctx)
        data = event.get("data")
        lateral = np.asarray(event.get("lateral",
                                       np.zeros(self.dim)), dtype=float)
        if np.any(lateral):
            self.face.inbox = lateral
        spikes = self.face.step(
            data if data is not None else np.zeros(self.dim),
            ctx.working_memory.kv_stack,
            float(event.get("modulation", 1.0)),
            bool(event.get("training_mode", False)),
            event.get("attn"),
        )
        outgoing = self.face.collect_outgoing()
        self.memory.set("last_outgoing", outgoing.tolist())
        self.memory.set("last_spike_count", len(spikes))
        return {"outgoing": outgoing, "spike_count": len(spikes),
                "spikes": spikes}

    def on_unload(self) -> None:
        # 权重与状态由内核随 .CuteMamen 存档, 这里蒸馏情景记忆
        self.memory.consolidate()
        super().on_unload()

    # ── 权重序列化 (复用 v0.7.0 .dfpkg 逐位可复现格式) ────────
    def save_weights(self) -> Dict[str, np.ndarray]:
        from ..core import face_pkg
        if self.face is None:
            return {}
        params, connections, state = face_pkg._face_to_arrays(self.face)
        weights = dict(params)
        weights["inbox"] = state["inbox"]
        # 小世界连接以 JSON 字节串存 npz (保持 face_pkg 真值格式)
        weights["connections_json"] = np.frombuffer(
            json.dumps(connections, ensure_ascii=False).encode("utf-8"),
            dtype=np.uint8)
        for key in ("state", "fatigue", "refractory", "spike_count",
                    "ltp_count", "ltd_count", "total_weight_change"):
            weights[f"mem_{key}"] = state[key]
        return weights

    def load_weights(self, weights: Dict[str, np.ndarray],
                     manifest: Dict[str, Any]) -> None:
        from ..core import face_pkg
        from ..core.distributedformer import CubeFace
        self.depth = int(manifest.get("depth", 1))
        self.dim = int(manifest.get("dim", 16))
        face = CubeFace(self.modality, depth=self.depth, dim=self.dim)
        params = {k: weights[k] for k in face_pkg.UNIT_PARAM_NAMES}
        params["dim"] = np.array([self.dim])
        params["n_port_units"] = weights["n_port_units"]
        params["n_cortex_units"] = weights["n_cortex_units"]
        connections = json.loads(
            weights["connections_json"].tobytes().decode("utf-8"))
        state = {
            "state": weights["mem_state"],
            "fatigue": weights["mem_fatigue"],
            "refractory": weights["mem_refractory"],
            "spike_count": weights["mem_spike_count"],
            "ltp_count": weights["mem_ltp_count"],
            "ltd_count": weights["mem_ltd_count"],
            "total_weight_change": weights["mem_total_weight_change"],
            "inbox": weights["inbox"],
        }
        face_pkg._arrays_to_face(face, params, connections, state)
        self.face = face

    def load_weights_dfpkg(self, params: Dict, connections: Dict,
                           state: Dict) -> None:
        """直接从 .dfpkg 的 (params, connections, state) 三元组还原"""
        from ..core import face_pkg
        from ..core.distributedformer import CubeFace
        self.depth = _depth_from_units(int(params["n_cortex_units"][0]))
        self.dim = int(params["dim"][0])
        face = CubeFace(self.modality, depth=self.depth, dim=self.dim)
        face_pkg._arrays_to_face(face, params, connections, state)
        self.face = face

    @classmethod
    def from_pkg(cls, manifest: Dict[str, Any]) -> "FacePlugin":
        # .dfpkg 特例: manifest 字段为 modality/depth/dim
        modality = manifest.get("modality") or manifest.get("name")
        return cls(modality,
                   depth=int(manifest.get("depth", 1)),
                   dim=int(manifest.get("dim", 16)))

    def build_manifest(self, **extra: Any) -> Dict[str, Any]:
        from ..core import face_pkg
        if self.face is not None:
            units = len(face_pkg._collect_units(self.face))
            extra.setdefault("depth", self.depth)
            extra.setdefault("dim", self.dim)
            extra.setdefault("modality", self.modality)
            extra.setdefault("units", units)
            extra.setdefault("params", units * 16)
        extra.setdefault("capability",
                         f"{self.modality} 模态面思考插件 (端口 + 分形皮层)")
        return super().build_manifest(**extra)

    def footprint_mb(self) -> float:
        from ..core import face_pkg
        if self.face is None:
            return round(self.memory.footprint_bytes() / (1024 * 1024), 6)
        units = len(face_pkg._collect_units(self.face))
        return round(units * 16 * 8 / (1024 * 1024)
                     + self.memory.footprint_bytes() / (1024 * 1024), 6)

    def stats(self) -> Dict[str, Any]:
        s = super().stats()
        s["modality"] = self.modality
        s["depth"] = self.depth
        if self.face is not None:
            s["units"] = len(self.face.get_units())
        return s


def _depth_from_units(cortex_units: int) -> int:
    """皮层单元数 → 分形深度 (Σ16^k, k=1..depth+1)"""
    total, depth = 0, -1
    while total < cortex_units:
        depth += 1
        total += 16 ** (depth + 1)
    return depth
