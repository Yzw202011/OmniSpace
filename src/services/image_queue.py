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
  - 每任务开跑前 vLLM 睡眠让渡；队列排空卸载绘画管线（连续生图接力
    免换载 churn）。2026-10-02 用户拍板「按需真实加载」：排空不再
    唤醒 vLLM（防生成间隙反复拉起-被杀的拉锯），对话引擎回到真使用
    时 ensure_loaded；
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
from collections.abc import Callable
from typing import Any

from .task_queue import QueueSpec, TaskQueueCore

log = logging.getLogger("omnispace.services.image_queue")

_WAIT_POLL_S = 1.0        # 等锁/热保护轮询周期（秒）
_THERMAL_POLL_S = 2.0
_DEFAULT_PRIORITY = 5     # 与 draw 既有语义一致：数值大者先跑
# 任务级显存需求档位表（2026-10-02 用户拍板「如实报数」）：kind → GB。
# comic_asset = qwen21 整图直出四视图（TE 6.0G + UNet 6.9G ≈ 13G，
# ComfyUI dynamic staging 实测口径，logs/comfyui_paint.log:4455/4465）；
# 其余 kind 维持 0（现状行为零变化）。
_PAINT_NEED_GB_BY_KIND: dict[str, float] = {"comic_asset": 13.0}


class ImageTaskCancelled(Exception):
    """图像任务取消信号（排队出队与运行中检查点共用）。"""


class ImageTaskQueue:
    """图像任务队列单例：本地单 worker 持锁排空 + 云道并发池。"""

    _instance: ImageTaskQueue | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        # B5 终态（2026-09-17 用户拍板删 legacy）：调度一律委托通用
        # TaskQueueCore（灰度开关与内联实现已删，历史见 git）。本类
        # 保留全部宿主钩子方法，Core 经 host 回调——金标准 Harness 的
        # monkeypatch 手法继续生效。
        self._core = TaskQueueCore(host=self, spec=QueueSpec(
            name="图像", label="图像",
            acquire_hook="_acquire_paint_lock",
            release_hook="_release_paint_lock",
            thermal_hook="_thermal_paused",
            vllm_sleep_hook="_sleep_vllm_for_generation",
            unload_hook="_unload_paint_pipeline",
            # 2026-10-02 用户拍板「按需真实加载」：不接排空唤醒——
            # 生成间隙反复拉起 vLLM 与下一任务杀回形成拉锯（V9-β
            # wake_hook 摘除，对话引擎回归真使用时 ensure_loaded）
            budget_admit_hook="_budget_admit",
            budget_release_hook="_budget_release",
            cloud_concurrency_hook="_cloud_concurrency",
            # 云道执行体走带 busy 登记簿的包装层（保 legacy 语义：
            # 云任务上榜「系统非空闲」）
            cloud_run_hook="_run_cloud_one",
            wait_poll_s=_WAIT_POLL_S, thermal_poll_s=_THERMAL_POLL_S,
            priority_sort=True, default_priority=_DEFAULT_PRIORITY))

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
        return self._core.submit(task)

    async def submit_and_wait(self, task: dict,
                              timeout_s: float = 3600.0) -> Any:
        """同步响应端点的排队等待形态（关键帧/资产图）。

        入队 → 在 API 请求协程内等待完成 → 返回 runner 返回值
        （runner 异常/准入取消原样上抛，HTTP 错误语义与旧实现一致）。
        排队等待期间经 wait_progress_cb 广播「排队中·前N」。
        """
        return await self._core.submit_and_wait(task, timeout_s)

    def cancel(self, task_id: str) -> str:
        """取消：'queued'（已出队）/ 'running'（置旗标，runner 检查点
        收割——绘画页任务的引擎协作中断旗标由调用方自理）/ 'missing'。
        本地道与云端道同源判定（云端任务在轮询间隔收割取消）。"""
        return self._core.cancel(task_id)

    def set_priority(self, task_id: str, priority: int) -> bool:
        """调整排队任务优先级并重排（0~9；未在队列返回 False）。"""
        return self._core.set_priority(task_id, priority)

    def position(self, task_id: str) -> int | None:
        """排队位次（1 起，按调度序；仅排队中任务有值）。"""
        return self._core.position(task_id)

    def snapshot(self) -> dict:
        """队列状态快照（/paint/queue 与诊断用；本地道与云端道合并）。"""
        return self._core.snapshot()

    def is_cancelled(self, task_id: str) -> bool:
        """运行中任务的取消旗标查询（check_cancel 闭包数据源）。"""
        return self._core.is_cancelled(task_id)

    # ── 云端道（批2）────────────────────────────────────────────

    def _cloud_concurrency(self) -> int:
        """云端并发上限（设置钳 1~8；读取失败默认 2）。"""
        try:
            from .cloud_provider_service import get_image_concurrency
            return int(get_image_concurrency())
        except Exception:  # noqa: BLE001 - 设置读取失败按默认
            return 2

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
            log.debug("_cloud_busy_release: 降级忽略", exc_info=True)

    def _run_cloud_one(self, task: dict) -> None:
        """云端道单任务包装（Core 的 cloud_run_hook）：busy 登记簿上榜
        → 执行体 → 下榜（队列态清理由 Core 收尾，宿主不再碰）。"""
        task_id = str(task.get("task_id"))
        busy_token = self._cloud_busy_admit(task)
        try:
            self._run_one_cloud(task)
        except Exception as exc:  # noqa: BLE001 - 云道 worker 永不退出
            log.error("云端图像任务异常逃逸: %s: %s", task_id, exc, exc_info=True)
        finally:
            self._cloud_busy_release(busy_token)

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
            log.error("云端图像任务执行异常: %s: %s", task_id, exc, exc_info=True)
            err = exc
        finally:
            if finish is not None:
                try:
                    finish(err)
                except Exception as exc:  # noqa: BLE001
                    log.warning("on_finish 钩子异常: %s", exc, exc_info=True)

    def _make_check_cancel(self, task_id: str) -> Callable[[], None]:
        def check_cancel() -> None:
            if self.is_cancelled(task_id):
                raise ImageTaskCancelled(task_id)
        return check_cancel

    # ── 准入协商（宿主钩子；本地道编排/准入循环已由 Core 承担）──

    def _budget_admit(self, task: dict) -> None:
        """gpu_budget 顾问判定+登记（批2 need=0 登记式 → 2026-10-02
        升级 kind 档位表如实报数：comic_asset 报 13G）。

        WAIT（额度不足）时做一次增量让位——释放 ComfyUI 自持的上一单
        模型缓存（vLLM 让渡已由 Core 在准入前经 vllm_sleep_hook 统一
        调过，这里的增量动作只有 /free）。全程顾问语义：只让位不拦截
        （绝不因额度拒绝生成，2026-10-02 用户拍板），任何异常不阻断。
        """
        try:
            from .inference.gpu_budget import Verdict, get_gpu_budget
            need = _PAINT_NEED_GB_BY_KIND.get(str(task.get("kind") or ""), 0.0)
            r = get_gpu_budget().request(
                "paint", need_gb=need, note=str(task.get("task_id")),
                advisory=True)
            if need > 0 and r.verdict == Verdict.WAIT:
                self._yield_paint_deficit()
            task["_budget_token"] = r.busy_token
        except Exception as exc:  # noqa: BLE001 - 顾问失败不影响任务
            log.debug("gpu_budget 顾问判定跳过: %s", exc)

    def _yield_paint_deficit(self) -> None:
        """额度不足的增量让位：请 ComfyUI 缓存与进程内对话模型让路。

        实测口径（2026-10-02 logs/backend.log）：①连建角色窗口 admit 帧
        external 9.5~10.9G 多为 ComfyUI 攥着的上一单 qwen21 权重（其
        缓存 ~5min 才自动让）；②14:19 夏倩倩重生成前 6s 对话引擎自动
        降级的 4B（transformers 进程内）占 9.0G → 空闲仅 5.0G →
        qwen21/zviews「需近乎空卡」准入双双被拒回退 klein。vLLM 子
        进程让渡已由 Core 前置钩子统一调过，这里的增量动作是 /free
        与进程内对话模型卸载（paint 锁在手=对话互斥锁必然空闲，无
        在途推理风险）。释放后下一任务冷载权重——时间换空间，用户
        拍板优先不卡死。best-effort：未运行/失败静默跳过。
        """
        try:
            from .inference.comfy_paint_engine import get_comfy_paint_engine
            get_comfy_paint_engine().unload()
            log.info("gpu-budget 额度不足让位：已请求 ComfyUI 释放自持"
                     "模型缓存（POST /free）")
        except Exception as exc:  # noqa: BLE001 - 让位失败不影响任务
            log.debug("ComfyUI 缓存释放跳过: %s", exc)
        try:
            from .inference.dialog_engine import get_dialog_engine
            if get_dialog_engine().unload_model():
                log.info("gpu-budget 额度不足让位：已卸载进程内对话模型"
                         "（对话按需自愈装载，产品铁律兜底）")
        except Exception as exc:  # noqa: BLE001 - 让位失败不影响任务
            log.debug("对话模型让位跳过: %s", exc)

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

    def _thermal_paused(self) -> bool:
        try:
            from .thermal_guard import get_thermal_guard
            if get_thermal_guard().is_paused():
                return True
        except Exception:  # noqa: BLE001 - 探测失败不阻断
            pass
        # 互斥增强（2026-10-05 用户令）：video_gen 本地道（H3 视频链）
        # 占用中 → 本地 paint 任务排队让位，防 RAM/显存双大户叠跑卡死
        # （云道任务不受此限——不占本地卡）
        return self._video_local_busy()

    def _video_local_busy(self) -> bool:
        """video_gen 本地道是否占用中（2026-10-05 互斥增强：H3 视频链
        是 RAM+显存双大户，分镜图与其叠跑曾致 RAM 提交 92% 卡死——
        读 video 队列宿主 _lock_held 旗标，非阻塞）。"""
        try:
            from .video_queue import get_video_queue
            q = get_video_queue()
            return bool(getattr(q, "_lock_held", False))
        except Exception:
            return False

    def _acquire_paint_lock(self, task_id: str,
                            loop: asyncio.AbstractEventLoop | None) -> bool:
        """获取 paint 功能锁（P1-04 修复同 video_queue——超时后取消
        或补偿 release，防计数泄漏假死）。"""
        from ..middleware.feature_lock import get_feature_lock
        if loop is None or loop.is_closed():
            raise RuntimeError("事件循环不可用，无法获取功能锁")
        fut = asyncio.run_coroutine_threadsafe(
            get_feature_lock().acquire("paint", task_id=task_id), loop)
        try:
            return bool(fut.result(timeout=10.0))
        except TimeoutError:
            if fut.cancel():
                return False
            log.warning("paint 锁 acquire 超时且取消失败——补发 release"
                        "（task=%s）", task_id)
            try:
                rel = asyncio.run_coroutine_threadsafe(
                    get_feature_lock().release("paint"), loop)
                rel.result(timeout=5.0)
            except Exception:  # noqa: BLE001
                log.error("paint 迟到 acquire 补偿 release 失败",
                          exc_info=True)
            return False

    def _release_paint_lock(self, loop: asyncio.AbstractEventLoop | None) -> None:
        from ..middleware.feature_lock import get_feature_lock
        try:
            if loop is not None and not loop.is_closed():
                # B0（2026-09-13）：提交后等待结果（10s）——原 fire-and-
                # forget 在 release 静默失败时令 feature_lock 残留持有，
                # 对话排队最长白等 300s（09-13 锁序普查冻结链 #3）。
                fut = asyncio.run_coroutine_threadsafe(
                    get_feature_lock().release("paint"), loop)
                fut.result(timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            log.error("paint 锁释放失败（可能功能锁残留）: %s", exc, exc_info=True)

    def _sleep_vllm_for_generation(self) -> None:
        """vLLM 睡眠让渡（批3 起经 gpu_budget 让渡协调器单源；
        best-effort，协调器内部容错不阻断生图）。"""
        from .inference.gpu_budget import get_yield_coordinator
        get_yield_coordinator().sleep_for_generation("image_queue")

    # 2026-10-02 用户拍板「按需真实加载」：V9-β 排空唤醒三件套
    # （_wake_vllm_after_generation / _local_lane_idle /
    #  _schedule_wake_if_idle）随 wake_hook 接线一并摘除——生成间隙
    # 反复拉起 vLLM 与下一任务的让渡形成拉锯（backend.log:67227 实
    # 弹：唤醒被 RAM 闸拒绝时系统内存仅剩 5.8G）。对话引擎回归真使
    # 用时 ensure_loaded（产品铁律：未加载必须全自动）。

    def _unload_paint_pipeline(self) -> None:
        try:
            from .inference.paint_engine import get_paint_engine
            get_paint_engine().unload_model()
        except Exception as exc:  # noqa: BLE001
            log.warning("绘画管线卸载失败（不阻断收尾）: %s", exc, exc_info=True)


def get_image_queue() -> ImageTaskQueue:
    """获取图像任务队列单例。"""
    return ImageTaskQueue.instance()
# 本项目仅供学习使用，商业授权请+Q 3559331368
