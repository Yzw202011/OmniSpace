"""ImageTaskQueue unified 引擎镜像验证（B5 步 2，2026-09-14）。

金标准纪律：**不改一稿**现有 legacy 测试——本文件把 test_image_queue
/ test_queue_b0_guards 的核心场景在 ``task_queue.impl=unified`` 下
重跑（monkeypatch 模块开关后构造实例）。legacy/unified 双实现必须
语义逐比特一致；本文件任何一红 = unified 不可灰度。

与 legacy 金标准相同：宿主功能锁钩子必须经 Harness 替身（真
_acquire_paint_lock 需要 loop，纯调度测试不触真锁——同 legacy 约定）。
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

import pytest

import src.services.image_queue as _iq
from src.services.image_queue import ImageTaskQueue


@pytest.fixture()
def unified_queue(monkeypatch: pytest.MonkeyPatch) -> ImageTaskQueue:
    monkeypatch.setattr(_iq, "_use_unified", lambda: True)
    return ImageTaskQueue()


def _wait_until(pred: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时: {what}")


def _task(task_id: str, runner: Callable, **extra) -> dict:
    return {"task_id": task_id, "kind": "paint", "runner": runner,
            "loop": None, "priority": 5, **extra}


class Harness:
    """与 legacy 金标准同型替身：monkeypatch 宿主钩子（unified 的
    Core 经 host 回调，patch 自动生效）。"""

    def __init__(self, q: ImageTaskQueue, unload_sleep: float = 0.0) -> None:
        self.q = q
        self.events: list[str] = []
        self.lock = threading.Lock()
        self._monkey = pytest.MonkeyPatch()
        m = self._monkey
        m.setattr(q, "_thermal_paused", lambda: False)
        m.setattr(q, "_wake_debounce_s", 0.05)
        m.setattr(q, "_acquire_paint_lock", self._acquire)
        m.setattr(q, "_release_paint_lock",
                  lambda loop: self._mark("release"))
        m.setattr(q, "_sleep_vllm_for_generation",
                  lambda: self._mark("vllm_sleep"))
        m.setattr(q, "_unload_paint_pipeline", self._slow_unload)
        m.setattr(q, "_wake_vllm_after_generation",
                  lambda: self._mark("vllm_wake"))
        self._unload_sleep = unload_sleep

    def undo(self) -> None:
        self._monkey.undo()

    def _mark(self, e: str) -> None:
        with self.lock:
            self.events.append(e)

    def _slow_unload(self) -> None:
        self._mark("unload:start")
        time.sleep(self._unload_sleep)
        self._mark("unload:end")

    def _acquire(self, task_id: str, loop) -> bool:  # type: ignore[no-untyped-def]
        self._mark(f"start:{task_id}")
        return True

    def task(self, task_id: str, runner: Callable, **extra) -> dict:
        return _task(task_id, runner, **extra)


def _ok(result: str) -> Callable:
    def runner(task, check_cancel):  # type: ignore[no-untyped-def]
        return result
    return runner


def test_unified_basic_roundtrip(unified_queue: ImageTaskQueue) -> None:
    h = Harness(unified_queue)
    box: list[str] = []

    def runner_t1(task, check_cancel):  # type: ignore[no-untyped-def]
        box.append("ran")
        return "ok"

    h.q.submit(h.task("t1", runner_t1))
    _wait_until(lambda: bool(box), timeout=5.0, what="任务执行")
    # current 清理在 worker 外层 finally（晚于 on_finish 唤醒主线程）——
    # 立即断言是竞态（全量下稳定复现），改轮询等待
    _wait_until(lambda: h.q.snapshot()["current"] is None, timeout=5.0,
                what="worker 收尾清 current")
    h.undo()


def test_unified_snapshot_and_cancel_states(
        unified_queue: ImageTaskQueue) -> None:
    h = Harness(unified_queue)
    gate = threading.Event()

    def holder(task, check_cancel):  # type: ignore[no-untyped-def]
        gate.wait(3.0)
        return "held"

    h.q.submit(h.task("holder", holder))
    assert h.q.submit(h.task("q1", holder)) >= 1
    snap = h.q.snapshot()
    assert snap["queued"], "应有排队任务"
    assert h.q.cancel("q1") == "queued"
    assert h.q.cancel("nope") == "missing"
    gate.set()
    _wait_until(lambda: h.q.snapshot()["current"] is None,
                timeout=5.0, what="holder 完成")
    h.undo()


def test_unified_submit_and_wait_timeout(
        unified_queue: ImageTaskQueue) -> None:
    h = Harness(unified_queue)
    release = threading.Event()

    def stuck(task, check_cancel):  # type: ignore[no-untyped-def]
        while not h.q.is_cancelled("stuck"):
            if release.wait(0.05):
                break
        raise RuntimeError("cancelled")

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            await h.q.submit_and_wait(_task("stuck", stuck), timeout_s=0.3)

    asyncio.run(scenario())
    release.set()
    _wait_until(lambda: not h.q.is_cancelled("stuck"),
                timeout=5.0, what="超时撤销清理")
    h.undo()


def test_unified_priority_order(unified_queue: ImageTaskQueue) -> None:
    h = Harness(unified_queue)
    done: list[str] = []
    gate = threading.Event()

    def first(task, check_cancel):  # type: ignore[no-untyped-def]
        gate.wait(3.0)
        done.append("first")
        return "first"

    def low(task, check_cancel):  # type: ignore[no-untyped-def]
        done.append("low")
        return "low"

    def high(task, check_cancel):  # type: ignore[no-untyped-def]
        done.append("high")
        return "high"

    h.q.submit(h.task("first", first))
    h.q.submit(h.task("low", low, priority=1))
    h.q.submit(h.task("high", high, priority=9))
    gate.set()
    _wait_until(lambda: done == ["first", "high", "low"], timeout=5.0,
                what="unified 优先级排序")
    h.undo()


def test_unified_drain_tail_does_not_block_submit(
        unified_queue: ImageTaskQueue) -> None:
    h = Harness(unified_queue, unload_sleep=1.0)
    gate = threading.Event()

    def runner_a(task, check_cancel):  # type: ignore[no-untyped-def]
        gate.set()
        return "a"

    def submit_b_later() -> None:
        _wait_until(lambda: "unload:start" in h.events, timeout=5.0,
                    what="A 完成进入收尾")
        t0 = time.monotonic()
        h.q.submit(h.task("b", _ok("b")))
        elapsed = time.monotonic() - t0
        with h.lock:
            h.events.append(f"submit_b:{elapsed:.3f}")

    t = threading.Thread(target=submit_b_later, daemon=True)
    t.start()
    h.q.submit(h.task("a", runner_a))
    gate.wait(5.0)
    t.join(5.0)
    elapsed = next((float(e.split(":", 1)[1]) for e in h.events
                    if e.startswith("submit_b:")), None)
    assert elapsed is not None and elapsed < 0.5, (
        f"[unified] 收尾窗口 submit 被阻塞 {elapsed}s（冻结链#1 回潮）")
    _wait_until(lambda: "release" in h.events and "unload:end" in h.events,
                timeout=5.0, what="完整收尾")
    h.undo()
