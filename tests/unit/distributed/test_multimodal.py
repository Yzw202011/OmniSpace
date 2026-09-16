import numpy as np
import pytest

from src.core.distributedformer import (
    DistributedFormer, InputModule, OutputModule, SUPPORTED_MODALITIES
)


def test_input_module_rejects_unknown_modality():
    with pytest.raises(ValueError):
        InputModule("audio")


def test_each_modality_encodes_independently():
    for mod, raw in [
        ("numeric", 1.5),
        ("text", "market surges on earnings"),
        ("timeseries", [1.0, 2.0, 4.0, 3.0]),
    ]:
        m = InputModule(mod)
        spikes = m.step(raw)
        assert isinstance(spikes, list)
        pattern = m.get_pattern()
        assert np.linalg.norm(pattern) > 0


def test_image_encoding_deterministic():
    img = np.random.RandomState(42).rand(8, 8)
    m = InputModule("image")
    s1, s2 = m.encode(img), m.encode(img)
    assert np.allclose(s1, s2)
    assert np.linalg.norm(s1) > 0


def test_vector_passthrough_numeric_module():
    m = InputModule("numeric")
    v = np.arange(16, dtype=float)
    assert np.allclose(m.encode(v), v)


def test_network_multimodal_step():
    df = DistributedFormer(depth=1, dim=16, num_think_layers=1)
    spikes = df.step({
        "numeric": 0.7,
        "text": "apple reports record revenue",
        "timeseries": [1, 2, 3, 5],
        "image": np.random.RandomState(1).rand(8, 8),
    })
    assert isinstance(spikes, list)
    stats = df.get_network_stats()
    assert set(stats["input_modules"]) == set(SUPPORTED_MODALITIES)


def test_missing_modalities_stay_silent():
    df = DistributedFormer(depth=1, dim=16, num_think_layers=1)
    before = {n: m.units[0].state.copy() for n, m in df.input_modules.items()}
    df.step({"text": "only text this time"})
    # numeric 模块未被喂入, 其状态不应因编码变化 (仅节律/自发影响)
    for name in df.input_modules:
        if name != "text":
            assert df.input_modules[name].last_spikes == []


def test_empty_input_spontaneous_activity():
    df = DistributedFormer(depth=1, dim=16, num_think_layers=1)
    for _ in range(5):
        df.step({})
    assert df.get_output_pattern().shape == (16,)


def test_invalid_inputs_raise():
    df = DistributedFormer(depth=1, dim=16, num_think_layers=1)
    with pytest.raises(TypeError):
        df.step(np.zeros(16))
    with pytest.raises(KeyError):
        df.step({"audio": 1.0})


def test_output_module_isolated():
    om = OutputModule(dim=16)
    spikes = om.step(np.random.randn(16) * 0.5, 1.0)
    assert isinstance(spikes, list)
    assert om.get_pattern().shape == (16,)
