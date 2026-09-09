"""V9 稳定性修复单测（2026-09-09）。

V9-α：model_manager.allocate_memory 在拒绝前先对账 vLLM 孤儿——
账外孤儿占显存时先清再试（解 09-08 15:26 实测死锁）。
V9-β：图像队列排空后唤醒 vLLM 加去抖窗——任务间隙的唤醒不再被
紧邻任务撞死（09-08 15:37:04 实测 booting 被功能锁拒绝且无重试）。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from backend.services.image_queue import ImageTaskQueue


def _wait_until(pred, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时: {what}")


# ── V9-α：准入前孤儿对账 ──────────────────────────────────────────

def _bare_manager():
    from backend.services.model_manager import ModelManager

    mgr = ModelManager.__new__(ModelManager)
    mgr._engines = {}
    mgr._loaded = {}
    mgr._loaded_lock = threading.Lock()
    mgr._vram_lock = threading.RLock()
    mgr._reserved_vram_gb = 0.0
    mgr.last_error = ""
    return mgr


def test_alpha_reap_unlocks_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """孤儿占显存 → 驱逐无可驱逐 → 对账清孤儿 → 复测通过并记账。"""
    mgr = _bare_manager()
    gpu_state = {"available": True, "vram_free_gb": 1.0}
    monkeypatch.setattr(mgr, "get_gpu_status", lambda: gpu_state)
    monkeypatch.setattr(mgr, "evict_lowest_priority", lambda: False)

    class _Svc:
        def reap_orphans(self) -> int:
            gpu_state["vram_free_gb"] = 16.0  # 孤儿清掉，空闲回升
            return 1

    import backend.engines.vllm_service as vmod

    monkeypatch.setattr(vmod, "get_vllm_service", lambda: _Svc())
    assert mgr.allocate_memory(7.5) is True
    assert mgr._reserved_vram_gb == pytest.approx(7.5)


def test_alpha_refusal_still_honest_without_orphans(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """无孤儿时对账不改变判定：仍诚实拒绝并给出余量。"""
    mgr = _bare_manager()
    monkeypatch.setattr(mgr, "get_gpu_status",
                        lambda: {"available": True, "vram_free_gb": 1.0})
    monkeypatch.setattr(mgr, "evict_lowest_priority", lambda: False)

    class _Svc:
        def reap_orphans(self) -> int:
            return 0

    import backend.engines.vllm_service as vmod

    monkeypatch.setattr(vmod, "get_vllm_service", lambda: _Svc())
    assert mgr.allocate_memory(7.5) is False
    assert "显存不足" in mgr.last_error


def test_alpha_reap_exception_keeps_refusal(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """对账通道异常不影响原判定路径（尽力而为语义）。"""
    mgr = _bare_manager()
    monkeypatch.setattr(mgr, "get_gpu_status",
                        lambda: {"available": True, "vram_free_gb": 1.0})
    monkeypatch.setattr(mgr, "evict_lowest_priority", lambda: False)

    def _boom():
        raise RuntimeError("psutil 不可用")

    import backend.engines.vllm_service as vmod

    monkeypatch.setattr(vmod, "get_vllm_service", _boom)
    assert mgr.allocate_memory(7.5) is False


# ── V9-β：排空后去抖唤醒 ──────────────────────────────────────────

def _deb_queue(monkeypatch: pytest.MonkeyPatch, debounce: float,
               fired: list[str]) -> ImageTaskQueue:
    q = ImageTaskQueue()
    monkeypatch.setattr(q, "_wake_debounce_s", debounce)
    monkeypatch.setattr(q, "_wake_vllm_after_generation",
                        lambda: fired.append("wake"))
    return q


def test_beta_wake_fires_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """排空后去抖窗内仍空闲 → 真唤醒。"""
    fired: list[str] = []
    q = _deb_queue(monkeypatch, 0.15, fired)
    with q._cond:
        q._current = None
        q._queue.clear()
    q._schedule_wake_if_idle()
    _wait_until(lambda: fired == ["wake"], 2.0, "去抖后应唤醒")
    assert fired == ["wake"]


def test_beta_wake_skipped_when_local_task_running(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """去抖窗内出现本地任务（current 占用）→ 跳过本次唤醒。"""
    fired: list[str] = []
    q = _deb_queue(monkeypatch, 0.2, fired)
    q._schedule_wake_if_idle()
    with q._cond:  # 模拟紧邻的新任务：正在跑
        q._current = {"task_id": "t2"}  # type: ignore[assignment]
    time.sleep(0.6)
    assert fired == []


def test_beta_cloud_only_backlog_does_not_block_wake(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """去抖窗内仅剩云端任务（云道不占 GPU）→ 唤醒照常。"""
    fired: list[str] = []
    q = _deb_queue(monkeypatch, 0.15, fired)
    with q._cond:
        q._current = None
        q._queue.clear()
        q._queue.append({"task_id": "c1", "cloud": True})
    q._schedule_wake_if_idle()
    _wait_until(lambda: fired == ["wake"], 2.0, "云端积压不拦唤醒")
    assert fired == ["wake"]


def test_beta_new_drain_supersedes_older_timer(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """窗内再次排空 → 旧定时器让位、新定时器只唤醒一次。"""
    fired: list[str] = []
    q = _deb_queue(monkeypatch, 0.3, fired)
    q._schedule_wake_if_idle()          # T0 排一次
    time.sleep(0.15)
    q._schedule_wake_if_idle()          # T0.15 又排空（时间戳前移）
    time.sleep(0.8)                     # 两个定时器均已到期
    assert fired == ["wake"]            # 只唤醒一次（旧定时器让位）


def _unused(_: Any) -> None:  # pragma: no cover - 占位保持导入整洁
    return None
