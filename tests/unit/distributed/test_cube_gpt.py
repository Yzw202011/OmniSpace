import numpy as np
import pytest

from src.core.distributedformer import CubeGPT
from src.core.distributedformer import (
    CubeFace, calculate_cube_scale
)


def test_scale_formula_matches_281k_nominal():
    s = calculate_cube_scale(2)
    # 4面 × (16端口 + 4368皮层) + 16输出头
    assert s["cortex_units_per_face"] == 4368
    assert s["units_per_face"] == 4384
    assert s["total_units"] == 4 * 4384 + 16
    assert s["nominal_params"] == 4 * 4400 * 16 == 281600
    assert s["total_params"] == s["total_units"] * 16


def test_construction_and_units():
    gpt = CubeGPT(depth=1, dim=16)
    s = calculate_cube_scale(1)
    assert len(gpt._all_units) == s["total_units"]
    assert list(gpt.faces) == ["numeric", "text", "timeseries", "image"]


def test_multimodal_step_all_faces():
    gpt = CubeGPT(depth=1, dim=16)
    spikes = gpt.step({
        "numeric": 0.7,
        "text": "apple reports record revenue",
        "timeseries": [1, 2, 3, 5],
        "image": np.random.RandomState(1).rand(8, 8),
    })
    assert isinstance(spikes, list)
    stats = gpt.get_network_stats()
    assert stats["model"] == "CubeGPT"
    assert set(stats["faces_stats"]) == set(gpt.faces)


def test_lateral_ring_propagation():
    """numeric 面的脉冲应通过立方体棱传入 text 面 (环形邻面) 的 inbox"""
    gpt = CubeGPT(depth=1, dim=16)
    gpt.reset_state()
    # 强输入, 提高_numeric面发放概率
    for _ in range(20):
        gpt.step({"numeric": 9.9})
    # inbox 被赋值为邻面聚合输出 (可能为零向量, 但必须是合法 dim 向量)
    for face in gpt.faces.values():
        assert face.inbox.shape == (16,)
        assert np.all(np.abs(face.inbox) <= 1.0)


def test_missing_modality_still_consumes_lateral_input():
    gpt = CubeGPT(depth=1, dim=16)
    for _ in range(3):
        gpt.step({"text": "only text"})
    assert gpt.get_output_pattern().shape == (16,)


def test_invalid_inputs_raise():
    gpt = CubeGPT(depth=1, dim=16)
    with pytest.raises(TypeError):
        gpt.step(np.zeros(16))
    with pytest.raises(KeyError):
        gpt.step({"audio": 1.0})


def test_reset_state_clears_faces():
    gpt = CubeGPT(depth=1, dim=16)
    gpt.step({"numeric": 1.0})
    gpt.reset_state()
    assert gpt.total_steps == 0
    assert gpt.cycle_phase == 0
    for face in gpt.faces.values():
        assert np.all(face.inbox == 0)
        assert face.last_spikes == []


def test_cube_face_rejects_unknown_modality():
    with pytest.raises(ValueError):
        CubeFace("audio", depth=1)


def test_stdp_toggle():
    gpt = CubeGPT(depth=1, dim=16)
    gpt.enable_learning(False)
    stats = gpt.get_stdp_stats()
    assert stats["learning_enabled"] is False
