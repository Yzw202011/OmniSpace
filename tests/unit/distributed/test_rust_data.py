import numpy as np
import pytest

from src.codec.spike_codec import SpikeEncoder
from src.data.rust_coding import (
    LABELS, RUST_SNIPPETS, load_rust_coding, static_metrics,
    structure_metrics, stratified_split
)
from src.training.readout import LinearReadout


def test_dataset_integrity():
    samples = load_rust_coding()
    assert len(samples) == 502  # v0.8.4 P2 扩充: 100 → 502
    counts = {}
    for s in samples:
        assert s["code"].strip()
        assert s["label_name"] in LABELS
        assert s["label"] == LABELS.index(s["label_name"])
        assert s["rustc"] and s["msg"]
        counts[s["label"]] = counts.get(s["label"], 0) + 1
    assert counts == {0: 101, 1: 100, 2: 100, 3: 100, 4: 101}


def test_real_rustc_error_codes_present():
    codes = {s["rustc"] for s in RUST_SNIPPETS}
    for expected in ("E0382", "E0502", "E0499", "E0597", "E0106", "E0308", "E0277"):
        assert expected in codes


def test_static_metrics():
    m = static_metrics('std::collections::HashMap::new();\nlet mut v = 1;')
    assert m.shape == (10,)
    assert m[1] == 0  # '&' 数
    assert m[2] == 1  # 'mut' 数
    assert m[9] == 3  # '::' 数


def test_structure_metrics():
    # 结构感知特征 (P1): [&mut, 返回引用, 类型标注, println, let, 防御调用]
    m = structure_metrics("fn f(s: &mut Vec<i32>) -> &i32 {\n"
                          "    let x = s.clone();\n"
                          "    println!(\"{}\", x);\n"
                          "}")
    assert m.shape == (6,)
    assert m[0] == 1  # '&mut' 数
    assert m[1] == 1  # '-> &' 返回引用数
    assert m[2] >= 1  # ': ' 类型标注数
    assert m[3] == 1  # 'println' 数
    assert m[4] == 1  # 'let ' 绑定数
    assert m[5] == 1  # 防御调用 (clone) 数
    # 常量: 返回引用是 lifetime 强信号
    assert structure_metrics("fn dangle() -> &str { &s }")[1] == 1
    # 'mut' 单独出现不构成 '&mut' (需引用符号前缀)
    assert structure_metrics("let mut v = vec![1]; v.push(2);")[0] == 0
    assert structure_metrics("let r = &mut v;")[0] == 1


def test_real_dataset_static_signal_16d():
    from src.data.real_dataset import RustCodingTrainingDataset
    ds = RustCodingTrainingDataset(dim=16)
    train, val = ds.generate_dataset(train_ratio=0.75, seed=0)
    for s in train[:5]:
        assert s.static_signal.shape == (16,)  # 10 static + 6 structure
        assert np.all(s.static_signal >= 0) and np.all(s.static_signal <= 1)
        mm = s.multimodal_input()
        assert mm["numeric"].shape == (16,)
        assert isinstance(mm["text"], str)


def test_stratified_split():
    samples = load_rust_coding()
    train, val = stratified_split(samples, train_ratio=0.75, seed=0)
    assert len(train) == 377 and len(val) == 125  # v0.8.4: 502 × 75/25
    assert set(s["label"] for s in val) == set(range(5))
    train_codes = {s["code"] for s in train}
    val_codes = {s["code"] for s in val}
    assert not train_codes & val_codes


def test_text_encoder_code_aware_and_deterministic():
    e1, e2 = SpikeEncoder(dim=16), SpikeEncoder(dim=16)
    code = "let mut v = vec![1]; let r = &v;"
    a, b, c = e1.encode_text(code), e2.encode_text(code), e1.encode_text(code)
    assert np.allclose(a, b) and np.allclose(a, c)
    # 代码感知: '&' 应参与编码 (与去掉 & 的编码不同)
    assert not np.allclose(a, e1.encode_text("let mut v = vec![1]; let r = v;"))


def test_feature_pipeline_and_readout():
    from src.training.readout import CubeFeatureExtractor
    samples = load_rust_coding()[:10]
    ex = CubeFeatureExtractor(depth=1, seed=0)
    F = np.stack([ex.features({
        "numeric": static_metrics(s["code"]), "text": s["code"]})
        for s in samples])
    assert F.shape[0] == 10
    assert np.all(np.isfinite(F))
    # 同一样本特征确定
    F2 = np.stack([ex.features({
        "numeric": static_metrics(s["code"]), "text": s["code"]})
        for s in samples[:2]])
    assert np.allclose(F[0], F2[0]) and np.allclose(F[1], F2[1])
    # 线性读出层在小子集上应不低于随机
    y = np.array([s["label"] for s in samples])
    r = LinearReadout(n_features=F.shape[1], n_classes=5, seed=0).fit(F, y)
    assert r.accuracy(F, y) >= 0.2
