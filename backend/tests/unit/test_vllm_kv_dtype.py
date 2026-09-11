"""V3 FP8 KV cache 档位解析单测（2026-09-08）。

覆盖 resolve_kv_cache_dtype 的配置解析 × 硬件能力路由 × 保守降级
三轴：默认关（历史行为逐比特不变）/ auto 按 8.9 能力线自动 / fp8
强制但能力不足降级 / 探测异常一律按关。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

from typing import Any

import pytest
import torch

from backend.engines.vllm_service import resolve_kv_cache_dtype


def _patch_config(monkeypatch: pytest.MonkeyPatch,
                  vllm_cfg: dict[str, Any] | None) -> None:
    import backend.config as cfg_mod

    base = {"vllm": vllm_cfg} if vllm_cfg is not None else {}
    monkeypatch.setattr(cfg_mod, "get_config", lambda: base)


def _patch_gpu(monkeypatch: pytest.MonkeyPatch,
               cap: tuple[int, int] | None,
               available: bool = True) -> None:
    monkeypatch.setattr(
        torch.cuda, "is_available", lambda: available)
    if cap is None:
        def _boom(_idx: int) -> tuple[int, int]:
            raise RuntimeError("nvml 不可用")
        monkeypatch.setattr(
            torch.cuda, "get_device_capability", _boom)
    else:
        monkeypatch.setattr(
            torch.cuda, "get_device_capability",
            lambda _idx: cap)


def test_default_off_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, None)
    _patch_gpu(monkeypatch, (12, 0))
    assert resolve_kv_cache_dtype(0) is None


def test_explicit_empty_string_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, {"kv_cache_dtype": ""})
    _patch_gpu(monkeypatch, (12, 0))
    assert resolve_kv_cache_dtype(0) is None


def test_garbage_value_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, {"kv_cache_dtype": "int4-kv"})
    _patch_gpu(monkeypatch, (12, 0))
    assert resolve_kv_cache_dtype(0) is None


def test_auto_on_blackwell_sm120(monkeypatch: pytest.MonkeyPatch) -> None:
    """5070 Ti 开发档（sm_120）auto → fp8。"""
    _patch_config(monkeypatch, {"kv_cache_dtype": "auto"})
    _patch_gpu(monkeypatch, (12, 0))
    assert resolve_kv_cache_dtype(0) == "fp8"


def test_auto_off_on_baseline_sm86(monkeypatch: pytest.MonkeyPatch) -> None:
    """3060 基线档（sm_86，无 FP8）auto → 关——按硬件分档路由的设计内行为。"""
    _patch_config(monkeypatch, {"kv_cache_dtype": "auto"})
    _patch_gpu(monkeypatch, (8, 6))
    assert resolve_kv_cache_dtype(0) is None


def test_forced_fp8_degrades_on_sm86(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 fp8 遇无能力卡：降级为关（不硬上崩启动）。"""
    _patch_config(monkeypatch, {"kv_cache_dtype": "fp8"})
    _patch_gpu(monkeypatch, (8, 6))
    assert resolve_kv_cache_dtype(0) is None


def test_fp8_at_capability_boundary_89(monkeypatch: pytest.MonkeyPatch) -> None:
    """能力线边界（8.9，Ada）→ 开。"""
    _patch_config(monkeypatch, {"kv_cache_dtype": "fp8"})
    _patch_gpu(monkeypatch, (8, 9))
    assert resolve_kv_cache_dtype(0) == "fp8"


def test_cuda_unavailable_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, {"kv_cache_dtype": "auto"})
    _patch_gpu(monkeypatch, (12, 0), available=False)
    assert resolve_kv_cache_dtype(0) is None


def test_probe_exception_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测异常（nvml 不可用等）一律按关——保守优先。"""
    _patch_config(monkeypatch, {"kv_cache_dtype": "auto"})
    _patch_gpu(monkeypatch, None)
    assert resolve_kv_cache_dtype(0) is None
