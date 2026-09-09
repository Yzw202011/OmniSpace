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

批2 云端道（云端API接入，2026-09-06）：task 带 "cloud": True 标记的
任务走独立云端道——不 acquire paint 锁、不做热保护/vLLM 让渡、不卸
本地绘画管线（本地显卡零占用的卖点），由云道 dispatcher 派发到并发
线程池（上限 cloud.settings.image_concurrency，默认 2）。排队/取消/
位次语义与本地道同源（同一 deque 与 cancel_flags）。本地道逐比特
不变：无云端任务时行为与改造前完全一致。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .vram_policy import WAKE_DEBOUNCE_S

log = logging.getLogger("omnispace.services.image_queue")

_WAIT_POLL_S = 1.0        # 等锁/热保护轮询周期（秒）
_THERMAL_POLL_S = 2.0
_DEFAULT_PRIORITY = 5     # 与 draw 既有语义一致：数值大者先跑
_CLOUD_POOL_MAX = 8       # 云道线程池硬上限（实际并发由设置钳 1~8）
# 排空→唤醒 vLLM 去抖窗（V9-β 2026-09-09）：客户端逐个提交（基准
# 脚本/用户连点）时任务间隙即排空，旧实现排空即唤醒——唤醒重启会被
# 紧邻的下一个任务撞死（15:37:04 实测：booting 被功能锁门禁拒绝且
# 无人重试，state 卡 unloaded）。到点仍空闲才真唤醒。
# （2026-09-10 批1 搬家 vram_policy 单源，本地名保留为别名）
_WAKE_DEBOUNCE_S = WAKE_DEBOUNCE_S


class ImageTaskCancelled(Exception):
    """图像任务取消信号（排队出队与运行中检查点共用）。"""


class ImageTaskQueue:
    """图像任务队列单例：本地单 worker 持锁排空 + 云道并发池。"""

    _instance: ImageTaskQueue | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._queue: deque[dict] = deque()
        self._cond = threading.Condition()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._current: dict | None = None       # 正在运行的本地任务
        self._cancel_flags: set[str] = set()    # 运行中取消旗标
        self._lock_held = False                 # 本队列当前持有 paint 锁
        self._wake_debounce_s = _WAKE_DEBOUNCE_S  # 实例化便于单测缩短（批3：协调器去抖窗入参）
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reconciled = False
        # 云端道（批2）：dispatcher 线程 + 并发池 + 运行中云端任务表
        self._cloud_worker: threading.Thread | None = None
        self._cloud_pool: ThreadPoolExecutor | None = None
        self._cloud_running: dict[str, dict] = {}

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
        （排队等待期间周期回调 (task_id, position)）/ label（诊断用）/
        cloud（bool，True=云端道：跳过本地准入，见模块 docstring）。
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
        log.info("图像任务入队: %s kind=%s 位次=%d pri=%s cloud=%s",
                 task.get("task_id"), task.get("kind"), position,
                 task.get("priority"), bool(task.get("cloud")))
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
        收割——绘画页任务的引擎协作中断旗标由调用方自理）/ 'missing'。
        本地道与云端道同源判定（云端任务在轮询间隔收割取消）。"""
        with self._cond:
            for i, t in enumerate(self._queue):
                if str(t.get("task_id")) == task_id:
                    del self._queue[i]
                    log.info("排队图像任务取消出队: %s", task_id)
                    return "queued"
        if (self._current is not None
                and str(self._current.get("task_id")) == task_id) \
                or task_id in self._cloud_running:
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
        """队列状态快照（/paint/queue 与诊断用；本地道与云端道合并）。"""
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
        """运行中任务的取消旗标查询（check_cancel 闭包数据源）。"""
        return str(task_id) in self._cancel_flags

    # ── worker ──────────────────────────────────────────────────

    def _position_locked(self, task_id: str) -> int | None:
        for i, t in enumerate(self._queue):
            if str(t.get("task_id")) == task_id:
                return i + 1
        return None

    def _pop_lane_locked(self, cloud: bool) -> dict | None:
        """按道取队首任务（cloud=True 取首个云端任务，否则首个本地任务）。

        跨道任务留在 deque 里等对方 worker 认领（priority 全局排序
        保留；两道消费速率独立——本地单 worker 串行，云端并发池）。
        """
        for i, t in enumerate(self._queue):
            if bool(t.get("cloud")) == cloud:
                del self._queue[i]
                return t
        return None

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                pass
            else:
                self._worker = threading.Thread(
                    target=self._worker_loop, daemon=True, name="image-queue")
                self._worker.start()
            if self._cloud_worker is not None \
                    and self._cloud_worker.is_alive():
                pass
            else:
                self._cloud_worker = threading.Thread(
                    target=self._cloud_dispatcher_loop, daemon=True,
                    name="image-queue-cloud")
                self._cloud_worker.start()

    def _worker_loop(self) -> None:
        """本地道消费主循环：取本地任务 → 准入 → 执行；排空时释放锁并
        收尾协商。云端任务不在此道（云道 dispatcher 独立消费）；仅剩
        云端任务的积压不阻碍本地道释放 paint 锁（云端不需要本地锁）。"""
        while True:
            with self._cond:
                task = None
                while task is None:
                    task = self._pop_lane_locked(cloud=False)
                    if task is None:
                        if self._lock_held:
                            self._lock_held = False
                            self._unload_paint_pipeline()
                            self._release_paint_lock(self._loop)
                            self._schedule_wake_if_idle()
                        self._cond.wait()
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

    # ── 云端道（批2）────────────────────────────────────────────

    def _cloud_concurrency(self) -> int:
        """云端并发上限（设置钳 1~8；读取失败默认 2）。"""
        try:
            from .cloud_provider_service import get_image_concurrency
            return int(get_image_concurrency())
        except Exception:  # noqa: BLE001 - 设置读取失败按默认
            return 2

    def _cloud_dispatcher_loop(self) -> None:
        """云端道派发循环：有空位时取首个云端任务 → 并发池执行。

        与本地道共用 deque/cancel_flags/位次（用户视角一条队列）；
        空位判定在 cond 内做（新任务入队/云任务完成都会 notify）。
        """
        while True:
            with self._cond:
                task = None
                while task is None:
                    if len(self._cloud_running) < self._cloud_concurrency():
                        task = self._pop_lane_locked(cloud=True)
                    if task is None:
                        self._cond.wait()
            task_id = str(task.get("task_id"))
            if self._cloud_pool is None:
                self._cloud_pool = ThreadPoolExecutor(
                    max_workers=_CLOUD_POOL_MAX,
                    thread_name_prefix="image-queue-cloud")
            with self._cond:
                self._cloud_running[task_id] = task
            self._cloud_pool.submit(self._run_cloud_one, task)

    def _cloud_busy_admit(self, task: dict) -> str:
        """云任务上榜（批5，方案 §3.6）：不取本地锁但登记忙碌——空闲
        回收/看门狗/预加载经登记簿看见「系统非空闲」。返回注销令牌。"""
        try:
            from .inference.gpu_budget import get_busy_registry
            token = get_busy_registry().register(
                "paint", "cloud", str(task.get("task_id")))
            log.info("[gpu-budget] paint 云任务上榜: %s（不占本地 GPU，"
                     "系统按非空闲对待）", task.get("task_id"))
            return token
        except Exception:  # noqa: BLE001 - 登记失败不影响云任务
            return ""

    def _cloud_busy_release(self, token: str) -> None:
        """云任务下榜（no-op 安全）。"""
        if not token:
            return
        try:
            from .inference.gpu_budget import get_busy_registry
            get_busy_registry().unregister(token)
        except Exception:  # noqa: BLE001
            pass

    def _run_cloud_one(self, task: dict) -> None:
        """云端道单任务：无本地准入（锁/热保护/vLLM 让渡全跳过），
        直接执行 runner；完成回调与本地道同构（on_finish / 取消旗标）。"""
        task_id = str(task.get("task_id"))
        busy_token = self._cloud_busy_admit(task)
        try:
            self._run_one_cloud(task)
        except Exception as exc:  # noqa: BLE001 - 云道 worker 永不退出
            log.error("云端图像任务异常逃逸: %s: %s", task_id, exc)
        finally:
            self._cloud_busy_release(busy_token)
            with self._cond:
                self._cloud_running.pop(task_id, None)
                self._cancel_flags.discard(task_id)
                self._cond.notify_all()

    def _run_one_cloud(self, task: dict) -> None:
        task_id = str(task.get("task_id"))
        finish = task.get("on_finish")
        err: BaseException | None = None
        log.info("云端图像任务开跑: %s kind=%s", task_id, task.get("kind"))
        try:
            task["_result"] = task["runner"](
                task, self._make_check_cancel(task_id))
        except ImageTaskCancelled as exc:
            log.info("云端图像任务已取消: %s", task_id)
            err = exc
        except Exception as exc:  # noqa: BLE001 - runner 异常兜底记录
            log.error("云端图像任务执行异常: %s: %s", task_id, exc)
            err = exc
        finally:
            if finish is not None:
                try:
                    finish(err)
                except Exception as exc:  # noqa: BLE001
                    log.warning("on_finish 钩子异常: %s", exc)

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
            self._budget_release(task)
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
        # 批2 顾问接入（D3=A）：准入通过后记一帧供需判定 + 忙碌登记，
        # 不拦截不让位（硬闸翻转=批3 实弹验证后）
        self._budget_admit(task)

    def _budget_admit(self, task: dict) -> None:
        """gpu_budget 顾问判定+登记（批2）：need_gb=0 登记式调用，
        任务级显存需求估计批4 接档位表；任何异常不阻断任务。"""
        try:
            from .inference.gpu_budget import get_gpu_budget
            r = get_gpu_budget().request(
                "paint", note=str(task.get("task_id")), advisory=True)
            task["_budget_token"] = r.busy_token
        except Exception as exc:  # noqa: BLE001 - 顾问失败不影响任务
            log.debug("gpu_budget 顾问判定跳过: %s", exc)

    def _budget_release(self, task: dict) -> None:
        """gpu_budget 收尾（批2）：注销忙碌 + 结束帧日志（no-op 安全）。"""
        try:
            from .inference.gpu_budget import get_gpu_budget
            get_gpu_budget().release(
                token=str(task.get("_budget_token") or ""),
                feature="paint",
                note=str(task.get("task_id")))
        except Exception as exc:  # noqa: BLE001 - 收尾失败不影响任务
            log.debug("gpu_budget 收尾跳过: %s", exc)

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
        """vLLM 睡眠让渡（批3 起经 gpu_budget 让渡协调器单源；
        best-effort，协调器内部容错不阻断生图）。"""
        from .inference.gpu_budget import get_yield_coordinator
        get_yield_coordinator().sleep_for_generation("image_queue")

    def _wake_vllm_after_generation(self) -> None:
        try:
            from ..engines.vllm_service import get_vllm_service
            get_vllm_service().wake_from_paint()
        except Exception as exc:  # noqa: BLE001 - 唤醒协商失败不影响生图结果
            log.warning("vLLM 唤醒协商失败（不影响生图结果）: %s", exc)

    def _local_lane_idle(self) -> bool:
        """本地道空闲：无在跑任务且无本地排队（仅剩云端任务不算忙，
        云道不占 GPU）。"""
        with self._cond:
            return (self._current is None and not any(
                not bool(t.get("cloud")) for t in self._queue))

    def _schedule_wake_if_idle(self) -> None:
        """排空后延迟唤醒 vLLM（V9-β 2026-09-09；批3 起判定逻辑转
        gpu_budget 让渡协调器单源——跨队列只认最新排空，video 队列
        同款接入后两侧不再互相撞车）。

        哨兵兼容：debounce 仍读实例属性 _wake_debounce_s（单测缩短
        用）；唤醒动作注入 _wake_vllm_after_generation（V9-β 哨兵
        monkeypatch 点，行为不变）。
        """
        from .inference.gpu_budget import get_yield_coordinator
        get_yield_coordinator().schedule_wake_if_idle(
            "image_queue", self._local_lane_idle,
            debounce_s=self._wake_debounce_s,
            wake=self._wake_vllm_after_generation)

    def _unload_paint_pipeline(self) -> None:
        try:
            from .inference.paint_engine import get_paint_engine
            get_paint_engine().unload_model()
        except Exception as exc:  # noqa: BLE001
            log.warning("绘画管线卸载失败（不阻断收尾）: %s", exc)


def get_image_queue() -> ImageTaskQueue:
    """获取图像任务队列单例。"""
    return ImageTaskQueue.instance()
