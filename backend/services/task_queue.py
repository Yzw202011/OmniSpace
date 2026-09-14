"""通用任务队列核心（B5 统一任务子系统 2026-09-14，v4 §23.2）。

把 image_queue / video_queue 両队的**共同调度语义**收敛为单一实现：
双道（本地单 worker + 可选云端并发池）、取消三态、位次、优先级、
排空收尾在锁外（B0 冻结链#1 语义）、submit_and_wait 超时真实生效
（B0 P1-1 语义）。

宿主回调面（差异注入）：Core 不 import 业务模块，一切差异经 QueueSpec
的钩子名 + host 实例回调——monkeypatch 宿主实例方法（金标准 Harness
手法）对 unified 路径同样生效。差异清单：
  acquire/release 锁方法名、热保护与等锁轮询周期、优先级排序开关、
  云道并发函数、让渡/卸载/唤醒/预算钩子名、日志前缀。

灰度：config ``task_queue.impl = legacy | unified``（默认 legacy，
行为零变更合入；host 侧 __init__ 按开关选择走 Core 或既有内联实现）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("omnispace.services.task_queue")

_CLOUD_POOL_MAX = 8


@dataclass
class QueueSpec:
    """宿主差异清单（全部为宿主上的钩子名或常量）。"""
    name: str                       # 日志前缀（"图像"/"视频"）
    acquire_hook: str               # 宿主获取功能锁方法名 (task_id, loop)->bool
    release_hook: str               # 宿主释放锁方法名 (loop)->None
    thermal_hook: str               # 热保护暂停查询 ()->bool
    vllm_sleep_hook: str            # 生成期让渡 ()->None
    unload_hook: str                # 排空收尾卸载 ()->None
    wake_hook: str                  # 排空去抖唤醒 ()->None
    budget_admit_hook: str          # 预算准入 (task)->None
    budget_release_hook: str        # 预算收尾 (task)->None
    cloud_concurrency_hook: str     # 云道并发上限 ()->int
    wait_poll_s: float = 1.0
    thermal_poll_s: float = 2.0
    priority_sort: bool = True      # False=纯 FIFO（video 语义）
    default_priority: int = 5
    label: str = "task"             # 任务种类日志词（"图像"/"视频"）


class TaskQueueCore:
    """统一调度核心。host 必须提供：spec 列出的全部钩子、
    _make_check_cancel(task_id)、_run_cloud_one(task)（云道执行体）。

    线程模型（与 legacy 逐比特同型，B0 语义）：
      - 本地道单 worker：cond 内只「取任务/翻转收尾旗标」，卸载/放锁/
        唤醒一律在 cond 外串行执行；
      - 云道 dispatcher：并发池按 host 云并发上限认领 cloud=True 任务；
      - submit/position/cancel/set_priority/snapshot 快进快出 _cond。
    """

    def __init__(self, host: Any, spec: QueueSpec) -> None:
        self._host = host
        self.spec = spec
        self._queue: deque[dict] = deque()
        self._cond = threading.Condition()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._cloud_worker: threading.Thread | None = None
        self._cloud_pool: Any = None
        self._cloud_running: dict[str, dict] = {}
        self._current: dict | None = None
        self._cancel_flags: set[str] = set()
        self._lock_held = False
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── 宿主钩子快捷方式 ────────────────────────────────────────
    def _h(self, name: str) -> Callable:
        return getattr(self._host, name)

    # ── 对外接口（与 legacy 语义一致）───────────────────────────
    def submit(self, task: dict) -> int:
        if self.spec.priority_sort:
            task.setdefault("priority", self.spec.default_priority)
        with self._cond:
            self._queue.append(task)
            if self.spec.priority_sort:
                self._queue = deque(sorted(
                    self._queue, key=lambda t: -int(t.get("priority", 5))))
            loop = task.get("loop")
            if loop is not None:
                self._loop = loop
            position = self._position_locked(str(task.get("task_id")))
            assert position is not None, "刚入队任务必有位次"
            self._cond.notify_all()
        self._ensure_worker()
        log.info("%s任务入队: %s kind=%s 位次=%d pri=%s cloud=%s",
                 self.spec.name, task.get("task_id"), task.get("kind"),
                 position, task.get("priority"), bool(task.get("cloud")))
        return position

    async def submit_and_wait(self, task: dict,
                              timeout_s: float = 3600.0) -> Any:
        woken = threading.Event()
        box: dict[str, Any] = {"error": None}

        def on_finish(err: BaseException | None) -> None:
            box["error"] = err
            woken.set()

        task = {**task, "on_finish": on_finish}
        self.submit(task)
        loop = asyncio.get_running_loop()
        woken_in_time = await loop.run_in_executor(
            None, lambda: woken.wait(timeout_s))
        if not woken_in_time:
            tid = str(task.get("task_id") or "")
            if tid:
                self.cancel(tid)
            raise TimeoutError(
                f"{self.spec.label}任务等待超时（>{timeout_s:.0f}s）")
        err = box["error"]
        if err is not None:
            raise err
        return task.get("_result")

    def cancel(self, task_id: str) -> str:
        with self._cond:
            for i, t in enumerate(self._queue):
                if str(t.get("task_id")) == task_id:
                    del self._queue[i]
                    log.info("%s排队任务取消出队: %s", self.spec.name, task_id)
                    return "queued"
            running = (
                (self._current is not None
                 and str(self._current.get("task_id")) == task_id)
                or task_id in self._cloud_running)
        if running:
            with self._cond:
                self._cancel_flags.add(task_id)
            log.info("%s运行中任务置取消旗标: %s", self.spec.name, task_id)
            return "running"
        return "missing"

    def set_priority(self, task_id: str, priority: int) -> bool:
        if not self.spec.priority_sort:
            return False
        with self._cond:
            for t in self._queue:
                if str(t.get("task_id")) == task_id:
                    t["priority"] = int(priority)
                    self._queue = deque(sorted(
                        self._queue, key=lambda x: -int(x.get("priority", 5))))
                    return True
        return False

    def position(self, task_id: str) -> int | None:
        with self._cond:
            return self._position_locked(task_id)

    def snapshot(self) -> dict:
        with self._cond:
            return {
                "queued": [
                    {"task_id": str(t.get("task_id")), "kind": t.get("kind"),
                     "priority": int(t.get("priority", 5)),
                     "cloud": bool(t.get("cloud"))}
                    for t in self._queue],
                "current": (self._current or {}).get("task_id"),
                "cloud_running": list(self._cloud_running.keys()),
                "lock_held": self._lock_held,
            }

    def is_cancelled(self, task_id: str) -> bool:
        return str(task_id) in self._cancel_flags

    # ── 内部 ────────────────────────────────────────────────────
    def _position_locked(self, task_id: str) -> int | None:
        for i, t in enumerate(self._queue):
            if str(t.get("task_id")) == task_id:
                return i + 1
        return None

    def _pop_lane_locked(self, cloud: bool) -> dict | None:
        for i, t in enumerate(self._queue):
            if bool(t.get("cloud")) == cloud:
                del self._queue[i]
                return t
        return None

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if not (self._worker and self._worker.is_alive()):
                self._worker = threading.Thread(
                    target=self._worker_loop, daemon=True,
                    name=f"{self.spec.name.lower()}-queue-unified")
                self._worker.start()
            if not (self._cloud_worker and self._cloud_worker.is_alive()):
                self._cloud_worker = threading.Thread(
                    target=self._cloud_dispatcher_loop, daemon=True,
                    name=f"{self.spec.name.lower()}-queue-cloud-unified")
                self._cloud_worker.start()

    def _worker_loop(self) -> None:
        while True:
            drain = False
            with self._cond:
                task = self._pop_lane_locked(cloud=False)
                if task is not None:
                    self._current = task
                elif self._lock_held:
                    # 旗标翻转在 cond 内；收尾动作在 cond 外（B0 语义：
                    # 收尾窗口内 submit/position/cancel 不被阻塞）
                    self._lock_held = False
                    drain = True
            if drain:
                try:
                    self._h(self.spec.unload_hook)()
                finally:
                    self._h(self.spec.release_hook)(self._loop)
                    self._h(self.spec.wake_hook)()
                continue  # 重取：收尾窗口内新入队任务立即消费
            if task is None:
                with self._cond:
                    self._cond.wait()
                continue
            task_id = str(task.get("task_id"))
            try:
                self._run_one(task)
            except Exception as exc:  # noqa: BLE001 - worker 永不退出
                log.error("%s队列任务异常逃逸: %s: %s",
                          self.spec.name, task_id, exc)
            finally:
                with self._cond:
                    if self._current is task:
                        self._current = None
                    self._cancel_flags.discard(task_id)

    def _run_one(self, task: dict) -> None:
        task_id = str(task.get("task_id"))
        err: BaseException | None = None
        finish: Callable | None = task.get("on_finish")
        try:
            self._wait_admission(task)
            log.info("%s任务开跑: %s kind=%s",
                     self.spec.name, task_id, task.get("kind"))
            try:
                task["_result"] = task["runner"](
                    task, self._host._make_check_cancel(task_id))
            except Exception as exc:  # noqa: BLE001 - 统一记账
                err = exc
        except Exception as exc:  # noqa: BLE001 - 准入失败/取消
            log.error("%s任务准入失败: %s: %s", self.spec.name, task_id, exc)
            err = exc
        finally:
            self._h(self.spec.budget_release_hook)(task)
            if finish is not None:
                try:
                    finish(err)
                except Exception as exc:  # noqa: BLE001
                    log.warning("on_finish 钩子异常: %s", exc)

    def _wait_admission(self, task: dict) -> None:
        task_id = str(task.get("task_id"))
        check = self._host._make_check_cancel(task_id)
        wait_cb = task.get("wait_progress_cb")
        while self._h(self.spec.thermal_hook)():
            check()
            if wait_cb:
                self._call_wait_cb(wait_cb, task_id)
            time.sleep(self.spec.thermal_poll_s)
        with self._cond:
            held = self._lock_held
        if not held:
            while True:
                check()
                if wait_cb:
                    self._call_wait_cb(wait_cb, task_id)
                if self._h(self.spec.acquire_hook)(task_id, task.get("loop")):
                    with self._cond:
                        self._lock_held = True
                    break
                time.sleep(self.spec.wait_poll_s)
        else:
            if wait_cb:
                self._call_wait_cb(wait_cb, task_id)
        self._h(self.spec.vllm_sleep_hook)()
        self._h(self.spec.budget_admit_hook)(task)

    def _call_wait_cb(self, wait_cb: Callable, task_id: str) -> None:
        """排队位次广播（legacy 语义：宿主 position 实查，失败降 1）。"""
        try:
            pos: int | None = None
            try:
                pos = self._host.position(task_id)
            except Exception:  # noqa: BLE001 - 位次实查失败降级 1
                pos = None
            wait_cb(task_id, pos or 1)
        except Exception:  # noqa: BLE001 - 广播失败不阻断
            pass

    # ── 云道 ────────────────────────────────────────────────────
    def _cloud_dispatcher_loop(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        while True:
            with self._cond:
                task = None
                limit = int(self._h(self.spec.cloud_concurrency_hook)())
                if len(self._cloud_running) < limit:
                    task = self._pop_lane_locked(cloud=True)
                if task is None:
                    self._cond.wait()
                    continue  # 重走外层：重新判定并发空位与队列（防 None 下行）
            task_id = str(task.get("task_id"))
            if self._cloud_pool is None:
                self._cloud_pool = ThreadPoolExecutor(
                    max_workers=_CLOUD_POOL_MAX,
                    thread_name_prefix=f"{self.spec.name.lower()}-cloud")
            with self._cond:
                self._cloud_running[task_id] = task
            self._cloud_pool.submit(self._run_cloud_task, task)

    def _run_cloud_task(self, task: dict) -> None:
        task_id = str(task.get("task_id"))
        try:
            self._host._run_one_cloud(task)
        except Exception as exc:  # noqa: BLE001
            log.error("%s云任务执行异常: %s: %s", self.spec.name, task_id, exc)
        finally:
            with self._cond:
                self._cloud_running.pop(task_id, None)
                self._cancel_flags.discard(task_id)
                self._cond.notify_all()
