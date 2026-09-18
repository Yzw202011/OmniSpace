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

批3 云端道（云端API接入，2026-09-06）：task 带 "cloud": True 标记的
任务走独立云端道——不 acquire video_gen 锁、不做热保护/vLLM 让渡/
绘画管线卸载（本地显卡零占用），由云道 dispatcher 派发到并发线程池
（上限 cloud.settings.video_concurrency，默认 1——视频分钟级计费重，
默认串行）。排队/取消/位次语义与本地道同源；next_kind 对云端任务
返回其 kind（非 h3_chain → H3 keep_loaded 正常卸载，零改动）。
本地道逐比特不变。

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
from collections.abc import Callable

from .task_queue import QueueSpec, TaskQueueCore
from .vram_policy import WAKE_DEBOUNCE_S

log = logging.getLogger("omnispace.services.video_queue")


# 排队任务取消检查周期（秒）：等待锁/热保护的循环里以此时长让出
_WAIT_POLL_S = 1.0
# 热保护等待检查周期（秒）
_THERMAL_POLL_S = 2.0


class VideoTaskCancelled(Exception):
    """视频任务取消信号（排队出队与运行中检查点共用）。"""


class VideoTaskQueue:
    """视频任务队列单例：本地 FIFO 单 worker 持锁排空 + 云端道并发池。

    显存互斥语义（本地道）：排空循环开始时获取 video_gen 功能锁（其他
    功能持锁时等待让位而非拒绝——「一律受理、排队等跑」），队列排空才
    释放。锁获取/释放为 asyncio 协程，经任务携带的 loop 引用回投
    主事件循环执行。
    """

    _instance: VideoTaskQueue | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        # B5 终态（2026-09-17 用户拍板删 legacy）：调度一律委托通用
        # TaskQueueCore（灰度开关与内联实现已删，历史见 git）。video
        # 差异：纯 FIFO、排空收尾不卸载绘画管线（卸载在准入时做，
        # unload_hook=None）、snapshot/next_kind 形状适配、宿主编排
        # _run_one（完整状态机经 run_one_hook 逐比特保留）。
        self._core = TaskQueueCore(host=self, spec=QueueSpec(
            name="视频", label="视频",
            acquire_hook="_acquire_video_gen",
            release_hook="_release_video_gen",
            thermal_hook="_thermal_paused",
            vllm_sleep_hook="_sleep_vllm_for_generation",
            unload_hook=None,
            wake_hook="_schedule_wake_if_idle",
            budget_admit_hook="_budget_admit",
            budget_release_hook="_budget_release",
            # 宿主编排：video 任务全流程状态机（经 update_status 的
            # generating/error/cancelled 终态）必须逐比特保留
            run_one_hook="_run_one",
            # 锁旗标代理到宿主字段（宿主 _wait_admission 写自己的
            # _lock_held，Core drain 看同一份状态）
            host_lock_field="_lock_held",
            cloud_run_hook="_run_cloud_one",
            cloud_concurrency_hook="_cloud_concurrency",
            wait_poll_s=_WAIT_POLL_S, thermal_poll_s=_THERMAL_POLL_S,
            priority_sort=False))
        # 宿主侧准入协调状态（host_lock_field 代理目标 + _wait_admission
        # 的读写保护；与 Core 的队列态互不重叠）
        self._cond = threading.Condition()
        self._lock_held = False                # 本队列当前持有 video_gen
        # 排空→唤醒去抖窗（批3 2026-09-10，对齐 image 队列 V9-β）：
        # 实例化便于单测缩短
        self._wake_debounce_s = WAKE_DEBOUNCE_S
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

        task 必备键：task_id / kind / runner / loop / update_status；
        可选键：cloud（bool，True=云端道：跳过本地准入，见模块 docstring）。
        首次入队时回收历史孤儿任务（后端重启遗留的 pending/generating
        行，无 worker 接管则永卡假进度）。
        """
        if not self._reconciled:
            self._reconciled = True
            self._reconcile_orphan_tasks(task)
        return self._core.submit(task)

    def cancel(self, task_id: str) -> str:
        """取消任务：'queued'（已出队，调用方写终态）/ 'running'
        （已置旗标，运行中检查点收割）/ 'missing'（不在队列中，走旧
        旗标兜底路径）。本地道与云端道同源判定。"""
        return self._core.cancel(task_id)

    def position(self, task_id: str) -> int | None:
        """排队位次（1 起，仅排队中任务有值）。"""
        return self._core.position(task_id)

    def next_kind(self) -> str | None:
        """下一个排队任务的 kind（无排队返回 None）。

        供 H3 链式引擎决定 keep_loaded：后继同为 h3_chain 时跳过权重
        卸载，镜间接力免整轮重载。云端任务排在队首时返回 cloud_video
        ——非 h3_chain，H3 正常收尾卸载（正确语义：云端不接力本地权重）。
        """
        queued = self._core.snapshot()["queued"]
        return queued[0]["kind"] if queued else None

    def snapshot(self) -> dict:
        """队列状态快照（诊断/日志用；本地道与云端道合并）。"""
        cs = self._core.snapshot()
        # 形状适配：core 的 queued 是列表，video 对外契约是数量+kinds
        return {
            "queued": len(cs["queued"]),
            "current": cs["current"],
            "kinds": [t.get("kind") for t in cs["queued"]],
            "cloud_running": cs["cloud_running"],
            "lock_held": cs["lock_held"],
        }

    def is_cancelled(self, task_id: str) -> bool:
        """运行中任务的取消旗标查询（check_cancel 闭包数据源）。"""
        return self._core.is_cancelled(task_id)

    # ── 云端道（批3）────────────────────────────────────────────

    def _cloud_concurrency(self) -> int:
        """云端视频并发上限（设置钳 1~4；读取失败默认 1）。"""
        try:
            from .cloud_provider_service import get_video_concurrency
            return int(get_video_concurrency())
        except Exception:  # noqa: BLE001 - 设置读取失败按默认
            return 1

    def _cloud_busy_admit(self, task: dict) -> str:
        """云任务上榜（批5，方案 §3.6）：不取本地锁但登记忙碌——空闲
        回收/看门狗/预加载经登记簿看见「系统非空闲」。返回注销令牌。"""
        try:
            from .inference.gpu_budget import get_busy_registry
            token = get_busy_registry().register(
                "video_gen", "cloud", str(task.get("task_id")))
            log.info("[gpu-budget] video_gen 云任务上榜: %s（不占本地 GPU，"
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
        """云端道单任务包装（Core 的 cloud_run_hook）：busy 登记簿上榜
        → 宿主状态机 _run_one（云端跳过本地准入）→ 下榜（队列态清理
        由 Core 收尾，宿主不再碰）。"""
        task_id = str(task.get("task_id"))
        busy_token = self._cloud_busy_admit(task)
        try:
            self._run_one(task)
        except Exception as exc:  # noqa: BLE001 - 云道 worker 永不退出
            log.error("云端视频任务异常逃逸: %s: %s", task_id, exc, exc_info=True)
        finally:
            self._cloud_busy_release(busy_token)

    def _run_one(self, task: dict) -> None:
        """单个任务全流程：准入协商 → 翻 generating → 跑 runner。

        云端任务经 _run_cloud_one 进入时跳过 _wait_admission（本地
        准入无意义），直接翻 generating 跑 runner。
        """
        task_id = str(task.get("task_id"))
        update: Callable[[str, dict], None] = task["update_status"]
        # 排队期间被取消：出队时 cancel() 已移除，此分支兜底竞态
        if self.is_cancelled(task_id):
            update(task_id, {"status": "cancelled"})
            return
        if not task.get("cloud"):
            try:
                self._wait_admission(task)
            except VideoTaskCancelled:
                update(task_id, {"status": "cancelled"})
                return
            except Exception as exc:  # noqa: BLE001 - 准入失败收敛为任务错误
                log.error("视频任务准入失败: %s: %s", task_id, exc, exc_info=True)
                update(task_id, {"status": "error",
                                 "error": f"任务准入失败: {exc}"[:500]})
                return
        update(task_id, {"status": "generating", "progress": 0.0})
        log.info("视频任务开跑: %s kind=%s cloud=%s", task_id,
                 task.get("kind"), bool(task.get("cloud")))
        try:
            task["runner"](task, self._make_check_cancel(task_id))
        except VideoTaskCancelled:
            # runner 契约上自写终态；此处兜底防漏写（否则前端轮询永挂）
            log.warning("runner 以取消信号逃逸（兜底写终态）: %s", task_id, exc_info=True)
            update(task_id, {"status": "cancelled"})
        except Exception as exc:  # noqa: BLE001 - runner 未捕获的异常兜底
            log.error("视频任务执行异常: %s: %s", task_id, exc, exc_info=True)
            update(task_id, {"status": "error",
                             "error": str(exc)[:500]})
        finally:
            self._budget_release(task)
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
        with self._cond:
            held = self._lock_held
        if not held:
            while True:
                self._make_check_cancel(task_id)()
                if self._acquire_video_gen(task_id, task.get("loop")):
                    with self._cond:
                        self._lock_held = True
                    break
                time.sleep(_WAIT_POLL_S)
        # ③ 显存让渡：持锁后执行，保证与对话/绘画无并发装载
        self._sleep_vllm_for_generation()
        self._unload_paint_pipeline()
        # 批2 顾问接入（D3=A）：准入通过后记一帧供需判定 + 忙碌登记，
        # 不拦截不让位（硬闸翻转=批3 实弹验证后）
        self._budget_admit(task)

    def _budget_admit(self, task: dict) -> None:
        """gpu_budget 顾问判定+登记（批2）：need_gb=0 登记式调用，
        任务级显存需求估计批4 接档位表；任何异常不阻断任务。"""
        try:
            from .inference.gpu_budget import get_gpu_budget
            r = get_gpu_budget().request(
                "video_gen", note=str(task.get("task_id")), advisory=True)
            task["_budget_token"] = r.busy_token
        except Exception as exc:  # noqa: BLE001 - 顾问失败不影响任务
            log.debug("gpu_budget 顾问判定跳过: %s", exc)

    def _budget_release(self, task: dict) -> None:
        """gpu_budget 收尾（批2）：注销忙碌 + 结束帧日志（no-op 安全）。"""
        try:
            from .inference.gpu_budget import get_gpu_budget
            get_gpu_budget().release(
                token=str(task.get("_budget_token") or ""),
                feature="video_gen",
                note=str(task.get("task_id")))
        except Exception as exc:  # noqa: BLE001 - 收尾失败不影响任务
            log.debug("gpu_budget 收尾跳过: %s", exc)

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
        """释放 video_gen 功能锁（回投主事件循环并等待结果）。

        B0（2026-09-13）：由 fire-and-forget 改为等待结果（10s）——
        释放静默失败会令 feature_lock 残留持有，对话排队最长白等
        300s（09-13 锁序普查冻结链 #3）。失败升 error 级日志。"""
        from ..middleware.feature_lock import get_feature_lock
        try:
            if loop is not None and not loop.is_closed():
                fut = asyncio.run_coroutine_threadsafe(
                    get_feature_lock().release("video_gen"), loop)
                fut.result(timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            log.error("video_gen 锁释放失败（可能功能锁残留）: %s", exc, exc_info=True)

    def _sleep_vllm_for_generation(self) -> None:
        """vLLM 权重睡眠让渡显存（批3 起经 gpu_budget 让渡协调器
        单源；best-effort，协调器内部容错不阻断生成）。"""
        from .inference.gpu_budget import get_yield_coordinator
        get_yield_coordinator().sleep_for_generation("video_queue")

    def _unload_paint_pipeline(self) -> None:
        """卸载本地绘画管线驻留权重（best-effort，幂等）。

        W3-C（2026-09-13）：双栈过渡期 comfy 与 legacy 同卸。"""
        try:
            from .inference.comfy_paint_engine import get_comfy_paint_engine
            get_comfy_paint_engine().unload()
        except Exception as exc:  # noqa: BLE001
            log.warning("comfy 绘画栈卸载失败（不阻断生成）: %s", exc, exc_info=True)
        try:
            from .inference.paint_engine import get_paint_engine
            get_paint_engine().unload_model()
        except Exception as exc:  # noqa: BLE001
            log.warning("绘画管线卸载失败（不阻断生成）: %s", exc, exc_info=True)

    def _local_lane_idle(self) -> bool:
        """本地道空闲：无在跑任务且无本地排队（仅剩云端任务不算忙，
        云道不占 GPU）。判定委托 Core（队列态真源在 Core 侧）。"""
        return self._core.local_lane_idle()

    def _schedule_wake_if_idle(self) -> None:
        """排空后去抖唤醒 vLLM（批3 2026-09-10，对齐 image 队列
        V9-β 实测模式，判定经协调器单源——跨队列只认最新排空）。"""
        from .inference.gpu_budget import get_yield_coordinator
        get_yield_coordinator().schedule_wake_if_idle(
            "video_queue", self._local_lane_idle,
            debounce_s=self._wake_debounce_s,
            wake=self._wake_vllm_after_generation)

    def _wake_vllm_after_generation(self) -> None:
        """队列排空收尾：唤醒 vLLM 恢复对话能力（best-effort；后台
        线程重启装载，不阻塞 worker）。仅经去抖调度器调用（批3）。"""
        try:
            from ..engines.vllm_service import get_vllm_service
            get_vllm_service().wake_from_paint()
        except Exception as exc:  # noqa: BLE001
            log.warning("vLLM 唤醒协商失败（不影响生成结果）: %s", exc, exc_info=True)

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
            # 活任务集合改经 Core 快照取（队列态真源在 Core 侧）
            snap = self._core.snapshot()
            live = {str(t.get("task_id")) for t in snap["queued"]}
            if snap.get("current"):
                live.add(str(snap["current"]))
            live.update(str(x) for x in snap.get("cloud_running") or [])
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
            log.warning("孤儿视频任务回收失败（忽略）: %s", exc, exc_info=True)


def get_video_queue() -> VideoTaskQueue:
    """获取视频任务队列单例。"""
    return VideoTaskQueue.instance()
# 本项目仅供学习使用，商业授权请+Q 3559331368
