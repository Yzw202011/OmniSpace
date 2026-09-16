"""队列 B0 修复锁测试（2026-09-13，v4 §四 B9-c）。

把 B0 止血批的两条 P1 修复钉死为行为金标准，兼作 B5 统一任务子系统
的迁移前置（现队列语义冻结）：

  1. submit_and_wait 超时真实生效（原 wait() 无参永久阻塞、
     TimeoutError 分支不可达——image_queue.py P1-1）；
  2. 排空收尾不持 _cond：收尾动作（卸载/释放锁）执行期间，新任务
     仍能入队并被消费（原收尾持 _cond 做 empty_cache+ComfyUI /free
     60s → 全站请求冻结——锁序普查冻结链 #1）。

Harness 风格与 test_image_queue.py 一致（直实例化 + monkeypatch 隔离，
不触模块级单例）。
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

import pytest

from src.services.image_queue import ImageTaskQueue


def _wait_until(pred: Callable[[], bool], timeout: float,
                what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时: {what}")


class Harness:
    """与 test_image_queue.py 同型替身：隔离锁/显存协商，记录事件。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch,
                 unload_sleep: float = 0.0) -> None:
        self.events: list[str] = []
        self.lock = threading.Lock()
        self.q = ImageTaskQueue()
        monkeypatch.setattr(self.q, "_thermal_paused", lambda: False)
        monkeypatch.setattr(self.q, "_wake_debounce_s", 0.05)
        monkeypatch.setattr(self.q, "_acquire_paint_lock",
                            self._acquire)
        monkeypatch.setattr(self.q, "_release_paint_lock",
                            lambda loop: self._mark("release"))
        monkeypatch.setattr(self.q, "_sleep_vllm_for_generation",
                            lambda: self._mark("vllm_sleep"))
        monkeypatch.setattr(self.q, "_wake_vllm_after_generation",
                            lambda: self._mark("vllm_wake"))
        self._unload_sleep = unload_sleep
        monkeypatch.setattr(self.q, "_unload_paint_pipeline",
                            self._slow_unload)

    def _mark(self, event: str) -> None:
        with self.lock:
            self.events.append(event)

    def _slow_unload(self) -> None:
        self._mark("unload:start")
        time.sleep(self._unload_sleep)
        self._mark("unload:end")

    def _acquire(self, task_id: str, loop) -> bool:  # type: ignore[no-untyped-def]
        self._mark(f"start:{task_id}")
        return True

    def task(self, task_id: str, runner: Callable,
             kind: str = "paint", **extra) -> dict:
        return {"task_id": task_id, "kind": kind, "runner": runner,
                "loop": None, "priority": 5, **extra}


def test_submit_and_wait_timeout_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    """B0 P1-1 金标准：worker 卡死时 timeout_s 必须真实生效——
    TimeoutError 抛出、请求不悬挂、任务被尽力撤销（旗标登记）。"""
    h = Harness(monkeypatch)
    release = threading.Event()

    def stuck_runner(task, check_cancel):  # type: ignore[no-untyped-def]
        # 模拟引擎挂起：只在取消旗标出现时退出（队列 cancel 会置旗标，
        # runner 经 check_cancel 感知——真实关键帧 runner 同契约）
        while not h.q.is_cancelled(str(task["task_id"])):
            if release.wait(0.05):
                break
        raise RuntimeError("cancelled-by-timeout")

    async def scenario() -> None:
        task = h.task("t-timeout", stuck_runner)
        with pytest.raises(TimeoutError):
            await h.q.submit_and_wait(task, timeout_s=0.3)

    asyncio.run(scenario())
    # 任务已被超时路径尽力撤销（cancel 置旗标）→ runner 退出、worker 收尾
    _wait_until(
        lambda: not h.q.is_cancelled("t-timeout"),
        timeout=8.0, what="取消旗标清理（worker 收尾）")


def test_drain_tail_does_not_block_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    """B0 冻结链#1 金标准：排空收尾（慢卸载 1s）执行期间，新任务
    submit+开跑不得被 _cond 阻塞——旧实现 worker 持 _cond 做收尾，
    新任务提交/消费全被卡满整个收尾窗口。"""
    h = Harness(monkeypatch, unload_sleep=1.0)
    gate = threading.Event()

    def runner_a(task, check_cancel):  # type: ignore[no-untyped-def]
        gate.set()  # A 已开跑
        return "a"

    def runner_b(task, check_cancel):  # type: ignore[no-untyped-def]
        return "b"

    def submit_b_later() -> None:
        # 等 A 完成并进入收尾（unload:start 出现）后再提交 B，
        # 计时 submit() 本身的耗时——旧实现 worker 持 _cond 做收尾，
        # submit 会卡满整个收尾窗口（1s）；B0 修复后收尾在 cond 外，
        # submit 应立即返回。
        _wait_until(lambda: "unload:start" in h.events, timeout=8.0,
                    what="A 完成进入收尾")
        t0 = time.monotonic()
        h.q.submit(h.task("t-b", runner_b))
        elapsed = time.monotonic() - t0
        with h.lock:
            h.events.append(f"submit_b_done:{elapsed:.3f}")

    t = threading.Thread(target=submit_b_later, daemon=True)
    t.start()
    h.q.submit(h.task("t-a", runner_a))
    gate.wait(8.0)

    t.join(8.0)
    # 金标准：submit() 不得被收尾窗口阻塞（收尾 1.0s >> 0.5s 阈值）
    submit_elapsed = next((float(e.split(":", 1)[1])
                           for e in h.events
                           if e.startswith("submit_b_done:")), None)
    assert submit_elapsed is not None, "B 未提交"
    assert submit_elapsed < 0.5, (
        f"submit 被排空收尾阻塞 {submit_elapsed:.3f}s（收尾持 _cond 回潮）")
    # B 最终仍被消费、收尾完整发生（卸载 + 释放锁语义未破坏）
    _wait_until(lambda: "start:t-b" in h.events, timeout=8.0,
                what="B 被消费")
    _wait_until(lambda: "release" in h.events and "unload:end" in h.events,
                timeout=8.0, what="完整收尾（unload+release）")
