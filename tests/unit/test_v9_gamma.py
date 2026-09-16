"""V9-γ 单测（2026-09-09，卡 90% 事故根治件）。

场景链：绘画让渡 → wake 后台线程重启服务（引擎/台账不知情）→
①引擎 _unified_state 永远报 booting（根因③）②ensure_loaded 因台账
盲区拒绝装载已装好的模型（根因②）。修复=重挂收养（引擎侧限频自愈
+ mgr 收养旁路 + note_external_load 台账补记）。
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from src.services.inference.backends.vllm_backend import VLLMBackend
from src.services.inference.dialog_engine import DialogEngine


class _FakeSvc:
    """vllm_service 替身：唤醒产物形态（running+健康+served 有名）。

    booting+flip_after_polls 组合可模拟「热备启动窗口期 → 稍后转健康」
    （V9-γ+ 互踩收编：启动中等待场景）。
    """

    def __init__(self, running: bool = True, healthy: bool = True,
                 served: str = "qwen3-vl-8b-awq", booting: bool = False,
                 flip_after_polls: int = 0) -> None:
        self._running = running
        self._healthy = healthy
        self._served = served
        self._booting = booting
        self._flip_after = flip_after_polls
        self._polls = 0

    def is_running(self) -> bool:
        return self._running

    def is_healthy(self) -> bool:
        self._polls += 1
        if self._flip_after and self._polls >= self._flip_after:
            self._healthy = True
            self._booting = False
        return self._healthy

    def is_booting(self) -> bool:
        return self._booting

    @property
    def served_name(self) -> str:
        return self._served

    @property
    def stopped_for_paint(self) -> bool:
        return False


class _FakeMgr:
    def __init__(self, real: Any = None) -> None:
        self._real = real
        self.adopted: list[str] = []

    def resolve_model_path(self, model_id: str) -> str:
        return f"E:/models/{model_id}"

    def note_external_load(self, model_id: str, category: str) -> bool:
        self.adopted.append(model_id)
        return True


def _bare_engine() -> DialogEngine:
    eng = DialogEngine.__new__(DialogEngine)
    eng._backend = None
    eng._backend_name = ""
    eng._state = "unloaded"
    eng._model_id = ""
    eng._model_dir = None
    eng._last_error = ""
    DialogEngine._last_reattach_ts = 0.0
    return eng


def _patch_svc(monkeypatch: pytest.MonkeyPatch, svc: _FakeSvc) -> None:
    import src.engines.vllm_service as vmod

    monkeypatch.setattr(vmod, "get_vllm_service", lambda: svc)


def _patch_mgr(monkeypatch: pytest.MonkeyPatch, mgr: Any) -> None:
    import src.services.model_manager as mm

    monkeypatch.setattr(mm, "get_model_manager", lambda: mgr)


def test_unified_state_heals_healthy_orphan(monkeypatch) -> None:
    """唤醒产物（服务健康+引擎无 backend）→ 查询即自愈 ready 并重挂。"""
    eng = _bare_engine()
    _patch_svc(monkeypatch, _FakeSvc())
    _patch_mgr(monkeypatch, _FakeMgr())
    assert eng._unified_state() == "ready"
    assert eng._backend is not None
    assert isinstance(eng._backend, VLLMBackend)
    assert eng._model_id == "qwen3-vl-8b-awq"


def test_unified_state_booting_when_service_loading(monkeypatch) -> None:
    """服务 running 但健康未通（真装载窗口）→ 仍如实报 booting。"""
    eng = _bare_engine()
    _patch_svc(monkeypatch, _FakeSvc(healthy=False))
    assert eng._unified_state() == "booting"
    assert eng._backend is None


def test_try_adopt_target_mismatch_rejects(monkeypatch) -> None:
    """目标与在跑模型不符（want=9B served=8B）→ 不收养。"""
    eng = _bare_engine()
    _patch_svc(monkeypatch, _FakeSvc(served="qwen3-vl-8b-awq"))
    _patch_mgr(monkeypatch, _FakeMgr())
    assert eng.try_adopt("qwen35-9b-w4a16") is False
    assert eng._backend is None


def test_try_adopt_target_match(monkeypatch) -> None:
    eng = _bare_engine()
    _patch_svc(monkeypatch, _FakeSvc())
    fake_mgr = _FakeMgr()
    _patch_mgr(monkeypatch, fake_mgr)
    assert eng.try_adopt("qwen3-vl-8b-awq") is True
    assert fake_mgr.adopted == ["qwen3-vl-8b-awq"]


def test_try_adopt_waits_for_booting_same_target(monkeypatch) -> None:
    """互踩收编（V9-γ+）：热备正启动同目标（不健康+booting）→
    等待并在转健康后收养成功（模拟 07:46 场景）。"""
    eng = _bare_engine()
    svc = _FakeSvc(healthy=False, booting=True, flip_after_polls=3)
    _patch_svc(monkeypatch, svc)
    _patch_mgr(monkeypatch, _FakeMgr())
    assert eng.try_adopt("qwen3-vl-8b-awq", wait_s=10.0) is True
    assert eng._backend is not None


def test_try_adopt_no_wait_for_different_booting_target(
        monkeypatch) -> None:
    """外部通道在装别的模型（热备 9B / 请求 8B）→ 不等，立即 False。"""
    eng = _bare_engine()
    _patch_svc(monkeypatch, _FakeSvc(
        healthy=False, booting=True, served="qwen35-9b-w4a16"))
    _patch_mgr(monkeypatch, _FakeMgr())
    assert eng.try_adopt("qwen3-vl-8b-awq", wait_s=10.0) is False
    assert eng._backend is None


def test_try_adopt_dead_boot_returns_fast(monkeypatch) -> None:
    """无在飞启动且未健康 → 立即 False 走原路径（不空等）。"""
    eng = _bare_engine()
    _patch_svc(monkeypatch, _FakeSvc(healthy=False, booting=False))
    import time as _t
    t0 = _t.monotonic()
    assert eng.try_adopt("qwen3-vl-8b-awq", wait_s=10.0) is False
    assert _t.monotonic() - t0 < 2.0


def test_reattach_backend_attrs() -> None:
    b = VLLMBackend.reattach("m1", None)
    assert b.model_id == "m1"
    assert b.is_ready() is True


def test_ensure_loaded_adoption_bypass(monkeypatch) -> None:
    """收养命中时：不触碰分配器（allocate 被调即抛）直接入账 True。"""
    from src.services.model_manager import ModelManager

    mgr = ModelManager.__new__(ModelManager)
    mgr._engines = {}
    mgr._loaded = {}
    mgr._loaded_lock = threading.Lock()
    mgr._vram_lock = threading.RLock()
    mgr._reserved_vram_gb = 0.0
    mgr.last_error = "x"

    class _Engine:
        name = "fake"

        def try_adopt(self, model_id: str) -> bool:  # noqa: D102
            return model_id == "ghost-model"

    monkeypatch.setattr(mgr, "_get_engine", lambda _cat: _Engine())
    monkeypatch.setattr(mgr, "resolve_model_path",
                        lambda mid: f"E:/models/{mid}")
    monkeypatch.setattr(mgr, "estimate_vram_gb",
                        lambda mid, cat: 7.5)

    def _no_allocate(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("收养旁路命中时不得触碰分配器")

    monkeypatch.setattr(mgr, "allocate_memory", _no_allocate)
    assert mgr.ensure_loaded("dialog", "ghost-model") is True
    assert "ghost-model" in mgr._loaded
    assert mgr.last_error == ""


def test_note_external_load_idempotent(monkeypatch) -> None:
    from src.services.model_manager import ModelManager

    mgr = ModelManager.__new__(ModelManager)
    mgr._loaded = {}
    mgr._loaded_lock = threading.Lock()
    monkeypatch.setattr(mgr, "resolve_model_path",
                        lambda mid: f"E:/models/{mid}")
    monkeypatch.setattr(mgr, "estimate_vram_gb", lambda mid, cat: 7.5)
    assert mgr.note_external_load("m1", "dialog") is True
    mgr._loaded["m1"]["loaded_at"] = 111.0  # type: ignore[index]
    assert mgr.note_external_load("m1", "dialog") is True
    assert mgr._loaded["m1"]["loaded_at"] == 111.0  # 幂等不覆盖
# 本项目仅供学习使用，商业授权请+Q 3559331368
