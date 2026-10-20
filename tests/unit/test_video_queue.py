"""视频任务队列单元测试（2026-09-02 B 方案：单 worker 顺序消费）。

覆盖四类历史隐患的回归锁定：
  1. 跨镜多任务 FIFO 顺序消费 + pending→generating 状态区分
     （此前一律落 generating 0%，排队与卡死不可分辨）；
  2. 排队任务取消 = 直接出队不占 GPU（此前排队任务取消无效）；
  3. 运行中任务经 check_cancel 检查点取消；
  4. 功能锁/vLLM 唤醒只在队列排空时执行一次——镜间接力不再出现
     「上一镜收尾唤醒 vLLM 与下一镜采样叠载」窗口。

准入协商小方法（_acquire_video_gen 等）在 Harness 中替换为记录桩，
不触 GPU/事件循环。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from src.services.video_queue import VideoTaskCancelled, VideoTaskQueue


def _wait_until(pred: Callable[[], bool], timeout: float = 8.0,
                what: str = "条件") -> None:
    """轮询等待断言条件成立（超时失败带语义标签）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时: {what}")


class Harness:
    """测试替身：记录准入/状态事件，隔离锁与显存协商。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.status_log: dict[str, list[dict]] = {}
        self.q = VideoTaskQueue()
        monkeypatch.setattr(self.q, "_thermal_paused", lambda: False)
        monkeypatch.setattr(self.q, "_acquire_video_gen", self._acquire)
        monkeypatch.setattr(self.q, "_release_video_gen", self._release)
        monkeypatch.setattr(self.q, "_sleep_vllm_for_generation",
                            lambda: self.events.append("vllm_sleep"))
        monkeypatch.setattr(self.q, "_unload_paint_pipeline",
                            lambda: self.events.append("unload_paint"))
        # 2026-10-02 用户拍板「按需真实加载」：排空唤醒已摘除（原
        # _wake_vllm_after_generation / _wake_debounce_s patch 行删除；
        # 各用例 vllm_wake 断言翻转为 not-in 或改以 release 为收尾信号）
        monkeypatch.setattr(self.q, "_reconcile_orphan_tasks",
                            lambda task: None)

    # 准入替身（记录调用序）
    def _acquire(self, task_id: str, loop) -> bool:
        self.events.append(f"acquire:{task_id}")
        return True

    def _release(self, loop) -> None:
        self.events.append("release")

    # update_status 记录器（含状态字段才入 events，进度不刷屏）
    def update(self, task_id: str, fields: dict) -> None:
        self.status_log.setdefault(task_id, []).append(dict(fields))
        if "status" in fields:
            self.events.append(f"status:{task_id}:{fields['status']}")

    def task(self, task_id: str, runner: Callable,
             kind: str = "local") -> dict:
        return {"task_id": task_id, "kind": kind, "runner": runner,
                "loop": None, "update_status": self.update}

    def statuses(self, task_id: str) -> list[str]:
        return [f["status"] for f in self.status_log.get(task_id, [])
                if "status" in f]


def _blocking_runner(h: Harness, tid: str,
                     gate: threading.Event) -> Callable:
    """占位 runner：开跑即报 generating、放行后写 done。"""

    def runner(task: dict, check_cancel: Callable) -> None:
        h.update(tid, {"status": "runner_started"})
        gate.wait(timeout=10.0)
        h.update(tid, {"status": "done"})
    return runner


def _cooperative_cancel_runner(h: Harness, tid: str) -> Callable:
    """协作取消 runner：入口即查取消点（模拟采样轮询检查）。"""

    def runner(task: dict, check_cancel: Callable) -> None:
        h.update(tid, {"status": "runner_started"})
        try:
            check_cancel()
        except VideoTaskCancelled:
            h.update(tid, {"status": "cancelled"})
            return
        h.update(tid, {"status": "done"})
    return runner


def test_fifo_order_and_status_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """三任务 FIFO 顺序消费；排空才释放锁/唤醒一次。"""
    h = Harness(monkeypatch)
    gates = {tid: threading.Event() for tid in ("t1", "t2", "t3")}
    for tid in ("t1", "t2", "t3"):
        h.q.submit(h.task(tid, _blocking_runner(h, tid, gates[tid])))

    # t1 开跑：pending 由 submit 前调用方写入（端点职责），队列翻 generating
    _wait_until(lambda: "status:t1:generating" in h.events, what="t1 开跑")
    assert h.statuses("t1") == ["generating", "runner_started"]
    # t2/t3 仍排队：不出现在状态流里
    assert h.statuses("t2") == []
    # 排空循环只获取一次锁（接力不重复协商）
    assert h.events.count("acquire:t1") == 1

    gates["t1"].set()
    _wait_until(lambda: "status:t2:generating" in h.events, what="t2 接力")
    assert "vllm_wake" not in h.events, "接力中不得唤醒 vLLM（叠载窗口）"
    assert "release" not in h.events, "接力中不得释放功能锁"

    gates["t2"].set()
    _wait_until(lambda: "status:t3:generating" in h.events, what="t3 接力")
    gates["t3"].set()
    _wait_until(lambda: "release" in h.events, what="排空释放锁")
    # 全程一次 acquire / 一次 release；排空不唤醒（2026-10-02 按需真实加载）
    assert sum(1 for e in h.events if e.startswith("acquire:")) == 1
    assert h.events.count("release") == 1
    assert h.events.count("vllm_wake") == 0
    assert h.statuses("t3") == ["generating", "runner_started", "done"]


def test_cancel_queued_task_never_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """排队任务取消：立即出队，不占 GPU 直接终态。"""
    h = Harness(monkeypatch)
    gate1, gate3 = threading.Event(), threading.Event()
    h.q.submit(h.task("t1", _blocking_runner(h, "t1", gate1)))
    h.q.submit(h.task("t2", _blocking_runner(h, "t2", threading.Event())))
    h.q.submit(h.task("t3", _blocking_runner(h, "t3", gate3)))
    _wait_until(lambda: "status:t1:generating" in h.events, what="t1 开跑")

    assert h.q.cancel("t2") == "queued"
    assert h.q.position("t2") is None

    gate1.set()
    _wait_until(lambda: "status:t3:generating" in h.events, what="t3 跳过 t2 接力")
    gate3.set()
    _wait_until(lambda: "release" in h.events, what="排空收尾")
    # t2 从未开跑（无 runner_started/generating）
    assert h.statuses("t2") == []


def test_cancel_running_task_via_checkpoint(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """运行中任务取消：旗标 → runner 检查点抛取消 → 写终态。"""
    h = Harness(monkeypatch)
    started = threading.Event()
    proceed = threading.Event()

    def runner(task: dict, check_cancel: Callable) -> None:
        h.update("t1", {"status": "runner_started"})
        started.set()
        proceed.wait(timeout=10.0)  # 模拟采样进行中
        try:
            check_cancel()
        except VideoTaskCancelled:
            h.update("t1", {"status": "cancelled"})
            return
        h.update("t1", {"status": "done"})

    h.q.submit(h.task("t1", runner))
    _wait_until(started.is_set, what="t1 开跑")
    assert h.q.cancel("t1") == "running"
    proceed.set()
    _wait_until(lambda: "status:t1:cancelled" in h.events, what="取消终态")
    _wait_until(lambda: "release" in h.events, what="取消后排空收尾")


def test_next_kind_and_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """next_kind（H3 接力 keep_loaded 判据）与排队位次。"""
    h = Harness(monkeypatch)
    gate = threading.Event()
    h.q.submit(h.task("t1", _blocking_runner(h, "t1", gate),
                      kind="h3_chain"))
    h.q.submit(h.task("t2", _blocking_runner(h, "t2", threading.Event()),
                      kind="h3_chain"))
    h.q.submit(h.task("t3", _blocking_runner(h, "t3", threading.Event()),
                      kind="local"))
    _wait_until(lambda: "status:t1:generating" in h.events, what="t1 开跑")
    # t1 运行中，队首 t2 → 后继同为 H3 → keep_loaded 应为 True
    assert h.q.next_kind() == "h3_chain"
    assert h.q.position("t2") == 1
    assert h.q.position("t3") == 2
    assert h.q.position("t1") is None  # 运行中不在队列
    gate.set()
    _wait_until(lambda: "status:t2:generating" in h.events, what="t2 接力")
    # t3 为 local：后继非 H3 → keep_loaded 应为 False（释放权重给 diffusers）
    assert h.q.next_kind() == "local"


def test_runner_escape_cancelled_is_backstopped(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """runner 以取消信号逃逸时队列兜底写终态（前端轮询不挂死）。"""
    h = Harness(monkeypatch)

    def bad_runner(task: dict, check_cancel: Callable) -> None:
        h.update("t1", {"status": "runner_started"})
        raise VideoTaskCancelled("t1")  # 未捕获逃逸

    h.q.submit(h.task("t1", bad_runner))
    _wait_until(lambda: "status:t1:cancelled" in h.events, what="兜底终态")
    _wait_until(lambda: "release" in h.events, what="排空收尾")


def test_admission_cancel_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """等锁期间取消：任务直接终态，不进入 runner。"""
    h = Harness(monkeypatch)
    # 锁永远拿不到：等待循环里响应取消
    monkeypatch.setattr(h.q, "_acquire_video_gen",
                        lambda task_id, loop: False)

    def runner(task: dict, check_cancel: Callable) -> None:  # pragma: no cover
        h.update("t1", {"status": "runner_started"})

    h.q.submit(h.task("t1", runner))
    # worker 在等锁（1s 轮询），置取消旗标
    time.sleep(0.3)
    assert h.q.cancel("t1") == "running"
    _wait_until(lambda: "status:t1:cancelled" in h.events, what="等锁取消终态")
    assert "runner_started" not in h.statuses("t1")


# ── SH-GAP-04 视频自动重试（2026-10-21 拍板「做」）──────────────────


def test_auto_retry_once_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """本地任务失败→自动原样重入队一次→重试成功=done（恰跑 2 次）。"""
    h = Harness(monkeypatch)
    calls = {"n": 0}

    def flaky_runner(task: dict, check_cancel: Callable) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("COMFY 引擎瞬断")
        h.update(task["task_id"], {"status": "done"})

    h.q.submit(h.task("t-retry", flaky_runner))
    _wait_until(lambda: h.statuses("t-retry")[-1] == "done", what="重试后 done")
    assert calls["n"] == 2, "首败+自动重试恰跑 2 次"
    # 重入队前先翻回 pending（前端不悬挂在 error 态）
    assert "status:t-retry:pending" in h.events


def test_auto_retry_second_failure_honest_error(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """幂等铁律：同任务第二次失败不再重试，诚实落 error 终态。"""
    h = Harness(monkeypatch)
    calls = {"n": 0}

    def always_fail(task: dict, check_cancel: Callable) -> None:
        calls["n"] += 1
        raise RuntimeError("确定性失败")

    h.q.submit(h.task("t-perm", always_fail))
    _wait_until(lambda: h.statuses("t-perm")[-1] == "error",
                what="二败诚实 error")
    assert calls["n"] == 2, "重试恰好 1 次后放弃"


def test_auto_retry_never_for_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    """范围铁律：云端任务绝不自动重试（计费代价），失败即 error。"""
    h = Harness(monkeypatch)
    calls = {"n": 0}

    def cloud_fail(task: dict, check_cancel: Callable) -> None:
        calls["n"] += 1
        raise RuntimeError("云端 500")

    h.q.submit(h.task("t-cloud", cloud_fail, kind="cloud"))
    _wait_until(lambda: h.statuses("t-cloud")[-1] == "error",
                what="云端失败直落 error")
    assert calls["n"] == 1, "云端零自动重试"


def test_auto_retry_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """门控：video_auto_retry.enabled=false 时失败直落 error（回手动）。"""
    monkeypatch.setattr("src.config.get_config",
                        lambda: {"video_auto_retry": {"enabled": False}})
    h = Harness(monkeypatch)
    calls = {"n": 0}

    def fail_once(task: dict, check_cancel: Callable) -> None:
        calls["n"] += 1
        raise RuntimeError("失败")

    h.q.submit(h.task("t-gate", fail_once))
    _wait_until(lambda: h.statuses("t-gate")[-1] == "error",
                what="门控关=失败直落 error")
    assert calls["n"] == 1
# 本项目仅供学习使用，商业授权请+Q 3559331368
