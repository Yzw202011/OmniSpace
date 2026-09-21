"""队列丢失唤醒回归测试（round-2026-09-20 第一梯队 F1 根修）。

旧缺陷：_worker_loop 的「出队判空」与 cond.wait() 分属两个锁窗口，
两窗之间 submit 的 notify 落空 → wait() 无超时永久睡眠，任务滞留。
修复：判空与 wait 合并进同一锁窗口（对齐云道写法）。

本测试构造「worker 已进入等待、再提交」的确定性场景：先提交一个
空转任务让 worker 跑完进入 wait，随后新提交必须被及时消费——
旧实现下该提交将永久滞留（等待超时断言失败）。
"""
from __future__ import annotations

import threading
import time

from src.services.task_queue import QueueSpec, TaskQueueCore


class _IdleHost:
    """最小宿主：钩子全空转（与 test_task_queue_core.FakeHost 同构）。"""

    def __init__(self) -> None:
        self.spec = QueueSpec(
            name="唤醒", acquire_hook="_acquire", release_hook="_release",
            thermal_hook="_thermal", vllm_sleep_hook="_sleep",
            wake_hook="_wake", budget_admit_hook="_admit",
            budget_release_hook="_release_budget",
            cloud_concurrency_hook="_cloud_limit",
            wait_poll_s=0.05, thermal_poll_s=0.05, label="唤醒")
        self.core = TaskQueueCore(host=self, spec=self.spec)

    def _acquire(self, task_id, loop) -> bool:  # type: ignore[no-untyped-def]
        return True

    def _release(self, loop) -> None:  # type: ignore[no-untyped-def]
        pass

    def _thermal(self) -> bool:
        return False

    def _sleep(self) -> None:
        pass

    def _wake(self) -> None:
        pass

    def _admit(self, task: dict) -> None:
        pass

    def _release_budget(self, task: dict) -> None:
        pass

    def _cloud_limit(self) -> int:
        return 1

    def _make_check_cancel(self, task_id: str) -> callable:  # type: ignore[no-untyped-def]
        def check() -> None:
            if self.core.is_cancelled(task_id):
                raise RuntimeError(f"cancelled:{task_id}")
        return check


def test_worker_wakeup_after_idle() -> None:
    """worker 空闲进入等待后，新提交必须被消费（丢失唤醒回归）。"""
    h = _IdleHost()
    ran = threading.Event()

    def runner(task, check_cancel):  # type: ignore[no-untyped-def]
        ran.set()
        return "ok"

    # 先提交一个即完任务并等它跑完 → worker 完成收尾后进入 cond.wait
    first_done = threading.Event()
    h.core.submit({"task_id": "t0", "kind": "test",
                   "runner": lambda t, c: (first_done.set(), "ok")[1],
                   "loop": None})
    deadline = time.monotonic() + 5.0
    while not first_done.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert first_done.is_set(), "首任务未执行（前置失败）"
    time.sleep(0.3)  # 让 worker 走完收尾进入等待

    # 关键窗口：此刻 submit 的 notify 必须命中在锁内等待的 worker
    h.core.submit({"task_id": "t1", "kind": "test", "runner": runner,
                   "loop": None})
    assert ran.wait(timeout=3.0), (
        "提交未被消费：worker 丢失唤醒仍在睡眠（F1 回归）")
