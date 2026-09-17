"""DFTrainer 测试（DF v0.10.2/v0.11.0 跟版 2026-09-17）。

覆盖 v5.4 的 LR 调度（默认 cosine）与水库冻结两机制 + v0.11.0 的
可训练 token 嵌入层。移植自上游 test_trainer.py（其 md 数据集测试
依赖 md_text.py，本次跟版未含该文件——md 路径留待后续拍板）。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.training.trainer import DFTrainer


def _small_trainer(**kwargs):
    return DFTrainer(depth=0, dim=16, learning_rate=0.008, **kwargs)


def test_default_lr_schedule_is_cosine():
    t = _small_trainer()
    assert t.lr_schedule == "cosine"
    assert t.freeze_reservoir_epoch is None
    assert not t.reservoir_frozen


def test_cosine_schedule_decays_late():
    t = _small_trainer()
    t._apply_schedule(0, 12)
    mid = t.lr
    t._apply_schedule(11, 12)
    assert t.lr == pytest.approx(0.008 * 0.1)  # min_lr_ratio 兜底
    # 后期 LR 应始终低于早期, 且单调递减
    for ep in (2, 4, 6, 8, 10):
        t._apply_schedule(ep, 12)
        assert t.lr < mid
        mid = t.lr


def test_constant_schedule_no_change():
    t = _small_trainer(lr_schedule="constant")
    t._apply_schedule(11, 12)
    assert t.lr == pytest.approx(0.008)


def test_step_schedule_drops_at_epoch():
    t = _small_trainer(lr_schedule="step", step_drop_epoch=8)
    t._apply_schedule(7, 12)
    assert t.lr == pytest.approx(0.008)
    t._apply_schedule(8, 12)
    assert t.lr == pytest.approx(0.008 * 0.1)


def test_freeze_guards_reservoir_update():
    """冻结后 _update_weights_supervised 不应再改变任何 w_in。"""
    from src.data.real_dataset import RustCodingTrainingDataset

    t = _small_trainer(freeze_reservoir_epoch=0)
    ds = RustCodingTrainingDataset(dim=16)
    train, _val = ds.generate_dataset(train_ratio=0.75, seed=0)
    sample = train[0]
    feats, _nsp = t._forward_features(t._sample_inputs(sample))
    out = t.df.get_output_pattern()
    think = t.df.get_think_layer_pattern()
    t.reservoir_frozen = False
    t._update_weights_supervised(sample.static_signal, sample.target_pattern,
                                 out, think, lr=t.lr)
    # 转冻结后权重不应再变
    t.reservoir_frozen = True
    before = [u.w_in for u in t.df._all_units]
    t._update_weights_supervised(sample.static_signal, sample.target_pattern,
                                 out, think, lr=t.lr)
    for u, w_before in zip(t.df._all_units, before, strict=True):
        assert u.w_in == w_before, "冻结后 w_in 不应更新"
    assert feats is not None


def test_freeze_flag_applied_in_schedule():
    t = _small_trainer(freeze_reservoir_epoch=3)
    t._apply_schedule(2, 12)
    assert not t.reservoir_frozen
    t._apply_schedule(3, 12)
    assert t.reservoir_frozen


# ── v0.11.0: 端到端可学习 (可训练分类式 token 嵌入层) ──────────

def test_token_input_off_by_default():
    t = _small_trainer()
    assert not t.use_token_input
    assert t.token_emb is None


def test_token_forward_injects_embedding_block():
    from src.data.real_dataset import RustCodingTrainingDataset

    t = _small_trainer(use_token_input=True)
    ds = RustCodingTrainingDataset(dim=16)
    train, _val = ds.generate_dataset(train_ratio=0.75, seed=0)
    sample = train[0]
    assert sample.token_seq and sample.token_seq[0] is not None  # 跟版增量生效
    feats_no_tok = t._forward_features(t._sample_inputs(sample))[0]
    feats_tok, _ = t._forward_features(t._sample_inputs(sample),
                                       t._tokens_of(sample))
    # 嵌入块恒定前置, 特征维度 = 水库特征 + token_hid
    assert len(feats_no_tok) + t.token_hid == len(feats_tok)
    assert t.token_emb is not None


def test_token_empty_seq_stable_dim():
    t = _small_trainer(use_token_input=True)
    feats_empty, _ = t._forward_features({"numeric": np.zeros(16)}, [])
    t2 = _small_trainer(use_token_input=True)
    feats_filled, _ = t2._forward_features({"numeric": np.zeros(16)},
                                           [ord("a"), ord("中")])
    assert len(feats_empty) == len(feats_filled)
