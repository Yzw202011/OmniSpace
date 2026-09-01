"""图像生成任务队列（2026-09-02 生图 B 方案：单 worker 顺序消费统一队列）。

对齐 services/video_queue.py 的架构（用户裁定「生图也要这样」）：此前
生图三族并发各自为政——
  1. 绘画页 draw.py 自带派发器，但提交期其他功能（视频/对话）持锁
     直接 40007 拒绝、派发期抢不到锁任务直接报错（不等待）；
  2. 漫剧关键帧/资产图端点持 paint 锁（同名可重入）→ 并发请求双双
     放行后在 PaintEngine._infer_lock 上盲等（「生图没返回」的根因），
     无排队状态、无位次、无取消。

现改为统一单 daemon worker FIFO（priority 高者先，同优先级按提交序）：
  - 所有生图入口（绘画页/关键帧/资产图）一律入队受理；
  - 排空循环持有 "paint" 功能锁（其他功能持锁时等待让位而非拒绝——
    与视频队列 video_gen 互斥经 feature_lock 天然成立）；
  - 每任务开跑前 vLLM 睡眠让渡；队列排空才卸载绘画管线并唤醒 vLLM
    （连续生图接力免换载churn）；
  - 排队位次（position）+ 排队等待广播钩子（wait_progress_cb，供
    漫剧生图按钮显示「排队中·前N」）；
  - submit_and_wait：同步响应端点（关键帧/资产图）在请求内排队等待，
    HTTP 契约不变（原实现本就分钟级阻塞），进度走 WS 广播。

runner 契约同 video_queue：负责终态回写与业务进度；不负责功能锁/
显存协商（本队列统一编排）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

log = logging.getLogger("omnispace.services.image_queue")

_WAIT_POLL_S = 1.0        # 等锁/热保护轮询周期（秒）
_THERMAL_POLL_S = 2.0
_DEFAULT_PRIORITY = 5     # 与 draw 既有语义一致：数值大者先跑


class ImageTaskCancelled(Exception):
    """图像任务取消信号（排队出队与运行中检查点共用）。"""


class ImageTaskQueue:
    """图像任务队列单例：单 worker 顺序消费，持 paint 锁排空。"""

    _instance: ImageTaskQueue | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._queue: deque[dict] = deque()
        self._cond = threading.Condition()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._current: dict | None = None       # 正在运行的任务
        self._cancel_flags: set[str] = set()    # 运行中取消旗标
        self._lock_held = False                 # 本队列当前持有 paint 锁
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reconciled = False

    @classmethod
    def instance(cls) -> ImageTaskQueue:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = ImageTaskQueue()
            return cls._instance

    # ── 对外接口 ────────────────────────────────────────────────

    def submit(self, task: dict) -> int:
        """任务入队（priority 高者先，同优先级 FIFO），返回当前排队位次。

        task 必备键：task_id / kind / runner / loop；
        可选键：priority（int，默认 5，大者先）/ wait_progress_cb
        （排队等待期间周期回调 (task_id, position)）/ label（诊断用）。
        """
        task.setdefault("priority", _DEFAULT_PRIORITY)
        with self._cond:
            self._queue.append(task)
            # 稳定排序：priority 降序，同优先级保持入队序（deque 稳定）
            self._queue = deque(sorted(
                self._queue, key=lambda t: -int(t.get("priority", 5))))
            self._loop = task.get("loop") or self._loop
            position = self._position_locked(str(task.get("task_id")))
            self._cond.notify_all()
        self._ensure_worker()
        log.info("图像任务入队: %s kind=%s 位次=%d pri=%s",
                 task.get("task_id"), task.get("kind"), position,
                 task.get("priority"))
        return position

    async def submit_and_wait(self, task: dict,
                              timeout_s: float = 3600.0) -> Any:
        """同步响应端点的排队等待形态（关键帧/资产图）。

        入队 → 在 API 请求协程内等待完成 → 返回 runner 返回值
        （runner 异常/准入取消原样上抛，HTTP 错误语义与旧实现一致）。
        排队等待期间经 wait_progress_cb 广播「排队中·前N」。

        实现：on_finish 完成钩子 + threading.Event，经
        run_in_executor 阻塞等待（跨线程安全，worker 线程 set）；
        runner 返回值由队列回填 task["_result"]。
        """
        woken = threading.Event()
        box: dict[str, Any] = {"error": None}

        def on_finish(err: BaseException | None) -> None:
            box["error"] = err
            woken.set()

        task = {**task, "on_finish": on_finish}
        self.submit(task)
        loop = asyncio.get_running_loop()
        # 无 timeout 参数的 wait：超时由下方 woken.is_set 判定
        await loop.run_in_executor(None, woken.wait)
        if not woken.is_set():
            raise TimeoutError(f"图像任务等待超时（>{timeout_s:.0f}s）")
        err = box["error"]
        if err is not None:
            raise err
        return task.get("_result")

    def cancel(self, task_id: str) -> str:
        """取消：'queued'（已出队）/ 'running'（置旗标，runner 检查点
        收割——绘画页任务的引擎协作中断旗标由调用方自理）/ 'missing'。"""
        with self._cond:
            for i, t in enumerate(self._queue):
                if str(t.get("task_id")) == task_id:
                    del self._queue[i]
                    log.info("排队图像任务取消出队: %s", task_id)
                    return "queued"
        if self._current is not None \
                and str(self._current.get("task_id")) == task_id:
            self._cancel_flags.add(task_id)
            log.info("运行中图像任务置取消旗标: %s", task_id)
            return "running"
        return "missing"

    def set_priority(self, task_id: str, priority: int) -> bool:
        """调整排队任务优先级并重排（0~9；未在队列返回 False）。"""
        with self._cond:
            for t in self._queue:
                if str(t.get("task_id")) == task_id:
                    t["priority"] = int(priority)
                    self._queue = deque(sorted(
                        self._queue, key=lambda x: -int(x.get("priority", 5))))
                    return True
        return False

    def position(self, task_id: str) -> int | None:
        """排队位次（1 起，按调度序；仅排队中任务有值）。"""
        with self._cond:
            return self._position_locked(task_id)

    def snapshot(self) -> dict:
        """队列状态快照（/paint/queue 与诊断用）。"""
        with self._cond:
            return {
                "queued": [
                    {"task_id": str(t.get("task_id")), "kind": t.get("kind"),
                     "priority": int(t.get("priority", 5))}
                    for t in self._queue],
                "current": (self._current or {}).get("task_id"),
                "lock_held": self._lock_held,
            }

    def is_cancelled(self, task_id: str) -> bool:
        """运行中任务的取消旗标查询（check_cancel 闭包数据源）。"""
        return str(task_id) in self._cancel_flags

    # ── worker ──────────────────────────────────────────────────

    def _position_locked(self, task_id: str) -> int | None:
        for i, t in enumerate(self._queue):
            if str(t.get("task_id")) == task_id:
                return i + 1
        return None

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop, daemon=True, name="image-queue")
            self._worker.start()

    def _worker_loop(self) -> None:
        """消费主循环：取任务 → 准入 → 执行；排空时释放锁并收尾协商。"""
        while True:
            with self._cond:
                while not self._queue:
                    if self._lock_held:
                        self._lock_held = False
                        self._unload_paint_pipeline()
                        self._release_paint_lock(self._loop)
                        self._wake_vllm_after_generation()
                    self._cond.wait()
                task = self._queue.popleft()
                self._current = task
            task_id = str(task.get("task_id"))
            try:
                self._run_one(task)
            except Exception as exc:  # noqa: BLE001 - worker 永不退出
                log.error("图像队列任务异常逃逸: %s: %s", task_id, exc)
            finally:
                with self._cond:
                    if self._current is task:
                        self._current = None
                    self._cancel_flags.discard(task_id)

    def _run_one(self, task: dict) -> None:
        """单任务全流程：准入 → runner；一切结局经 on_finish 钩子回传
        （submit_and_wait 的等待方靠它唤醒，绝不悬挂）。"""
        task_id = str(task.get("task_id"))
        finish = task.get("on_finish")
        err: BaseException | None = None
        try:
            self._wait_admission(task)
        except ImageTaskCancelled as exc:
            log.info("图像任务排队期间取消: %s", task_id)
            err = exc
        except Exception as exc:  # noqa: BLE001 - 准入失败（锁/事件循环等）
            log.error("图像任务准入失败: %s: %s", task_id, exc)
            err = exc
        else:
            log.info("图像任务开跑: %s kind=%s", task_id, task.get("kind"))
            try:
                task["_result"] = task["runner"](
                    task, self._make_check_cancel(task_id))
            except ImageTaskCancelled as exc:
                log.info("图像任务已取消: %s", task_id)
                err = exc
            except Exception as exc:  # noqa: BLE001 - runner 异常兜底记录
                log.error("图像任务执行异常: %s: %s", task_id, exc)
                err = exc
        finally:
            if finish is not None:
                try:
                    finish(err)
                except Exception as exc:  # noqa: BLE001
                    log.warning("on_finish 钩子异常: %s", exc)

    def _make_check_cancel(self, task_id: str) -> Callable[[], None]:
        def check_cancel() -> None:
            if self.is_cancelled(task_id):
                raise ImageTaskCancelled(task_id)
        return check_cancel

    # ── 准入协商（可覆写小方法，测试注入点）─────────────────────

    def _wait_admission(self, task: dict) -> None:
        """热保护等待 → 排队等待广播 → 功能锁等待 → vLLM 让渡。"""
        task_id = str(task.get("task_id"))
        check = self._make_check_cancel(task_id)
        wait_cb = task.get("wait_progress_cb")
        while self._thermal_paused():
            check()
            if wait_cb:
                self._call_wait_cb(wait_cb, task_id)
            time.sleep(_THERMAL_POLL_S)
        if not self._lock_held:
            while True:
                check()
                if wait_cb:
                    self._call_wait_cb(wait_cb, task_id)
                if self._acquire_paint_lock(task_id, task.get("loop")):
                    self._lock_held = True
                    break
                time.sleep(_WAIT_POLL_S)
        else:
            # 队列已持锁（接力）：仍广播一次排队位次供 UI 收敛
            if wait_cb:
                self._call_wait_cb(wait_cb, task_id)
        self._sleep_vllm_for_generation()

    @staticmethod
    def _call_wait_cb(wait_cb: Callable, task_id: str) -> None:
        try:
            pos = ImageTaskQueue.instance().position(task_id)
            wait_cb(task_id, pos or 1)
        except Exception:  # noqa: BLE001 - 广播失败不阻断
            pass

    def _thermal_paused(self) -> bool:
        try:
            from .thermal_guard import get_thermal_guard
            return bool(get_thermal_guard().is_paused())
        except Exception:  # noqa: BLE001
            return False

    def _acquire_paint_lock(self, task_id: str,
                            loop: asyncio.AbstractEventLoop | None) -> bool:
        from ..middleware.feature_lock import get_feature_lock
        if loop is None or loop.is_closed():
            raise RuntimeError("事件循环不可用，无法获取功能锁")
        fut = asyncio.run_coroutine_threadsafe(
            get_feature_lock().acquire("paint", task_id=task_id), loop)
        return bool(fut.result(timeout=10.0))

    def _release_paint_lock(self, loop: asyncio.AbstractEventLoop | None) -> None:
        from ..middleware.feature_lock import get_feature_lock
        try:
            if loop is not None and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    get_feature_lock().release("paint"), loop)
        except Exception as exc:  # noqa: BLE001
            log.warning("paint 锁释放失败: %s", exc)

    def _sleep_vllm_for_generation(self) -> None:
        try:
            from ..engines.vllm_service import get_vllm_service
            get_vllm_service().sleep_for_paint()
        except Exception as exc:  # noqa: BLE001
            log.warning("vLLM 睡眠协商失败（不阻断生图）: %s", exc)

    def _wake_vllm_after_generation(self) -> None:
        try:
            from ..engines.vllm_service import get_vllm_service
            get_vllm_service().wake_from_paint()
        except Exception as exc:  # noqa: BLE001
            log.warning("vLLM 唤醒协商失败（不影响生图结果）: %s", exc)

    def _unload_paint_pipeline(self) -> None:
        try:
            from .inference.paint_engine import get_paint_engine
            get_paint_engine().unload_model()
        except Exception as exc:  # noqa: BLE001
            log.warning("绘画管线卸载失败（不阻断收尾）: %s", exc)


def get_image_queue() -> ImageTaskQueue:
    """获取图像任务队列单例。"""
    return ImageTaskQueue.instance()
