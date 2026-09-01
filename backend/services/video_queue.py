"""视频生成任务队列（2026-09-02 B 方案：单 worker 顺序消费的真队列）。

历史问题（2026-08-31 连点事故 + 2026-09-02 用户裁定 B 方案重做）：
此前每个视频生成请求各自起后台线程，跨镜多任务靠 H3 引擎
_gen_lock 阻塞排队——
  1. 排队任务显示「生成中 0%」，无法区分排队与卡死；
  2. 排队任务取消无效（H3 链式采样无取消检查点，取消后仍空跑整镜）；
  3. 镜间接力时上一镜收尾唤醒 vLLM（~12GB 重装载）与下一镜采样
     叠载显存（08-29 蓝屏同类风险）。

现改为：
  - API 只校验 + 落库（status='pending'）+ 入队即返回；
  - 单 daemon worker 顺序消费：准入协商（热保护等待 → 功能锁等待 →
    vLLM 让渡/绘画管线卸载）→ 翻 generating → 跑管线 → 终态回写；
  - 排空循环内持续持有 video_gen 功能锁，队列彻底排空才释放并唤醒
    vLLM——镜间接力无缝接管显存，不再有唤醒叠载窗口；
  - 排队任务取消 = 直接出队；运行中任务经 check_cancel 检查点取消
    （H3 链式采样轮询 3s 一查 + ComfyUI /interrupt 止损）。

线程模型对齐 LoRATrainingService（专属 daemon 线程 + 取消旗标集），
互斥/让渡编排在各可覆写小方法里（测试 monkeypatch 用）。

runner 契约（任务 dict["runner"]，由 api/manga/video.py 注入）：
  runner(task: dict, check_cancel: Callable[[], None]) -> None
  - 阻塞执行一个任务，负责把终态（done/error/cancelled）写回
    video_tasks（经 task["update_status"]）；
  - check_cancel() 须在采样/轮询循环中周期调用，取消时抛
    VideoTaskCancelled（runner 捕获后写 cancelled 终态）；
  - 不负责功能锁/显存协商（本队列统一编排）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable

log = logging.getLogger("omnispace.services.video_queue")

# 排队任务取消检查周期（秒）：等待锁/热保护的循环里以此时长让出
_WAIT_POLL_S = 1.0
# 热保护等待检查周期（秒）
_THERMAL_POLL_S = 2.0


class VideoTaskCancelled(Exception):
    """视频任务取消信号（排队出队与运行中检查点共用）。"""


class VideoTaskQueue:
    """视频任务队列单例：FIFO + 单 worker 顺序消费。

    显存互斥语义：排空循环开始时获取 video_gen 功能锁（其他功能
    持锁时等待让位而非拒绝——「一律受理、排队等跑」），队列排空才
    释放。锁获取/释放为 asyncio 协程，经任务携带的 loop 引用回投
    主事件循环执行。
    """

    _instance: VideoTaskQueue | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._queue: deque[dict] = deque()
        self._cond = threading.Condition()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._cancel_flags: set[str] = set()
        self._current: dict | None = None      # 正在运行的任务
        self._lock_held = False                # 本队列当前持有 video_gen
        self._loop: asyncio.AbstractEventLoop | None = None  # 锁操作回投目标
        self._reconciled = False               # 孤儿任务回收只做一次

    @classmethod
    def instance(cls) -> VideoTaskQueue:
        """进程级单例（双检锁，对齐 LoRATrainingService）。"""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = VideoTaskQueue()
            return cls._instance

    # ── 对外接口 ────────────────────────────────────────────────

    def submit(self, task: dict) -> int:
        """任务入队（FIFO 尾部），返回当前排队位数（1 起，含本任务）。

        task 必备键：task_id / kind / runner / loop / update_status。
        首次入队时回收历史孤儿任务（后端重启遗留的 pending/generating
        行，无 worker 接管则永卡假进度）。
        """
        if not self._reconciled:
            self._reconciled = True
            self._reconcile_orphan_tasks(task)
        with self._cond:
            self._queue.append(task)
            self._loop = task.get("loop") or self._loop
            position = len(self._queue)
            self._cond.notify_all()
        self._ensure_worker()
        log.info("视频任务入队: %s kind=%s 位次=%d",
                 task.get("task_id"), task.get("kind"), position)
        return position

    def cancel(self, task_id: str) -> str:
        """取消任务：'queued'（已出队，调用方写终态）/ 'running'
        （已置旗标，运行中检查点收割）/ 'missing'（不在队列中，走旧
        旗标兜底路径）。"""
        with self._cond:
            for i, t in enumerate(self._queue):
                if t.get("task_id") == task_id:
                    del self._queue[i]
                    log.info("排队任务取消出队: %s", task_id)
                    return "queued"
            if self._current is not None \
                    and self._current.get("task_id") == task_id:
                self._cancel_flags.add(task_id)
                log.info("运行中任务置取消旗标: %s", task_id)
                return "running"
        return "missing"

    def position(self, task_id: str) -> int | None:
        """排队位次（1 起，仅排队中任务有值）。"""
        with self._cond:
            for i, t in enumerate(self._queue):
                if t.get("task_id") == task_id:
                    return i + 1
        return None

    def next_kind(self) -> str | None:
        """下一个排队任务的 kind（无排队返回 None）。

        供 H3 链式引擎决定 keep_loaded：后继同为 h3_chain 时跳过权重
        卸载，镜间接力免整轮重载。
        """
        with self._cond:
            return self._queue[0].get("kind") if self._queue else None

    def snapshot(self) -> dict:
        """队列状态快照（诊断/日志用）。"""
        with self._cond:
            return {
                "queued": len(self._queue),
                "current": (self._current or {}).get("task_id"),
                "kinds": [t.get("kind") for t in self._queue],
                "lock_held": self._lock_held,
            }

    def is_cancelled(self, task_id: str) -> bool:
        """运行中任务的取消旗标查询（check_cancel 闭包数据源）。"""
        return task_id in self._cancel_flags

    # ── worker ──────────────────────────────────────────────────

    def _ensure_worker(self) -> None:
        """懒启动消费线程（已存活则复用）。"""
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop, daemon=True, name="video-queue")
            self._worker.start()

    def _worker_loop(self) -> None:
        """消费主循环：取任务 → 准入 → 执行；队列排空时释放锁并唤醒
        vLLM（显存协商闭环），再等待新任务。"""
        while True:
            with self._cond:
                while not self._queue:
                    if self._lock_held:
                        self._lock_held = False
                        self._release_video_gen(self._loop)
                        self._wake_vllm_after_generation()
                    self._cond.wait()
                task = self._queue.popleft()
                self._current = task
            task_id = str(task.get("task_id"))
            try:
                self._run_one(task)
            except Exception as exc:  # noqa: BLE001 - worker 永不退出
                log.error("视频队列任务异常逃逸: %s: %s", task_id, exc)
            finally:
                with self._cond:
                    if self._current is task:
                        self._current = None
                    self._cancel_flags.discard(task_id)

    def _run_one(self, task: dict) -> None:
        """单个任务全流程：准入协商 → 翻 generating → 跑 runner。"""
        task_id = str(task.get("task_id"))
        update: Callable[[str, dict], None] = task["update_status"]
        # 排队期间被取消：出队时 cancel() 已移除，此分支兜底竞态
        if self.is_cancelled(task_id):
            update(task_id, {"status": "cancelled"})
            return
        try:
            self._wait_admission(task)
        except VideoTaskCancelled:
            update(task_id, {"status": "cancelled"})
            return
        except Exception as exc:  # noqa: BLE001 - 准入失败收敛为任务错误
            log.error("视频任务准入失败: %s: %s", task_id, exc)
            update(task_id, {"status": "error",
                             "error": f"任务准入失败: {exc}"[:500]})
            return
        update(task_id, {"status": "generating", "progress": 0.0})
        log.info("视频任务开跑: %s kind=%s", task_id, task.get("kind"))
        try:
            task["runner"](task, self._make_check_cancel(task_id))
        except VideoTaskCancelled:
            # runner 契约上自写终态；此处兜底防漏写（否则前端轮询永挂）
            log.warning("runner 以取消信号逃逸（兜底写终态）: %s", task_id)
            update(task_id, {"status": "cancelled"})
        except Exception as exc:  # noqa: BLE001 - runner 未捕获的异常兜底
            log.error("视频任务执行异常: %s: %s", task_id, exc)
            update(task_id, {"status": "error",
                             "error": str(exc)[:500]})
        # 终态回写由 runner 负责；锁释放/唤醒由主循环在排空时统一编排

    def _make_check_cancel(self, task_id: str) -> Callable[[], None]:
        """构造 runner 用的取消检查闭包（命中抛 VideoTaskCancelled）。"""
        def check_cancel() -> None:
            if self.is_cancelled(task_id):
                raise VideoTaskCancelled(task_id)
        return check_cancel

    # ── 准入协商（可覆写小方法，测试注入点）─────────────────────

    def _wait_admission(self, task: dict) -> None:
        """任务开跑前准入：热保护等待 → 功能锁等待 → 显存让渡。

        全程响应取消：排队等锁/散热期间被取消的任务不占用 GPU。
        """
        task_id = str(task.get("task_id"))
        # ① 热保护：视频任务分钟级，等待散热优于失败（调度器
        #    ALL_TENSE 强制卸载兜底中断极端长等待）
        while self._thermal_paused():
            self._make_check_cancel(task_id)()
            time.sleep(_THERMAL_POLL_S)
        # ② 功能锁：其他功能（对话/绘画/训练）持锁时等待让位，不拒绝
        if not self._lock_held:
            while True:
                self._make_check_cancel(task_id)()
                if self._acquire_video_gen(task_id, task.get("loop")):
                    self._lock_held = True
                    break
                time.sleep(_WAIT_POLL_S)
        # ③ 显存让渡：持锁后执行，保证与对话/绘画无并发装载
        self._sleep_vllm_for_generation()
        self._unload_paint_pipeline()

    def _thermal_paused(self) -> bool:
        """热保护是否暂停中（探测失败按未暂停放行）。"""
        try:
            from .thermal_guard import get_thermal_guard
            return bool(get_thermal_guard().is_paused())
        except Exception:  # noqa: BLE001
            return False

    def _acquire_video_gen(self, task_id: str,
                           loop: asyncio.AbstractEventLoop | None) -> bool:
        """获取 video_gen 功能锁（回投主事件循环；被其他功能持有时
        返回 False 由调用方重试等待）。"""
        from ..middleware.feature_lock import get_feature_lock
        if loop is None or loop.is_closed():
            raise RuntimeError("事件循环不可用，无法获取功能锁")
        fut = asyncio.run_coroutine_threadsafe(
            get_feature_lock().acquire("video_gen", task_id=task_id), loop)
        return bool(fut.result(timeout=10.0))

    def _release_video_gen(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """释放 video_gen 功能锁（best-effort，回投主事件循环）。"""
        from ..middleware.feature_lock import get_feature_lock
        try:
            if loop is not None and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    get_feature_lock().release("video_gen"), loop)
        except Exception as exc:  # noqa: BLE001
            log.warning("video_gen 锁释放失败: %s", exc)

    def _sleep_vllm_for_generation(self) -> None:
        """vLLM 权重睡眠让渡显存（best-effort，同步直调引擎）。"""
        try:
            from ..engines.vllm_service import get_vllm_service
            get_vllm_service().sleep_for_paint()
        except Exception as exc:  # noqa: BLE001
            log.warning("vLLM 睡眠协商失败（不阻断生成）: %s", exc)

    def _unload_paint_pipeline(self) -> None:
        """卸载本地绘画管线驻留权重（best-effort，幂等）。"""
        try:
            from .inference.paint_engine import get_paint_engine
            get_paint_engine().unload_model()
        except Exception as exc:  # noqa: BLE001
            log.warning("绘画管线卸载失败（不阻断生成）: %s", exc)

    def _wake_vllm_after_generation(self) -> None:
        """队列排空收尾：唤醒 vLLM 恢复对话能力（best-effort；后台
        线程重启装载，不阻塞 worker）。"""
        try:
            from ..engines.vllm_service import get_vllm_service
            get_vllm_service().wake_from_paint()
        except Exception as exc:  # noqa: BLE001
            log.warning("vLLM 唤醒协商失败（不影响生成结果）: %s", exc)

    # ── 孤儿任务回收 ────────────────────────────────────────────

    def _reconcile_orphan_tasks(self, task: dict) -> None:
        """回收历史孤儿任务（后端重启遗留的 pending/generating 行）。

        判据：DB 中 status ∈ {pending, generating} 且 updated_at 距今
        超过 120s（正常任务流转秒级刷新）、且不在本队列与当前执行中。
        回收为 error（前端历史加载只认终态，不回收则永卡假进度）。
        """
        update: Callable[[str, dict], None] = task["update_status"]
        try:
            from ..data.database import get_db_safe
            db = get_db_safe()
            if db is None:
                return
            cutoff = time.time() - 120.0
            live = {str(t.get("task_id")) for t in self._queue}
            if self._current is not None:
                live.add(str(self._current.get("task_id")))
            rows = db.query(
                "SELECT id FROM video_tasks WHERE status IN "
                "('pending','generating') AND updated_at < ?",
                (cutoff,)) or []
            for r in rows:
                tid = str(r.get("id"))
                if tid in live:
                    continue
                update(tid, {"status": "error",
                             "error": "后端重启导致任务中断，请重新发起"})
                log.info("回收孤儿视频任务: %s", tid)
        except Exception as exc:  # noqa: BLE001 - 回收失败不阻断入队
            log.warning("孤儿视频任务回收失败（忽略）: %s", exc)


def get_video_queue() -> VideoTaskQueue:
    """获取视频任务队列单例。"""
    return VideoTaskQueue.instance()
