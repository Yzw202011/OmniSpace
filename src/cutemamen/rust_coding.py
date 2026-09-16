"""RustCodingPlugin: Rust coding 思考插件 (填补训练材料空白)

v0.8.6 起插件携带**主模型迁移知识** (新阶段 · 分布式架构):

    主模型 (冻结 CubeGPT 水库 + 线性读出层, training/readout.py)
        ──migrate_from_main_model()──▶  插件可存档权重 (W/mean/scale)
        ──.CuteMamen 包──▶ 内核路由即服务 (宿主无需主模型)

知识迁移与主模型验证协议逐位一致: 真实语料 → 双模态注入
(numeric=static+structure 语法/结构特征, text=代码原文) → 冻结
CubeGPT 水库特征 → 线性 softmax 读出层。迁移后 on_think 优先走
读出层; 无读出权重时回退到语料蒸馏原型 (最近质心)。

route = "rust": on_think 把一段 Rust 代码分类到 5 类编译错误之一,
并把结果发布到事件总线 ("rust.classified") 供其他插件订阅。
"""

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .plugin import ExpertPlugin, PluginContext

# 相对导入真实语料 (src.data.rust_coding)
try:
    from ..data.rust_coding import (
        LABELS, RUST_SNIPPETS, static_metrics, structure_metrics)
except Exception:  # pragma: no cover - 极罕见时序问题, 见 _ensure_corpus
    LABELS, RUST_SNIPPETS = [], []
    static_metrics = structure_metrics = None


class RustCodingPlugin(ExpertPlugin):
    """静态识别一段 Rust 代码命中哪一类编译错误的思考插件

    训练材料: 内嵌 100 段真实 Rust 代码 + 真实 rustc 错误类别 (move /
    borrow / lifetime / type / ok)。原型权重 = 每类别 static_metrics
    均值; 分类 = 最近原型 (特征空间欧氏距离), 置信度 = softmax(-距离)。

    事件格式: {"topic": "rust", "data": <代码 str 或 {"code": str}>}
    返回: {"label", "label_name", "rustc", "confidence"},
    并广播 "rust.classified"。
    """

    BASE_MODEL = "rust.coding"
    CAPABILITY = ("静态识别 Rust 代码命中的编译错误类别 "
                  "(move/borrow/lifetime/type/ok); v0.8.6 起可携带"
                  "主模型 (CubeGPT 水库读出层) 迁移知识")

    def __init__(self, name: str = "rust-coding", *,
                 route: Optional[str] = None, **kwargs):
        super().__init__(name, route=route or "rust", **kwargs)
        # 原型权重: label → 类别质心 (特征空间), 权重存档用
        self.prototypes: Dict[str, np.ndarray] = {}
        self._sample_counts: Dict[str, int] = {}
        self._corpus_loaded = False
        # 主模型迁移知识: {"W","mean","scale","depth","dim","seed"}
        # (线性读出层权重; None = 未迁移, on_think 回退原型路径)
        self.readout: Optional[Dict[str, Any]] = None
        self._extractor: Any = None   # 冻结 CubeGPT 水库特征提取器 (惰性)
        self._dataset: Any = None     # 语料数据集 (numeric 通路归一化, 惰性)
        self._feat_cache: Dict[str, np.ndarray] = {}  # 代码文本 → 水库特征

    # ── 真实训练材料 (填补训练数据空白) ─────────────────────
    def _ensure_corpus(self) -> None:
        """惰性加载真实语料并蒸馏每类别原型 (若权重未另行注入)"""
        if self._corpus_loaded:
            return
        if static_metrics is None:
            return
        features: Dict[str, List[np.ndarray]] = {lab: [] for lab in LABELS}
        for s in RUST_SNIPPETS:
            lab = s["label"]
            if lab in features:
                features[lab].append(static_metrics(s["code"]))
        for lab, arrs in features.items():
            if arrs:
                self.prototypes[lab] = np.mean(np.stack(arrs), axis=0)
                self._sample_counts[lab] = len(arrs)
        self._corpus_loaded = True

    def training_data(self) -> Tuple[np.ndarray, np.ndarray]:
        """导出真实训练材料: (特征矩阵, 标签索引), 供端到端训练/评估"""
        self._ensure_corpus()
        xs, ys = [], []
        for s in RUST_SNIPPETS:
            xs.append(static_metrics(s["code"]))
            ys.append(LABELS.index(s["label"]))
        return np.stack(xs), np.array(ys)

    def corpus_size(self) -> int:
        self._ensure_corpus()
        return sum(self._sample_counts.values())

    # ── 主模型知识迁移 (v0.8.6 新阶段 · 分布式架构) ──────────
    def migrate_from_main_model(self, train_samples: Optional[List] = None,
                                *, depth: int = 1, dim: int = 16,
                                seed: int = 0) -> "RustCodingPlugin":
        """把主模型的 Rust 开发知识迁移进本插件 (可存档权重)

        迁移路径与 training/readout.py 的主模型验证协议**逐位一致**:
            真实语料 → 双模态注入 → 冻结 CubeGPT 水库特征
            → 线性 softmax 读出层训练 → W/mean/scale 存为插件权重

        train_samples: RustCodingTrainingDataset 产出的训练样本;
            None = 全量 502 段真实语料 (部署形态: 迁移全部知识)。
        迁移后 on_think 优先走读出层; 原型路径保留为无权重回退。
        """
        from ..data.real_dataset import RustCodingTrainingDataset
        from ..training.readout import CubeFeatureExtractor, LinearReadout
        if train_samples is None:
            train_samples, _ = RustCodingTrainingDataset(
                dim=dim).generate_dataset(train_ratio=1.0, seed=seed)
        # 与主模型同构的冻结水库 (seed 确定 → 特征逐位可复现)
        extractor = CubeFeatureExtractor(depth=depth, dim=dim, seed=seed)
        X = np.stack([extractor.features(s.multimodal_input())
                      for s in train_samples])
        y = np.array([s.category for s in train_samples])
        readout = LinearReadout(n_features=X.shape[1],
                                n_classes=len(LABELS), seed=seed).fit(X, y)
        self.readout = {
            "W": np.asarray(readout.W, dtype=float),
            "mean": np.asarray(readout.mean_, dtype=float),
            "scale": np.asarray(readout.scale_, dtype=float),
            "depth": depth, "dim": dim, "seed": seed,
        }
        self._extractor = extractor  # 复用本轮已构建的冻结水库
        self.memory.remember("knowledge_source", "main-model-readout")
        self.memory.remember("migration_train_samples", len(train_samples))
        return self

    def _ensure_main_model_bridge(self) -> None:
        """惰性构建主模型侧设施: 冻结水库提取器 + 语料归一化数据集

        .CuteMamen 包只存读出权重与配置 (depth/dim/seed), 不存水库
        本体 —— 水库由 seed 确定性重建, 特征与训练时逐位一致。
        """
        from ..data.real_dataset import RustCodingTrainingDataset
        from ..training.readout import CubeFeatureExtractor
        cfg = self.readout
        if self._dataset is None:
            self._dataset = RustCodingTrainingDataset(dim=cfg["dim"])
        if self._extractor is None:
            self._extractor = CubeFeatureExtractor(
                depth=cfg["depth"], dim=cfg["dim"], seed=cfg["seed"])

    def _reservoir_features(self, code: str) -> np.ndarray:
        """代码原文 → 主模型冻结水库特征 (按代码文本缓存)

        双模态注入与训练协议一致: numeric = static(10) + structure(6)
        维语料归一化特征, text = 代码原文。
        """
        if code not in self._feat_cache:
            self._ensure_main_model_bridge()
            raw = np.concatenate([static_metrics(code), structure_metrics(code)])
            inputs = {"numeric": self._dataset._normalize_static(raw),
                      "text": code}
            self._feat_cache[code] = self._extractor.features(inputs)
        return self._feat_cache[code]

    def _classify_readout(self, code: str) -> Tuple[str, float]:
        """主模型迁移知识推理: 水库特征 → 线性读出 → (label, 置信度)"""
        f = self._reservoir_features(code)
        cfg = self.readout
        z = (f - cfg["mean"]) / cfg["scale"]
        logits = np.concatenate([z, [1.0]]) @ cfg["W"].T
        e = np.exp(logits - logits.max())
        probs = e / e.sum()
        idx = int(np.argmax(probs))
        return LABELS[idx], float(probs[idx])

    # ── 生命周期 ────────────────────────────────────────────
    def on_load(self, ctx: PluginContext) -> None:
        self._ensure_corpus()
        self.memory.set("base_model", self.BASE_MODEL)
        self.memory.set("corpus_size", self.corpus_size())
        self.memory.set("labels", LABELS)
        super().on_load(ctx)

    def on_think(self, event: Dict[str, Any],
                 ctx: PluginContext) -> Optional[Dict[str, Any]]:
        """分类一段 Rust 代码 → 5 类编译错误之一

        推理路径: 主模型迁移读出层 (有迁移权重时) → 语料原型回退。
        """
        super().on_think(event, ctx)
        code = _extract_code(event.get("data"))
        if code is None:
            return None
        self._ensure_corpus()
        f = static_metrics(code)  # 语法特征 (工作记忆写入用)
        if self.readout is not None:
            # 新阶段 · 分布式架构: 主模型迁移知识推理
            label, confidence = self._classify_readout(code)
        elif self.prototypes:
            probs = _classify(f, self.prototypes)
            label = LABELS[int(np.argmax(probs))]
            confidence = float(probs[np.argmax(probs)])
        else:
            return {"label": "ok", "label_name": "合法代码 (可编译)",
                    "rustc": "-", "confidence": 0.0, "needs_corpus": True}
        # 触发一次工作记忆写入 + 事件总线广播 (插件间通信)
        if ctx is not None:
            ctx.working_memory.push(
                f"rust_{ctx.kernel_version}_{len(ctx.working_memory.recent_events)}",
                f.astype(float), np.array([confidence]))
            ctx.emit("rust.classified",
                     {"label": label, "confidence": confidence,
                      "code_len": len(code),
                      "source": "main-model-readout" if self.readout
                      else "corpus-prototypes"})
        return {"label": label, "label_name": _label_name(label),
                "rustc": _rustc_for(label), "confidence": confidence,
                "source": "main-model-readout" if self.readout
                else "corpus-prototypes"}

    def on_unload(self) -> None:
        self.memory.consolidate()
        super().on_unload()

    # ── 权重序列化 (可学习权重 = 原型 + 主模型迁移读出层) ───
    def save_weights(self) -> Dict[str, np.ndarray]:
        self._ensure_corpus()
        out: Dict[str, np.ndarray] = {}
        for lab, proto in self.prototypes.items():
            out[f"proto_{lab}"] = np.asarray(proto, dtype=float)
        for lab, n in self._sample_counts.items():
            out[f"count_{lab}"] = np.array([n], dtype=int)
        if self.readout is not None:
            # 主模型迁移知识 (v0.8.6): 线性读出层 + 确定性重建配置
            out["readout_W"] = self.readout["W"]
            out["readout_mean"] = self.readout["mean"]
            out["readout_scale"] = self.readout["scale"]
            out["readout_cfg"] = np.array(
                [self.readout["depth"], self.readout["dim"],
                 self.readout["seed"]], dtype=int)
        return out

    def load_weights(self, weights: Dict[str, np.ndarray],
                     manifest: Dict[str, Any]) -> None:
        self.prototypes = {}
        self._sample_counts = {}
        for lab in LABELS:
            if f"proto_{lab}" in weights:
                self.prototypes[lab] = weights[f"proto_{lab}"].astype(float)
            if f"count_{lab}" in weights:
                self._sample_counts[lab] = int(weights[f"count_{lab}"][0])
        if self.prototypes:
            self._corpus_loaded = True  # 权重优先, 无需再从源码重蒸馏
        # 主模型迁移知识还原 (水库由 seed 确定性重建, 不随包存档)
        if "readout_W" in weights:
            depth, dim, seed = (int(x) for x in weights["readout_cfg"])
            self.readout = {
                "W": weights["readout_W"].astype(float),
                "mean": weights["readout_mean"].astype(float),
                "scale": weights["readout_scale"].astype(float),
                "depth": depth, "dim": dim, "seed": seed,
            }
            self._extractor = None  # 惰性重建冻结水库
            self._feat_cache = {}

    def build_manifest(self, **extra: Any) -> Dict[str, Any]:
        self._ensure_corpus()
        extra.setdefault("capability", self.CAPABILITY)
        extra.setdefault("corpus_size", self.corpus_size())
        extra.setdefault("labels", LABELS)
        extra.setdefault(
            "knowledge_source",
            "main-model-readout" if self.readout else "corpus-prototypes")
        if self.readout is not None:
            extra.setdefault("readout", {
                "depth": self.readout["depth"], "dim": self.readout["dim"],
                "seed": self.readout["seed"],
                "n_features": int(self.readout["W"].shape[1]) - 1,
                "n_classes": int(self.readout["W"].shape[0]),
            })
        return super().build_manifest(**extra)

    def stats(self) -> Dict[str, Any]:
        s = super().stats()
        s["labels"] = LABELS
        s["corpus_size"] = self.corpus_size()
        s["prototypes"] = {lab: len(proto)
                           for lab, proto in self.prototypes.items()}
        s["knowledge_source"] = ("main-model-readout" if self.readout
                                 else "corpus-prototypes")
        return s


# ── 辅助 ──────────────────────────────────────────────────

def _extract_code(data: Any) -> Optional[str]:
    """从事件取代码文本: 支持 str 或 {"code": str}"""
    if isinstance(data, str):
        return data
    if isinstance(data, dict) and isinstance(data.get("code"), str):
        return data["code"]
    if isinstance(data, dict) and "code" in data:
        return str(data["code"])
    return None


def _classify(feature: np.ndarray,
              prototypes: Dict[str, np.ndarray]) -> np.ndarray:
    """最近原型 (欧氏距离) + softmax(-距离) 给出类别置信度分布"""
    labels = LABELS
    d = np.array([float(np.linalg.norm(np.asarray(feature, dtype=float)
                                       - prototypes[lab]))
                  for lab in labels if lab in prototypes])
    present = [lab for lab in labels if lab in prototypes]
    if not present:
        return np.ones(len(labels)) / len(labels) * np.nan
    # softmax(-d), 距离越近置信度越高
    e = np.exp(-d - np.max(-d))
    probs = e / e.sum()
    out = np.zeros(len(labels))
    for i, lab in enumerate(present):
        out[LABELS.index(lab)] = probs[i]
    return out


_RUSTC_BY_LABEL = {
    "move": "E0382/E0505/E0507",
    "borrow": "E0502/E0499",
    "lifetime": "E0597/E0106/E0515/E0716",
    "type": "E0308/E0277/E0599/E0300",
    "ok": "-",
}


def _rustc_for(label: str) -> str:
    return _RUSTC_BY_LABEL.get(label, "-")


def _label_name(label: str) -> str:
    names = {
        "move": "所有权移动",
        "borrow": "借用冲突",
        "lifetime": "生命周期",
        "type": "类型不匹配",
        "ok": "合法代码 (可编译)",
    }
    return names.get(label, label)