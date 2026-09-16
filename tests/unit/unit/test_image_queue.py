"""图像任务队列单元测试（2026-09-02 生图 B 方案：单 worker 统一队列）。

覆盖：
  1. FIFO + 优先级排序 + 排空才释放锁/卸载/唤醒（接力免 churn）；
  2. submit_and_wait 返回 runner 结果 / 异常原样上抛 / 准入失败不悬挂
     （on_finish 契约）；
  3. 排队取消直接出队；
  4. set_priority 重排；
  5. 等锁期间 wait_progress_cb 排队广播。
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

import pytest

from src.services.image_queue import ImageTaskCancelled, ImageTaskQueue


def _wait_until(pred: Callable[[], bool], timeout: float = 8.0,
                what: str = "条件") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时: {what}")


class Harness:
    """测试替身：记录准入/等待广播事件，隔离锁与显存协商。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.q = ImageTaskQueue()
        monkeypatch.setattr(self.q, "_thermal_paused", lambda: False)
        monkeypatch.setattr(self.q, "_wake_debounce_s", 0.05)  # V9-β 去抖窗（单测缩短）
        monkeypatch.setattr(self.q, "_acquire_paint_lock", self._acquire)
        monkeypatch.setattr(self.q, "_release_paint_lock",
                            lambda loop: self.events.append("release"))
        monkeypatch.setattr(self.q, "_sleep_vllm_for_generation",
                            lambda: self.events.append("vllm_sleep"))
        monkeypatch.setattr(self.q, "_unload_paint_pipeline",
                            lambda: self.events.append("unload"))
        monkeypatch.setattr(self.q, "_wake_vllm_after_generation",
                            lambda: self.events.append("vllm_wake"))

    def _acquire(self, task_id: str, loop) -> bool:
        self.events.append(f"acquire:{task_id}")
        return True

    def task(self, task_id: str, runner: Callable, kind: str = "paint",
             priority: int = 5, **extra) -> dict:
        return {"task_id": task_id, "kind": kind, "runner": runner,
                "loop": None, "priority": priority, **extra}

    def starts(self, task_id: str) -> bool:
        return any(e.startswith(f"start:{task_id}") for e in self.events)


def _gate_runner(h: Harness, tid: str, gate: threading.Event) -> Callable:
    def runner(task: dict, check_cancel: Callable) -> str:
        h.events.append(f"start:{tid}")
        gate.wait(timeout=10.0)
        return f"done:{tid}"
    return runner


def test_fifo_priority_and_drain_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """FIFO 顺序 + 高优先插队 + 排空才一次卸载/释放/唤醒。

    单 worker 语义：已在跑的任务不被抢占（t1 先弹先跑），优先级只
    决定等待队列的出队序——t1 完成后 t3（p9）越过 t2（p5）接棒。
    """
    h = Harness(monkeypatch)
    g1, g2, g3 = threading.Event(), threading.Event(), threading.Event()
    h.q.submit(h.task("t1", _gate_runner(h, "t1", g1)))          # 先入队先跑
    _wait_until(lambda: h.starts("t1"), what="t1 先开跑")
    h.q.submit(h.task("t2", _gate_runner(h, "t2", g2)))          # 排队
    h.q.submit(h.task("t3", _gate_runner(h, "t3", g3), priority=9))  # 插队
    # t3 优先级 9 > 5：等待队列队首为 t3
    assert h.q.position("t3") == 1
    g1.set()
    _wait_until(lambda: h.starts("t3"), what="t3 插队接力")
    assert not h.starts("t2"), "低优先 t2 不得越过 t3"
    assert "vllm_wake" not in h.events
    g3.set()
    _wait_until(lambda: h.starts("t2"), what="t2 接力")
    g2.set()
    _wait_until(lambda: "vllm_wake" in h.events, what="排空唤醒")
    assert h.events.count("vllm_wake") == 1
    assert h.events.count("unload") == 1
    assert h.events.count("release") == 1
    assert sum(1 for e in h.events if e.startswith("acquire:")) == 1


def test_submit_and_wait_result_and_error(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """submit_and_wait：结果回传 / 异常上抛（等待方绝不悬挂）。"""
    h = Harness(monkeypatch)

    def ok_runner(task, check_cancel):  # noqa: ANN001
        return {"k": 42}

    def bad_runner(task, check_cancel):  # noqa: ANN001
        raise RuntimeError("boom")

    async def scenario() -> None:
        r = await h.q.submit_and_wait(h.task("w1", ok_runner))
        assert r == {"k": 42}
        with pytest.raises(RuntimeError, match="boom"):
            await h.q.submit_and_wait(h.task("w2", bad_runner))

    asyncio.run(scenario())


def test_submit_and_wait_admission_failure_no_hang(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """准入阶段失败（事件循环不可用）也必须唤醒等待方。"""
    h = Harness(monkeypatch)
    monkeypatch.setattr(
        h.q, "_acquire_paint_lock",
        lambda task_id, loop: (_ for _ in ()).throw(RuntimeError("loop gone")))

    def any_runner(task, check_cancel):  # noqa: ANN001  # pragma: no cover
        raise AssertionError("runner 不应被调用")

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="loop gone"):
            await h.q.submit_and_wait(h.task("w3", any_runner))

    asyncio.run(scenario())


def test_cancel_queued_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """排队任务取消：出队不跑，后继顶上。"""
    h = Harness(monkeypatch)
    g1, g3 = threading.Event(), threading.Event()
    h.q.submit(h.task("t1", _gate_runner(h, "t1", g1)))
    h.q.submit(h.task("t2", _gate_runner(h, "t2", threading.Event())))
    h.q.submit(h.task("t3", _gate_runner(h, "t3", g3)))
    _wait_until(lambda: h.starts("t1"), what="t1 开跑")
    assert h.q.cancel("t2") == "queued"
    g1.set()
    _wait_until(lambda: h.starts("t3"), what="t3 跳过 t2")
    g3.set()
    _wait_until(lambda: "vllm_wake" in h.events, what="排空收尾")
    assert not h.starts("t2")


def test_set_priority_reorders(monkeypatch: pytest.MonkeyPatch) -> None:
    """排队中调优先级 → 队首换人。"""
    h = Harness(monkeypatch)
    h.q.submit(h.task("t1", _gate_runner(h, "t1", threading.Event())))
    h.q.submit(h.task("t2", _gate_runner(h, "t2", threading.Event())))
    assert h.q.set_priority("t2", 9) is True
    assert h.q.position("t2") == 1
    assert h.q.set_priority("t-missing", 9) is False


def test_wait_progress_cb_broadcasts(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """等锁期间排队广播：wait_progress_cb 收到位次。"""
    h = Harness(monkeypatch)
    waits: list[tuple[str, int]] = []
    lock_state = {"free": False}

    def slow_acquire(task_id: str, loop) -> bool:
        h.events.append(f"acquire-try:{task_id}")
        if not lock_state["free"]:
            return False
        return True

    monkeypatch.setattr(h.q, "_acquire_paint_lock", slow_acquire)
    g1 = threading.Event()

    def wait_cb(task_id: str, pos: int) -> None:
        waits.append((task_id, pos))
        # 第二次广播后放锁
        if len(waits) >= 2:
            lock_state["free"] = True

    def unlocker() -> None:
        time.sleep(0.2)
        lock_state["free"] = True

    threading.Thread(target=unlocker, daemon=True).start()
    h.q.submit(h.task("t1", _gate_runner(h, "t1", g1),
                      wait_progress_cb=wait_cb))
    _wait_until(lambda: h.starts("t1"), what="t1 等锁后开跑")
    assert len(waits) >= 1, "等待期间必须有排队广播"
    g1.set()
    _wait_until(lambda: "vllm_wake" in h.events, what="排空收尾")


def test_runner_cancelled_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """运行中取消：check_cancel 检查点抛 ImageTaskCancelled，收尾正常。"""
    h = Harness(monkeypatch)
    started = threading.Event()
    proceed = threading.Event()
    seen: list[str] = []

    def runner(task: dict, check_cancel: Callable) -> None:
        started.set()
        proceed.wait(timeout=10.0)
        try:
            check_cancel()
        except ImageTaskCancelled:
            seen.append("cancelled")
            raise

    h.q.submit(h.task("t1", runner))
    _wait_until(started.is_set, what="t1 开跑")
    assert h.q.cancel("t1") == "running"
    proceed.set()
    _wait_until(lambda: "cancelled" in seen, what="检查点命中")
    _wait_until(lambda: "vllm_wake" in h.events, what="取消后排空收尾")
# 本项目仅供学习使用，商业授权请+Q 3559331368
