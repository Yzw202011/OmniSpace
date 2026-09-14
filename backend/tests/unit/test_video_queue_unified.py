"""VideoTaskQueue unified 引擎镜像验证（B5 步 3，2026-09-14）。

金标准纪律同 test_image_queue_unified：不改一稿 legacy 测试，把核心
场景在 ``task_queue.impl=unified`` 下重跑。video 特有契约：
纯 FIFO（无优先级）、排空收尾**不卸载**绘画管线（卸载在准入时做）、
snapshot 形状（queued=数量 + kinds 列表）、next_kind。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

import backend.services.video_queue as _vq
from backend.services.video_queue import VideoTaskQueue


@pytest.fixture()
def unified_queue(monkeypatch: pytest.MonkeyPatch) -> VideoTaskQueue:
    monkeypatch.setattr(_vq, "_use_unified", lambda: True)
    return VideoTaskQueue()


def _wait_until(pred: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时: {what}")


def _task(task_id: str, runner: Callable, **extra) -> dict:
    return {"task_id": task_id, "kind": "h3_chain", "runner": runner,
            "loop": None, "update_status": lambda *a, **k: None, **extra}


class Harness:
    def __init__(self, q: VideoTaskQueue, unload_spy: list | None = None) -> None:
        self.q = q
        self.events: list[str] = []
        self.lock = threading.Lock()
        self._monkey = pytest.MonkeyPatch()
        m = self._monkey
        self._unload_spy = unload_spy if unload_spy is not None else []
        m.setattr(q, "_thermal_paused", lambda: False)
        m.setattr(q, "_acquire_video_gen", self._acquire)
        m.setattr(q, "_release_video_gen",
                  lambda loop: self._mark("release"))
        m.setattr(q, "_sleep_vllm_for_generation",
                  lambda: self._mark("vllm_sleep"))
        def _spy_unload() -> None:
            self._unload_spy.append(1)
            self._mark("unload")
        m.setattr(q, "_unload_paint_pipeline", _spy_unload)
        m.setattr(q, "_schedule_wake_if_idle",
                  lambda: self._mark("wake"))

    def undo(self) -> None:
        self._monkey.undo()

    def _mark(self, e: str) -> None:
        with self.lock:
            self.events.append(e)

    def _acquire(self, task_id: str, loop) -> bool:  # type: ignore[no-untyped-def]
        self._mark(f"start:{task_id}")
        return True

    def task(self, task_id: str, runner: Callable, **extra) -> dict:
        return _task(task_id, runner, **extra)


def _ok(result: str) -> Callable:
    def runner(task, check_cancel):  # type: ignore[no-untyped-def]
        return result
    return runner


def test_unified_fifo_order_and_snapshot_shape(
        unified_queue: VideoTaskQueue) -> None:
    h = Harness(unified_queue)
    done: list[str] = []
    gate = threading.Event()

    def first(task, check_cancel):  # type: ignore[no-untyped-def]
        gate.wait(3.0)
        done.append("first")
        return "first"

    def second(task, check_cancel):  # type: ignore[no-untyped-def]
        done.append("second")
        return "second"

    h.q.submit(h.task("first", first))
    h.q.submit(h.task("second", second))
    # FIFO：无优先级重排——second 不可能插到 first 前
    snap = h.q.snapshot()
    assert snap["queued"] == 1 and snap["kinds"] == ["h3_chain"], snap
    assert snap["current"] == "first"
    gate.set()
    _wait_until(lambda: done == ["first", "second"], timeout=5.0,
                what="FIFO 顺序")
    h.undo()


def test_unified_cancel_states(unified_queue: VideoTaskQueue) -> None:
    h = Harness(unified_queue)
    gate = threading.Event()

    def holder(task, check_cancel):  # type: ignore[no-untyped-def]
        gate.wait(3.0)
        return "held"

    h.q.submit(h.task("holder", holder))
    h.q.submit(h.task("q1", holder))
    assert h.q.cancel("q1") == "queued"
    assert h.q.cancel("nope") == "missing"
    gate.set()
    _wait_until(lambda: h.q.snapshot()["current"] is None,
                timeout=5.0, what="holder 完成")
    h.undo()


def test_unified_drain_releases_without_unload(
        unified_queue: VideoTaskQueue) -> None:
    """video 特有契约：排空收尾放锁+唤醒，但**不卸载**绘画管线。"""
    h = Harness(unified_queue)
    spy: list[int] = []
    h._unload_spy = spy
    h.q.submit(h.task("v1", _ok("v1")))
    _wait_until(lambda: "release" in h.events, timeout=5.0,
                what="排空放锁")
    assert "wake" in h.events
    assert spy == [], "video 排空不得触发绘画管线卸载（行为漂移）"
    h.undo()


def test_unified_next_kind(unified_queue: VideoTaskQueue) -> None:
    h = Harness(unified_queue)
    gate = threading.Event()

    def holder(task, check_cancel):  # type: ignore[no-untyped-def]
        gate.wait(3.0)
        return "held"

    h.q.submit(h.task("cur", holder))
    h.q.submit(h.task("next", _ok("n"), kind="h3_chain"))
    assert h.q.next_kind() == "h3_chain"
    gate.set()
    _wait_until(lambda: h.q.snapshot()["current"] is None,
                timeout=5.0, what="排空")
    assert h.q.next_kind() is None
    h.undo()


def test_unified_orphan_reconcile_runs_once(
        unified_queue: VideoTaskQueue) -> None:
    """submit 前置的孤儿回收在 unified 态仍恰好执行一次。"""
    calls: list[str] = []

    def fake_reconcile(self, task: dict) -> None:
        calls.append(str(task.get("task_id")))

    orig = VideoTaskQueue._reconcile_orphan_tasks
    VideoTaskQueue._reconcile_orphan_tasks = fake_reconcile  # type: ignore[method-assign]
    try:
        q = VideoTaskQueue()
        assert q._core is not None
        q.submit(_task("r1", _ok("r")))
        q.submit(_task("r2", _ok("r")))
        assert calls == ["r1"], "孤儿回收只做一次（第二次 submit 不再触发）"
    finally:
        VideoTaskQueue._reconcile_orphan_tasks = orig  # type: ignore[method-assign]
