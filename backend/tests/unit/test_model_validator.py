"""ModelValidator 行为级直测（B9 高危模块补测 2026-09-13）。

背景（09-12 审计）：model_manager 的 validator/selector/cache/predictor
零直接测试。本文件先补 validator（SHA256 完整性校验）——它是 B6
「模型登记账实对账闸」的执行件，先立测试再动 B6。

覆盖：compute_sha256 分块正确性 / verify_model 三态（首算回填·匹配·
篡改拒绝）与两类坏输入 / verify_path 期望比对。
"""
from __future__ import annotations

import hashlib

import pytest

from backend.data.models import ModelCategory, ModelInfo, ModelStatus
from backend.services.model_manager.validator import ModelValidator


def _make_model(tmp_path, content: bytes | None, sha256: str = "",
                file_path: str | None = None) -> ModelInfo:
    p = tmp_path / "model.bin"
    if content is not None:
        p.write_bytes(content)
    return ModelInfo(id="m1", name="测试模型", category=ModelCategory.AUXILIARY,
                     status=ModelStatus.NOT_INSTALLED,
                     file_path=file_path if file_path is not None else str(p),
                     sha256=sha256)


def test_compute_sha256_matches_reference(tmp_path) -> None:
    content = b"omnispace-model-weights" * 1000
    _make_model(tmp_path, content)
    got = ModelValidator().compute_sha256(str(tmp_path / "model.bin"))
    assert got == hashlib.sha256(content).hexdigest()
    assert len(got) == 64


def test_compute_sha256_missing_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        ModelValidator().compute_sha256(str(tmp_path / "nope.bin"))


def test_verify_model_first_run_backfills_hash(tmp_path) -> None:
    m = _make_model(tmp_path, b"weights-v1")
    assert m.sha256 == ""
    assert ModelValidator().verify_model(m) is True
    assert m.sha256 == hashlib.sha256(b"weights-v1").hexdigest()


def test_verify_model_match_and_tamper(tmp_path) -> None:
    good = hashlib.sha256(b"weights-v1").hexdigest()
    m = _make_model(tmp_path, b"weights-v1", sha256=good)
    v = ModelValidator()
    assert v.verify_model(m) is True
    # 篡改：同路径换内容 → 哈希不匹配 → 拒绝
    (tmp_path / "model.bin").write_bytes(b"weights-TAMPERED")
    assert v.verify_model(m) is False


def test_verify_model_bad_inputs(tmp_path) -> None:
    v = ModelValidator()
    assert v.verify_model(_make_model(tmp_path, None,
                                      file_path="")) is False
    missing = _make_model(tmp_path, b"x", sha256=hashlib.sha256(b"x").hexdigest())
    (tmp_path / "model.bin").unlink()
    assert v.verify_model(missing) is False


def test_verify_path_with_and_without_expectation(tmp_path) -> None:
    content = b"path-verify"
    f = tmp_path / "w.bin"
    f.write_bytes(content)
    real = hashlib.sha256(content).hexdigest()
    v = ModelValidator()
    ok, got = v.verify_path(str(f))
    assert ok is True and got == real
    ok2, _ = v.verify_path(str(f), expected_sha256=real)
    assert ok2 is True
    ok3, _ = v.verify_path(str(f), expected_sha256="0" * 64)
    assert ok3 is False
