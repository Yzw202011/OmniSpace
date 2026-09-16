import numpy as np
import pytest

from src.core.distributedformer import (
    DistributedFormer, KVStack, SpikingUnit, calculate_scale
)


def test_calculate_scale():
    info = calculate_scale(2)
    assert info["base_units"] == 4368
    assert info["total_params"] == 4368 * 16


def test_kvstack_query_and_persistence(tmp_path):
    kv = KVStack(capacity=100, dim=16)
    for i in range(20):
        kv.push(f"k{i}", np.random.randn(16), np.random.randn(16))
    results = kv.query(np.random.randn(16), top_k=3)
    assert len(results) == 3
    kv.save_to_disk(str(tmp_path / "kv.json"))
    kv2 = KVStack(capacity=100, dim=16)
    kv2.load_from_disk(str(tmp_path / "kv.json"))
    assert len(kv2.entries) == 20


def test_spiking_unit_fires_spikes():
    unit = SpikingUnit("u")
    # 显式固定动力学参数, 保证确定性发放
    # (随机初始化下 w_in 为负时强输入会被饱和为负, 偶发 200 步零发放)
    unit._receptive = np.ones(16) / 4.0  # 正感受野投影
    unit.w_in = 1.0
    unit.b_in = 0.0
    unit.w_out = 1.0
    unit.b_out = 0.5
    unit.threshold = 0.1
    unit.w_attn = unit.b_attn = unit.w_state = unit.w_global = 0.0
    unit.spontaneous_rate = 0.0
    strong = np.ones(16) * 5.0  # 强输入保证确定性发放
    spikes = sum(
        1 for _ in range(200)
        if unit.step(strong, np.zeros(16), 1.0)
    )
    assert 0 < spikes <= 200


def test_network_step():
    df = DistributedFormer(depth=1, dim=16)
    for _ in range(3):
        spikes = df.step({"numeric": np.random.randn(16) * 0.5})
        assert isinstance(spikes, list)
    stats = df.get_network_stats()
    assert stats["total_units"] > 0
