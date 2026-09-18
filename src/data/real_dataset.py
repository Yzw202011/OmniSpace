"""
真实训练数据集 (v0.8.4) — 训练数据真实化

100% 真实数据: 本模块不含任何合成/随机生成的训练样本。全部数据来自
[`src.data.rust_coding`](rust_coding.py) 的真实 Rust
编码基准语料 —— 500 段真实风格代码 (v0.8.4 P2 扩充: rustc 错误索引
官方样例 + 真实 crate 编译失败样本) × 5 类真实 rustc 编译错误族
(move / borrow / lifetime / type / ok), 每段附带真实 rustc 错误码
与报错信息。

样本编码 (真实数据 → 脉冲信号, v0.7.5+ 双模态注入 P0):
- input_signal: `SpikeEncoder.encode_text(代码原文)` — 代码感知
  TF-IDF 脉冲编码 (保留 & ' -> :: 等代码 token, crc32 确定性哈希),
  真实代码文本直接驱动脉冲网络
- static_signal: `static_metrics` 语法扫描 (10 维) + `structure_metrics`
  结构感知 (6 维: &mut 数/返回引用/类型标注/println/let 绑定/防御调用,
  针对 move↔lifetime 与 type→ok 混淆源设计), 拼成 16 维走 numeric 通路
- multimodal_input(): 返回 {"numeric": static_signal 归一化,
  "text": 代码原文} 双模态输入字典, 供 CubeGPT / CubeFeatureExtractor
  的 step() 直接消费——判别信息不再在词袋哈希中丢失
  (v0.7.5 诊断: 纯静态特征线性可分 71.2%, 词袋仅 40%)
- target_pattern: 16 维监督输出模式, 维度 0-4 对应五类编译错误族,
  其余维度为低强度基底

评估基线 (5 分类):
- 随机基线: 20%
- 多数类基线: 训练集最大类占比 (本语料为均衡 20%)

用法:
    from src.data.real_dataset import (
        RustCodingTrainingDataset, TrainingSample
    )
"""

import logging
from dataclasses import dataclass

import numpy as np

from src.codec.class_token import encode as _token_encode
from src.codec.spike_codec import SpikeEncoder
from src.data.rust_coding import (
    LABELS,
    load_rust_coding,
    static_metrics,
    stratified_kfold,
    stratified_split,
    structure_metrics,
)

log = logging.getLogger("omnispace.data.real_dataset")


@dataclass
class TrainingSample:
    """单个真实训练样本"""
    sample_id: str
    category: int           # 0=move, 1=borrow, 2=lifetime, 3=type, 4=ok
    category_name: str
    input_signal: np.ndarray     # 16维文本脉冲 (代码原文 TF-IDF 编码)
    target_pattern: np.ndarray   # 16维期望输出模式
    metadata: dict
    static_signal: np.ndarray = None  # 10维语法扫描特征 (numeric 通路)
    token_seq: list[int] | None = None  # 64比特 utf8-mb4 class-token 序列 (v0.11.0 跟版)

    def multimodal_input(self) -> dict:
        """双模态输入字典: numeric=语法特征, text=代码原文 (P0 方案)

        供 CubeGPT.step() / CubeFeatureExtractor.features() 直接消费,
        static_metrics 的判别信息 (诊断线性可分 71.2%) 不再丢失。
        """
        return {"numeric": self.static_signal, "text": self.metadata["code"]}


class RustCodingTrainingDataset:
    """真实 Rust 编码基准训练数据集 (纯真实, 无合成样本)

    类别 (真实 rustc 编译错误族):
      0: move      所有权移动   E0382/E0505/E0507
      1: borrow    借用冲突     E0502/E0499
      2: lifetime  生命周期     E0597/E0106/E0515/E0716
      3: type      类型不匹配   E0308/E0277/E0599/E0300
      4: ok        合法代码 (可编译)

    监督信号 (目标输出模式):
    - move     → 输出维度0高激活 (~0.7)
    - borrow   → 输出维度1高激活 (~0.7)
    - lifetime → 输出维度2高激活 (~0.7)
    - type     → 输出维度3高激活 (~0.7)
    - ok       → 输出维度4中等激活 (~0.5)
    """

    CATEGORIES = list(LABELS)
    N_CLASSES = len(LABELS)          # 5
    RANDOM_BASELINE = 1.0 / len(LABELS)  # 20%

    # 各类别目标激活强度 (错误类更醒目, 合法代码温和)
    _TARGET_STRENGTH = {0: 0.7, 1: 0.7, 2: 0.7, 3: 0.7, 4: 0.5}

    def __init__(self, dim: int = 16):
        self.dim = dim
        self.encoder = SpikeEncoder(dim=dim)
        self._corpus = load_rust_coding()  # 真实语料, 500 段 (v0.8.4 P2 扩充)
        self._static_max = None  # 惰性计算: 语料内静态特征逐维最大值

    def _make_target_pattern(self, category: int) -> np.ndarray:
        """生成监督目标输出模式 (维度 0-4 为类别位, 其余低强度基底)"""
        pattern = np.ones(self.dim) * 0.05
        pattern[category] = self._TARGET_STRENGTH.get(category, 0.6)
        if category < 4:
            # 错误类伴随相邻维度的微弱联动
            pattern[(category + 1) % self.N_CLASSES] = 0.15
        return np.clip(pattern, 0.0, 1.0)

    def _to_sample(self, s: dict, idx: int) -> TrainingSample:
        """真实语料条目 → 脉冲训练样本 (双模态 + 结构感知特征)"""
        code = s["code"]
        # 语法扫描(10维) + 结构感知(6维) = 16 维, 恰好填满 numeric 通路
        raw_static = np.concatenate([
            static_metrics(code), structure_metrics(code)])
        return TrainingSample(
            sample_id=f"{s['label_name']}_{idx:03d}",
            category=s["label"],
            category_name=s["label_name"],
            # 真实代码文本 → 代码感知 TF-IDF 脉冲编码
            input_signal=self.encoder.encode_text(code),
            target_pattern=self._make_target_pattern(s["label"]),
            metadata={
                "code": code,
                "rustc": s["rustc"],
                "msg": s["msg"],
                "code_len": len(code),
            },
            static_signal=self._normalize_static(raw_static),
            token_seq=_token_encode(code),
        )

    def _normalize_static(self, raw: np.ndarray) -> np.ndarray:
        """静态特征归一化到 [0, 1] (语料内逐维最大值, 跨进程确定)"""
        if self._static_max is None:
            stacked = np.stack([
                np.concatenate([static_metrics(s["code"]),
                                structure_metrics(s["code"])])
                for s in self._corpus])
            self._static_max = stacked.max(axis=0)
        return np.clip(raw / np.maximum(self._static_max, 1e-8), 0.0, 1.0)

    def generate_dataset(self, train_ratio: float = 0.75,
                         seed: int = 0) -> tuple[list[TrainingSample],
                                                 list[TrainingSample]]:
        """加载真实语料并分层划分训练/验证集

        Returns:
            (train_samples, val_samples) — 全部来自真实数据, 无合成
        """
        train_raw, val_raw = stratified_split(
            self._corpus, train_ratio=train_ratio, seed=seed
        )
        # stratified_split 打乱了顺序, 需找回原始索引保证 sample_id 稳定
        index_of = {id(s): i for i, s in enumerate(self._corpus)}
        train = [self._to_sample(s, index_of[id(s)]) for s in train_raw]
        val = [self._to_sample(s, index_of[id(s)]) for s in val_raw]
        return train, val

    def kfold_datasets(self, n_folds: int = 5,
                       seed: int = 0) -> list[tuple[list[TrainingSample],
                                                    list[TrainingSample]]]:
        """分层 K 折交叉验证划分 (每类别轮流分配到各折)

        每个样本恰好作为一次验证样本, 训练集为其余折的并集;
        替代单次 75/25 划分, 评估结论不再依赖划分运气。

        Returns:
            [(train_samples, val_samples), ...] — 长度 n_folds, 全部真实数据
        """
        index_of = {id(s): i for i, s in enumerate(self._corpus)}
        folds_raw = stratified_kfold(self._corpus, n_folds=n_folds, seed=seed)
        splits = []
        for k in range(n_folds):
            val_raw = folds_raw[k]
            train_raw = [s for j in range(n_folds) if j != k
                         for s in folds_raw[j]]
            train = [self._to_sample(s, index_of[id(s)]) for s in train_raw]
            val = [self._to_sample(s, index_of[id(s)]) for s in val_raw]
            splits.append((train, val))
        return splits

    def get_class_distribution(self, samples: list[TrainingSample]) -> dict:
        """统计类别分布"""
        counts = {name: 0 for name in self.CATEGORIES}
        for s in samples:
            counts[s.category_name] += 1
        return counts

    def majority_baseline(self, samples: list[TrainingSample]) -> float:
        """多数类基线: 训练集最大类别占比"""
        if not samples:
            return 0.0
        dist = self.get_class_distribution(samples)
        return max(dist.values()) / len(samples)


# ═══════════════════════════════════════════════════════════════
# 自测试
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("=" * 60)
    log.info("真实训练数据集测试 (100% 真实 Rust 语料)")
    log.info("=" * 60)

    dataset = RustCodingTrainingDataset(dim=16)
    train, val = dataset.generate_dataset(train_ratio=0.75)

    log.info("\n[真实数据规模]")
    log.info(f"  训练集: {len(train)} 样本")
    log.info(f"  验证集: {len(val)} 样本")
    log.info(f"  训练集分布: {dataset.get_class_distribution(train)}")
    log.info(f"  验证集分布: {dataset.get_class_distribution(val)}")
    log.info(f"  随机基线: {dataset.RANDOM_BASELINE:.0%}")
    log.info(f"  多数类基线: {dataset.majority_baseline(train):.0%}")

    log.info("\n[各类真实样本示例]")
    for cat in range(5):
        sample = next(s for s in train if s.category == cat)
        log.info(f"\n  类别 {cat} ({sample.category_name}):")
        log.info(f"    ID: {sample.sample_id}")
        log.info(f"    rustc: {sample.metadata['rustc']}")
        log.info(f"    报错: {sample.metadata['msg'][:60]}")
        log.info(f"    代码: {sample.metadata['code'][:60]!r}")
        log.info(f"    输入信号非零维: {np.count_nonzero(sample.input_signal)}")
        log.info(f"    语法特征 (numeric 通路): "
              f"[{', '.join(f'{v:.2f}' for v in sample.static_signal[:5])}...]")
        log.info(f"    目标模式: [{', '.join(f'{v:.2f}' for v in sample.target_pattern[:5])}...]")

    log.info("\n" + "=" * 60)
    log.info("真实数据集测试通过!")
    log.info("=" * 60)
