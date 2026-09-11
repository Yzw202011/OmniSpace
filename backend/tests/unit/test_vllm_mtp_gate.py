"""V6 MTP 投机解码开关单测（2026-09-09，D6-bis=A 产品化）。

覆盖 mtp_spec_enabled 三轴：默认关 / config 真+权重在盘才开 / 缺权重
或配置异常一律降级关（诚实告警不硬上）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

from pathlib import Path

import pytest

from backend.engines.vllm_service import mtp_spec_enabled


def _patch_mtp_cfg(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    import backend.config as cfg_mod

    monkeypatch.setattr(
        cfg_mod, "get_config",
        lambda: {"vllm": {"mtp_speculative": value}})


def _model_dir_with_mtp(tmp_path: Path) -> Path:
    d = tmp_path / "qwen35-9b-w4a16"
    d.mkdir()
    (d / "model_mtp.safetensors").write_bytes(b"mtp")
    return d


def test_default_off(monkeypatch: pytest.MonkeyPatch,
                     tmp_path: Path) -> None:
    _patch_mtp_cfg(monkeypatch, False)
    assert mtp_spec_enabled(_model_dir_with_mtp(tmp_path)) is False


def test_on_with_weights(monkeypatch: pytest.MonkeyPatch,
                         tmp_path: Path) -> None:
    _patch_mtp_cfg(monkeypatch, True)
    assert mtp_spec_enabled(_model_dir_with_mtp(tmp_path)) is True


def test_on_without_weights_degrades(monkeypatch: pytest.MonkeyPatch,
                                     tmp_path: Path) -> None:
    """config 开但模型无 MTP 权重（如 8B）→ 降级关（不硬上崩启动）。"""
    _patch_mtp_cfg(monkeypatch, True)
    d = tmp_path / "qwen3-vl-8b-awq"
    d.mkdir()
    assert mtp_spec_enabled(d) is False


def test_string_truthy_values(monkeypatch: pytest.MonkeyPatch,
                              tmp_path: Path) -> None:
    d = _model_dir_with_mtp(tmp_path)
    _patch_mtp_cfg(monkeypatch, "true")
    assert mtp_spec_enabled(d) is True
    _patch_mtp_cfg(monkeypatch, "false")
    assert mtp_spec_enabled(d) is False
    _patch_mtp_cfg(monkeypatch, "0")
    assert mtp_spec_enabled(d) is False


def test_config_exception_off(monkeypatch: pytest.MonkeyPatch,
                              tmp_path: Path) -> None:
    import backend.config as cfg_mod

    def _boom() -> dict[str, object]:
        raise RuntimeError("yaml 坏了")

    monkeypatch.setattr(cfg_mod, "get_config", _boom)
    assert mtp_spec_enabled(_model_dir_with_mtp(tmp_path)) is False
