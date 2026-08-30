"""ADR-003 P3 验收测试：统一生命周期状态机与可观测性。

锁定四类不变量：
  ① EngineState 值域与 coerce 宽容映射（历史字符串兼容）；
  ② BaseEngine 统一钩子（state/health_check/graceful_stop/vram_reclaim_gb）；
  ③ dialog 引擎状态并入 vLLM 子进程事实（booting/sleeping/失联降级）；
  ④ release_for_module 终止 vLLM 后台账与引擎状态同步（面板一致性）。
"""
from __future__ import annotations

import threading

import pytest

from backend.services.inference.base_engine import (
    BaseEngine,
    EngineState,
    derive_state,
)

pytestmark = pytest.mark.smoke


# ── ① 状态机 ────────────────────────────────────────────────────────

def test_engine_state_coerce_legacy_strings() -> None:
    """历史 state 字符串（unavailable/unloaded/ready/error）逐字兼容。"""
    for legacy in ("unavailable", "unloaded", "ready", "error"):
        assert EngineState.coerce(legacy).value == legacy
    assert EngineState.coerce("booting") is EngineState.BOOTING
    assert EngineState.coerce("sleeping") is EngineState.SLEEPING
    # 宽容降级：None / 未知串 → UNLOADED（绝不抛异常）
    assert EngineState.coerce(None) is EngineState.UNLOADED
    assert EngineState.coerce("no_such_state") is EngineState.UNLOADED


def test_derive_state_priority() -> None:
    assert derive_state(loaded=False, booting=True) == "booting"
    assert derive_state(loaded=True, sleeping=True) == "sleeping"
    assert derive_state(loaded=True, unavailable=True) == "unavailable"
    assert derive_state(loaded=True) == "ready"
    assert derive_state(loaded=False, error="boom") == "error"
    assert derive_state(loaded=False) == "unloaded"


# ── ② BaseEngine 统一钩子 ──────────────────────────────────────────

class _StatefulFake(BaseEngine):
    name = "fake"

    def __init__(self) -> None:
        super().__init__()
        self.status_override: dict = {"engine": "fake", "ready": True,
                                      "state": "ready"}
        self.unload_calls = 0

    def load_model(self, model_id: str | None = None) -> bool:
        return True

    def unload_model(self) -> bool:
        self.unload_calls += 1
        return True

    def get_status(self) -> dict:
        return dict(self.status_override)


def test_base_engine_state_and_hooks() -> None:
    eng = _StatefulFake()
    assert eng.state is EngineState.READY
    eng.status_override["state"] = "sleeping"
    assert eng.state is EngineState.SLEEPING
    eng.status_override.pop("state")
    assert eng.state is EngineState.UNLOADED  # 缺省宽容映射

    eng.status_override["state"] = "ready"
    hc = eng.health_check()
    assert hc["state"] == "ready" and hc["ready"] is True
    assert hc["healthy"] is True  # READY 属正常运营态
    eng.status_override["state"] = "sleeping"
    assert eng.health_check()["healthy"] is True
    eng.status_override["state"] = "booting"
    assert eng.health_check()["healthy"] is False

    assert eng.graceful_stop() is True
    assert eng.unload_calls == 1  # 默认委托 unload_model
    assert eng.vram_reclaim_gb() == 0.0  # 进程内默认无独立预算


# ── ③ dialog 状态并入 vLLM 子进程事实 ──────────────────────────────

class _FakeVllmSvc:
    def __init__(self, booting=False, running=False, healthy=False,
                 stopped=False):
        self._booting = booting
        self._running = running
        self._healthy = healthy
        self._stopped = stopped

    def is_booting(self) -> bool:
        return self._booting

    def is_running(self) -> bool:
        return self._running

    def is_healthy(self) -> bool:
        return self._healthy

    @property
    def stopped_for_paint(self) -> bool:
        return self._stopped


def _dialog_with_vllm(monkeypatch, svc: _FakeVllmSvc):
    from backend.engines import vllm_service as vmod
    from backend.services.inference.backends.vllm_backend import VLLMBackend
    from backend.services.inference.dialog_engine import DialogEngine

    monkeypatch.setattr(vmod, "get_vllm_service", lambda: svc)
    eng = DialogEngine()
    eng._backend = VLLMBackend()
    eng._state = "ready"
    return eng


def test_dialog_state_booting(monkeypatch) -> None:
    eng = _dialog_with_vllm(monkeypatch, _FakeVllmSvc(booting=True))
    assert eng._unified_state() == "booting"


def test_dialog_state_booting_visible_before_backend_attached(monkeypatch) -> None:
    """启动预热窗口（引擎尚未持有 backend）也如实报 booting。"""
    from backend.engines import vllm_service as vmod
    from backend.services.inference.backends.vllm_backend import VLLMBackend
    from backend.services.inference.dialog_engine import DialogEngine

    monkeypatch.setattr(
        vmod, "get_vllm_service", lambda: _FakeVllmSvc(booting=True))
    eng = DialogEngine()
    eng._backend = VLLMBackend()  # backend 对象在但未 ready（load 未返回）
    eng._state = "unloaded"
    assert eng._unified_state() == "booting"

    # 纯预热（引擎完全没 backend）：子进程事实同样透出
    eng2 = DialogEngine()
    eng2._backend = None
    eng2._state = "unloaded"
    assert eng2._unified_state() == "booting"


def test_dialog_state_running_unhealthy_is_booting(monkeypatch) -> None:
    """进程存活但 /health 未通（权重装载窗口）→ booting 而非 ready。"""
    eng = _dialog_with_vllm(
        monkeypatch, _FakeVllmSvc(running=True, healthy=False))
    assert eng._unified_state() == "booting"


def test_dialog_state_ready_when_healthy(monkeypatch) -> None:
    eng = _dialog_with_vllm(
        monkeypatch, _FakeVllmSvc(running=True, healthy=True))
    assert eng._unified_state() == "ready"


def test_dialog_state_sleeping_after_stopped_for_paint(monkeypatch) -> None:
    eng = _dialog_with_vllm(monkeypatch, _FakeVllmSvc(stopped=True))
    assert eng._unified_state() == "sleeping"


def test_dialog_state_gone_subprocess_degrades(monkeypatch) -> None:
    """引擎态 ready 但子进程消失（被终止）→ 如实降级 unloaded。"""
    eng = _dialog_with_vllm(monkeypatch, _FakeVllmSvc())
    assert eng._unified_state() == "unloaded"


def test_dialog_state_inproc_backend_unchanged(monkeypatch) -> None:
    """进程内后端不走 vLLM 分支，既有 _state 直通（零行为变化）。"""
    from backend.services.inference.dialog_engine import DialogEngine

    eng = DialogEngine()
    eng._backend = None
    eng._state = "error"
    assert eng._unified_state() == "error"


# ── ④ release_for_module 台账/状态同步（面板一致性）────────────────

def _bare_manager():
    from backend.services.model_manager import ModelManager

    mgr = ModelManager.__new__(ModelManager)
    mgr._engines = {}
    mgr._loaded = {}
    mgr._loaded_lock = threading.Lock()
    mgr._vram_lock = threading.Lock()
    mgr._reserved_vram_gb = 0.0
    mgr.last_error = ""
    return mgr


def test_release_for_module_syncs_vllm_bookkeeping(monkeypatch) -> None:
    """终止 vLLM 后：台账条目弹出、预留量对称扣减（面板不再 stale）。"""
    from backend.engines import vllm_service as vmod

    stopped: dict = {"count": 0}

    class _StopableSvc(_FakeVllmSvc):
        served = "qwen3-vl-8b-awq"

        @property
        def served_name(self) -> str:  # noqa: D102 - 测试桩
            return self.served if stopped["count"] == 0 else ""

        def stop(self, timeout_s: float = 5.0) -> bool:
            stopped["count"] += 1
            return True

    monkeypatch.setattr(vmod, "get_vllm_service", lambda: _StopableSvc(running=True))
    mgr = _bare_manager()
    mgr._loaded["qwen3-vl-8b-awq"] = {
        "category": "dialog", "path": "models/qwen3-vl-8b-awq",
        "vram_gb": 7.5, "priority": 5, "loaded_at": 0.0,
        "engine": "autoload", "backend": "cuda"}
    mgr._reserved_vram_gb = 7.5

    result = mgr.release_for_module("paint")

    assert stopped["count"] == 1
    assert "qwen3-vl-8b-awq(vllm)" in result["freed_models"]
    assert mgr.get_loaded_models() == []  # 台账已同步弹出
    assert mgr._reserved_vram_gb == 0.0  # 预留量对称扣减


def test_release_for_module_vllm_booting_no_bookkeeping_side_effect(
        monkeypatch) -> None:
    """booting 期终止：台账本无条目，同步调用为安全 no-op。"""
    from backend.engines import vllm_service as vmod

    class _BootingSvc(_FakeVllmSvc):
        def __init__(self) -> None:
            super().__init__(booting=True, running=True)
            self.stop_calls = 0

        @property
        def served_name(self) -> str:
            return "qwen3-vl-8b-awq"

        def stop(self, timeout_s: float = 5.0) -> bool:
            self.stop_calls += 1
            return True

    svc = _BootingSvc()
    monkeypatch.setattr(vmod, "get_vllm_service", lambda: svc)
    mgr = _bare_manager()  # 台账为空（booting 期尚未登记）

    result = mgr.release_for_module("paint")

    assert svc.stop_calls == 1
    assert "qwen3-vl-8b-awq(vllm)" in result["freed_models"]
    assert mgr.get_loaded_models() == []
