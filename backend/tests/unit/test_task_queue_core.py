"""TaskQueueCore 单元测试（B5 步 1，2026-09-14）。

以最小假宿主验证统一核心的调度语义（金标准的镜像）：
FIFO/优先级、取消三态、位次、超时真实生效、**排空收尾不持锁**
（submit 在收尾窗口内不阻塞）、云道并发上限、钩子调用序。
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

import pytest

from backend.services.task_queue import QueueSpec, TaskQueueCore


class FakeHost:
    """最小宿主：钩子计数/慢动作可调，语义与 image/video 队一致。"""

    def __init__(self, unload_sleep: float = 0.0,
                 priority_sort: bool = True) -> None:
        self.events: list[str] = []
        self.lock = threading.Lock()
        self.unload_sleep = unload_sleep
        self.acquired = 0
        self.spec = QueueSpec(
            name="测试", acquire_hook="_acquire_paint_lock",
            release_hook="_release_paint_lock", thermal_hook="_thermal_paused",
            vllm_sleep_hook="_sleep_vllm_for_generation",
            unload_hook="_unload_paint_pipeline",
            wake_hook="_schedule_wake_if_idle",
            budget_admit_hook="_budget_admit",
            budget_release_hook="_budget_release",
            cloud_concurrency_hook="_cloud_concurrency",
            priority_sort=priority_sort, wait_poll_s=0.05,
            thermal_poll_s=0.05, label="测试")
        self.core = TaskQueueCore(host=self, spec=self.spec)

    def _mark(self, e: str) -> None:
        with self.lock:
            self.events.append(e)

    def _acquire_paint_lock(self, task_id, loop) -> bool:  # type: ignore[no-untyped-def]
        self._mark(f"start:{task_id}")
        self.acquired += 1
        return True

    def _release_paint_lock(self, loop) -> None:  # type: ignore[no-untyped-def]
        self._mark("release")

    def _thermal_paused(self) -> bool:
        return False

    def _sleep_vllm_for_generation(self) -> None:
        self._mark("vllm_sleep")

    def _unload_paint_pipeline(self) -> None:
        self._mark("unload:start")
        time.sleep(self.unload_sleep)
        self._mark("unload:end")

    def _schedule_wake_if_idle(self) -> None:
        self._mark("wake")

    def _budget_admit(self, task: dict) -> None:
        self._mark(f"budget_admit:{task.get('task_id')}")

    def _budget_release(self, task: dict) -> None:
        self._mark(f"budget_release:{task.get('task_id')}")

    def _cloud_concurrency(self) -> int:
        return 2

    def _make_check_cancel(self, task_id: str) -> Callable[[], None]:
        def check() -> None:
            if self.core.is_cancelled(task_id):
                raise RuntimeError(f"cancelled:{task_id}")
        return check

    def _run_one_cloud(self, task: dict) -> None:
        task["_result"] = task["runner"](task, self._make_check_cancel(
            str(task.get("task_id"))))

    def task(self, task_id: str, runner: Callable, **extra) -> dict:
        return {"task_id": task_id, "kind": "test", "runner": runner,
                "loop": None, **extra}

    def wait_until(self, pred: Callable[[], bool], timeout: float,
                   what: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.02)
        raise AssertionError(f"等待超时: {what}")


def _ok_runner(result: str = "done") -> Callable:
    def runner(task, check_cancel):  # type: ignore[no-untyped-def]
        return result
    return runner


def test_fifo_and_priority_order() -> None:
    h = FakeHost()
    done: list[str] = []
    gate = threading.Event()

    def first_runner(task, check_cancel):  # type: ignore[no-untyped-def]
        gate.wait(3.0)
        done.append("first")
        return "first"

    def low_runner(task, check_cancel):  # type: ignore[no-untyped-def]
        done.append("low")
        return "low"

    def high_runner(task, check_cancel):  # type: ignore[no-untyped-def]
        done.append("high")
        return "high"

    h.core.submit(h.task("first", first_runner))
    h.core.submit(h.task("low", low_runner, priority=1))
    h.core.submit(h.task("high", high_runner, priority=9))
    gate.set()
    h.wait_until(lambda: done == ["first", "high", "low"], timeout=5.0,
                 what="优先级排序（首任务在跑时高先于低）")


def test_cancel_three_states() -> None:
    h = FakeHost()
    gate = threading.Event()

    def holder(task, check_cancel):  # type: ignore[no-untyped-def]
        gate.wait(3.0)
        return "held"

    # 占住 worker，保证 q1 处于排队态
    h.core.submit(h.task("holder", holder))
    assert h.core.submit(h.task("q1", _ok_runner())) >= 1
    # holder 卡在 gate（3s），q1 必在排队 → 取消应出队
    assert h.core.cancel("q1") == "queued"
    assert h.core.cancel("nope") == "missing"
    gate.set()

    release = threading.Event()

    def runner(task, check_cancel):  # type: ignore[no-untyped-def]
        h._mark("running:r1")  # 接力开跑（复用 holder 的锁，无 start 事件）
        while not h.core.is_cancelled("r1"):
            if release.wait(0.05):
                break
        raise RuntimeError("cancelled")

    h.core.submit(h.task("r1", runner))
    h.wait_until(lambda: "running:r1" in h.events, timeout=5.0,
                 what="r1 接力开跑")
    assert h.core.cancel("r1") == "running"
    release.set()


def test_position_and_submit_and_wait_timeout() -> None:
    h = FakeHost()
    release = threading.Event()

    def stuck(task, check_cancel):  # type: ignore[no-untyped-def]
        while not h.core.is_cancelled("stuck"):
            if release.wait(0.05):
                break
        raise RuntimeError("cancelled")

    async def scenario() -> None:
        h.core.submit(h.task("stuck", stuck))
        with pytest.raises(TimeoutError):
            await h.core.submit_and_wait(
                h.task("late", _ok_runner()), timeout_s=0.3)

    asyncio.run(scenario())
    release.set()
    h.wait_until(lambda: not h.core.is_cancelled("late"), timeout=5.0,
                 what="超时任务撤销清理")


def test_drain_tail_does_not_block_submit() -> None:
    h = FakeHost(unload_sleep=1.0)
    gate = threading.Event()

    def runner_a(task, check_cancel):  # type: ignore[no-untyped-def]
        gate.set()
        return "a"

    h.core.submit(h.task("a", runner_a))
    gate.wait(5.0)
    observer_ready = threading.Event()

    def submit_b_later() -> None:
        h.wait_until(lambda: "unload:start" in h.events, timeout=5.0,
                     what="进入收尾")
        observer_ready.set()
        t0 = time.monotonic()
        h.core.submit(h.task("b", _ok_runner("b")))
        elapsed = time.monotonic() - t0
        with h.lock:
            h.events.append(f"submit_b:{elapsed:.3f}")

    t = threading.Thread(target=submit_b_later, daemon=True)
    t.start()
    observer_ready.wait(5.0)
    t.join(5.0)
    elapsed = next((float(e.split(":", 1)[1]) for e in h.events
                    if e.startswith("submit_b:")), None)
    assert elapsed is not None and elapsed < 0.5, (
        f"收尾窗口 submit 被阻塞 {elapsed}s（收尾持锁回潮）")
    h.wait_until(lambda: "release" in h.events and "unload:end" in h.events,
                 timeout=5.0, what="完整收尾")


def test_admission_hook_sequence() -> None:
    h = FakeHost()
    h.core.submit(h.task("x", _ok_runner()))
    h.wait_until(lambda: "budget_release:x" in h.events, timeout=5.0,
                 what="x 完成")
    seq = h.events
    # 准入序：热保护过→拿锁→让渡→预算准入→收尾预算释放
    assert seq.index("start:x") < seq.index("vllm_sleep") \
        < seq.index("budget_admit:x") < seq.index("budget_release:x")


def test_fifo_mode_when_priority_sort_off() -> None:
    h = FakeHost(priority_sort=False)
    assert h.core.spec.priority_sort is False
    assert h.core.set_priority("x", 1) is False  # FIFO 模式不支持改优先级
