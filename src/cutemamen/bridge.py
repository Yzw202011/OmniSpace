"""CuteMamen ↔ LoRA/Adapter 兼容桥接 (规范 §6 集成路径)

- LoRAAdapter: 低秩适配器 (target / rank / A / B / alpha),
  ΔW = (alpha/rank) · B @ A
- apply_lora: 把适配器合并进任意基础权重 (W' = W + ΔW)
- LoRABridgePlugin: 把 LoRA 适配器包装成 .CuteMamen 专家插件 —
  on_think 计算 ΔW·x 的低秩贡献, 与外部 Transformer 基座互联
- lora_from_weight: 任意线性权重 SVD 低秩分解 → LoRA 适配器
  (把任一插件的读出权重导出为 LoRA 形态, 供 Transformer 宿主挂载)
"""

from typing import Any

import numpy as np

from .plugin import ExpertPlugin, PluginContext


class LoRAAdapter:
    """LoRA 低秩适配器: ΔW = (alpha / rank) · B @ A

    A: (rank, in_dim), B: (out_dim, rank)
    """

    def __init__(self, target: str, a: np.ndarray, b: np.ndarray,
                 alpha: float = 1.0):
        if a.ndim != 2 or b.ndim != 2:
            raise ValueError("A/B 必须是 2D 矩阵")
        if b.shape[1] != a.shape[0]:
            raise ValueError(
                f"形状不匹配: B {b.shape} @ A {a.shape} 无法相乘")
        self.target = target
        self.a = np.asarray(a, dtype=np.float64)
        self.b = np.asarray(b, dtype=np.float64)
        self.alpha = float(alpha)

    @property
    def rank(self) -> int:
        return int(self.a.shape[0])

    @property
    def in_dim(self) -> int:
        return int(self.a.shape[1])

    @property
    def out_dim(self) -> int:
        return int(self.b.shape[0])

    @property
    def scale(self) -> float:
        return self.alpha / self.rank

    def delta(self) -> np.ndarray:
        """权重增量 ΔW = scale · B @ A, 形状 (out_dim, in_dim)"""
        return self.scale * (self.b @ self.a)

    def forward(self, x: np.ndarray) -> np.ndarray:
        """低秩路径前向: scale · B @ (A @ x)"""
        x = np.asarray(x, dtype=np.float64).reshape(-1)
        if x.shape[0] != self.in_dim:
            raise ValueError(
                f"输入维度 {x.shape[0]} != 适配器 in_dim {self.in_dim}")
        return self.scale * (self.b @ (self.a @ x))

    def stats(self) -> dict[str, Any]:
        return {"target": self.target, "rank": self.rank,
                "in_dim": self.in_dim, "out_dim": self.out_dim,
                "alpha": self.alpha,
                "params": int(self.a.size + self.b.size)}


def apply_lora(base_weight: np.ndarray, adapter: LoRAAdapter) -> np.ndarray:
    """把 LoRA 适配器合并进基础权重: W' = W + ΔW"""
    base = np.asarray(base_weight, dtype=np.float64)
    delta = adapter.delta()
    if base.shape != delta.shape:
        raise ValueError(
            f"基础权重形状 {base.shape} 与 ΔW 形状 {delta.shape} 不一致")
    return base + delta


def lora_from_weight(target: str, weight: np.ndarray, rank: int,
                     alpha: float = 1.0) -> LoRAAdapter:
    """任意线性权重 → SVD 低秩 LoRA 适配器 (导出路径)"""
    w = np.asarray(weight, dtype=np.float64)
    if w.ndim != 2:
        raise ValueError("只支持 2D 权重的低秩分解")
    rank = max(1, min(rank, min(w.shape)))
    u, s, vt = np.linalg.svd(w, full_matrices=False)
    # A = diag(s[:rank]) @ Vt[:rank], B = U[:, :rank]
    a = np.diag(s[:rank]) @ vt[:rank]
    b = u[:, :rank]
    return LoRAAdapter(target, a, b, alpha=alpha)


class LoRABridgePlugin(ExpertPlugin):
    """LoRA 适配器 ↔ CuteMamen 插件桥接

    on_think(event): event["data"] 为 in_dim 维向量, 返回
    {"output": adapter.forward(x), "target": adapter.target} —
    适配器对输入的低秩贡献。结果发布到 plugin.<name>.output,
    供其他插件或 Transformer 基座消费。
    """

    BASE_MODEL = "lora.adapter"
    CAPABILITY = "LoRA 低秩适配器桥接 (ΔW·x 低秩贡献计算)"

    def __init__(self, name: str, adapter: LoRAAdapter | None = None,
                 **kwargs):
        super().__init__(name, **kwargs)
        self.adapter = adapter

    @classmethod
    def from_pkg(cls, manifest: dict[str, Any]) -> "LoRABridgePlugin":
        # 权重 (A/B/alpha/target) 由 load_weights 注入, 这里先建壳
        return cls(manifest["name"], adapter=None,
                   route=manifest.get("route"),
                   capability=manifest.get("capability", ""),
                   author=manifest.get("author", "DistributedFormer"),
                   version=manifest.get("version", "0.1.0"))

    def on_load(self, ctx: PluginContext) -> None:
        super().on_load(ctx)
        if self.adapter is None:
            raise ValueError(
                f"LoRA 插件 {self.name!r} 缺少适配器 (load_weights 未注入?)")

    def on_think(self, event: dict[str, Any],
                 ctx: PluginContext) -> dict[str, Any] | None:
        base = super().on_think(event, ctx)
        data = event.get("data")
        if data is None or self.adapter is None:
            return base
        x = np.asarray(data, dtype=np.float64).reshape(-1)
        if x.shape[0] < self.adapter.in_dim:
            x = np.concatenate([x, np.zeros(self.adapter.in_dim - x.shape[0])])
        elif x.shape[0] > self.adapter.in_dim:
            x = x[:self.adapter.in_dim]
        out = self.adapter.forward(x)
        self.memory.set("last_output", out.tolist())
        return {"output": out, "target": self.adapter.target}

    def on_unload(self) -> None:
        # 情景记忆蒸馏到语义层后卸载
        self.memory.consolidate()
        super().on_unload()

    def save_weights(self) -> dict[str, np.ndarray]:
        if self.adapter is None:
            return {}
        return {
            "lora_a": self.adapter.a,
            "lora_b": self.adapter.b,
            "lora_alpha": np.array([self.adapter.alpha]),
        }

    def load_weights(self, weights: dict[str, np.ndarray],
                     manifest: dict[str, Any]) -> None:
        if "lora_a" not in weights:
            raise ValueError("LoRA 插件包缺少 lora_a/lora_b 权重")
        self.adapter = LoRAAdapter(
            target=manifest.get("lora_target", manifest.get("name", "base")),
            a=weights["lora_a"], b=weights["lora_b"],
            alpha=float(weights["lora_alpha"][0])
            if "lora_alpha" in weights else 1.0)

    def build_manifest(self, **extra: Any) -> dict[str, Any]:
        extra2 = dict(extra)
        if self.adapter is not None:
            extra2.setdefault("lora_target", self.adapter.target)
            extra2.setdefault("lora_rank", self.adapter.rank)
            extra2.setdefault("lora_in_dim", self.adapter.in_dim)
            extra2.setdefault("lora_out_dim", self.adapter.out_dim)
        return super().build_manifest(**extra2)

    def to_lora(self) -> LoRAAdapter:
        """导出为 LoRA 适配器 (供外部 Transformer 宿主挂载, 规范 §6)"""
        if self.adapter is None:
            raise ValueError(f"插件 {self.name!r} 没有可导出的适配器")
        return self.adapter

    def footprint_mb(self) -> float:
        if self.adapter is None:
            return round(self.memory.footprint_bytes() / (1024 * 1024), 6)
        return round((self.adapter.a.nbytes + self.adapter.b.nbytes
                      + self.memory.footprint_bytes()) / (1024 * 1024), 6)

    def stats(self) -> dict[str, Any]:
        s = super().stats()
        if self.adapter is not None:
            s["adapter"] = self.adapter.stats()
        return s
