"""H3 T8 双时钟门控单测（2026-09-16 批3）。

背景：h3_chain_engine._dual_clock_enabled 此前残留 `from backend.config
import get_config`（src 扁平化后 backend/ 无 config 模块）→ 必 ImportError
→ 门控恒 False：config.yaml 的 manga.h3_dual_clock: true 永远静默失效。
修复=改用模块级 get_config（顶部 :34 早已导入）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.inference import h3_chain_engine as h3  # noqa: E402


def _patch_cfg(monkeypatch, value: object) -> None:
    monkeypatch.setattr(
        h3, "get_config",
        lambda: {"manga": {"h3_dual_clock": value}})


def test_gate_true_when_enabled(monkeypatch) -> None:
    _patch_cfg(monkeypatch, True)
    assert h3._dual_clock_enabled() is True


def test_gate_false_default(monkeypatch) -> None:
    _patch_cfg(monkeypatch, False)
    assert h3._dual_clock_enabled() is False
    _patch_cfg(monkeypatch, "false")
    assert h3._dual_clock_enabled() is False
    _patch_cfg(monkeypatch, "0")
    assert h3._dual_clock_enabled() is False
    _patch_cfg(monkeypatch, "")
    assert h3._dual_clock_enabled() is False


def test_gate_fail_closed_on_bad_config(monkeypatch) -> None:
    """配置读取炸了必须保持关（fail-closed），不抛出。"""
    def boom():
        raise RuntimeError("config 不可读")
    monkeypatch.setattr(h3, "get_config", boom)
    assert h3._dual_clock_enabled() is False
