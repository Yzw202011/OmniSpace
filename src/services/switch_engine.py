"""统一模型切换引擎（ModelSwitchEngine，P0 骨架 + P1 增强 2026-08-25）。

把"换模型"从散落在 dialog/paint 引擎里的同步调用，收敛为一等公民的
异步任务：可查进度、可取消、失败尽力回滚。

状态机::

    pending → planning → unloading → loading → verifying → done
        ↘ cancelled（loading 前即时取消；loading 中 force 善后取消）
                       ↘ 失败 → rollback → failed

设计边界（方案裁定）：
- 编排层不重写引擎加载逻辑——量化布局 / KV 自适应 / 子进程管理零改动
- 显存记账与真实驱逐复用 ModelManager.allocate_memory（ensure_loaded
  内部触发），Plan 阶段的驱逐清单仅为预估展示
- 功能锁以 category 名义持有（dialog/paint），由 API 层 acquire、
  任务线程结束时经绑定的事件循环线程安全释放（跨线程移交）
- WS 进度复用 WsHub 广播器：task_progress / task_complete / task_error
  （module=model_switch，前端全局任务处理天然接收）

P1 增强（2026-08-25）：
- loading 阶段进度细化：vLLM 模型解析 logs/vllm-server.log 阶段信号
  （tqdm shards % / torch.compile / KV cache 就绪）映射 30→88；
  其余模型按 est_seconds 时间插值平滑爬升
- prefetch 预热：PUT /models/select 保存配置且功能锁空闲时后台低优
  提交切换任务；活跃 prefetch 任务可被新任务让位（含功能锁移交
  lock_handover，旧任务终态跳过释放）
- cancel force：vLLM 路径调 vllm_service.stop() 自杀协议真终止
  （start 轮询自查 → ensure_loaded False → cancelled 不回滚）；
  transformers/diffusers 无法安全中断线程 → 标记善后（加载返回后
  立即卸载目标模型）
- 预估改进：内存历史（每模型最近 5 次实测 loading 秒数）优先于
  启发式分档

P2 增强（2026-08-25）：
- video 类目接入：minimax-h3 = ComfyUI 子进程预热（加载=
  _ensure_running 冷启动 + register_external_load 台账登记，
  卸载=POST /free 权重下卡保进程 + 清台账，verify=is_alive）；
  diffusers 视频模型 = allocate_memory 闸门 + video_engine.
  load_model(path, enum) 直载 + 台账记账（ModelManager.
  ensure_loaded 的 video 分支 load_fn(model_id) 单参签名与
  video_engine.load_model(path, enum) 不匹配，不可用）
- 加载耗时历史持久化：system_settings kv（每模型最近 5 次），
  重启自动恢复（P1 进程内存态的延续）
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("omnispace.switch")

# 终态集合（任务归档判定）
_TERMINAL = {"done", "failed", "cancelled"}
# 不可中断阶段（引擎半初始化风险；P1 起 force 模式可善后取消）
_UNCANCELLABLE = {"loading", "verifying"}
# loading 阶段进度区间（plan 后 30 → verify 前 88）
_LOAD_PROGRESS_MIN, _LOAD_PROGRESS_MAX = 30, 88


class SwitchBusyError(Exception):
    """同类切换任务进行中（单飞行互斥）。"""

    def __init__(self, active_task_id: str, category: str) -> None:
        self.active_task_id = active_task_id
        self.category = category
        super().__init__(f"{category} 类别已有切换任务进行中: {active_task_id}")


class _Cancelled(Exception):
    """任务内部取消信号。"""


@dataclass
class SwitchTask:
    """一次模型切换任务的完整快照（线程安全读写经 engine._lock）。"""

    task_id: str
    category: str
    model_id: str
    source_model: str = ""          # 提交时引擎持有的旧模型（快照）
    status: str = "pending"         # pending/planning/unloading/loading/verifying/done/failed/cancelled
    stage: str = "排队中"            # 人读阶段名（随进度更新）
    progress: int = 0               # 0-100
    message: str = ""
    error: str = ""
    plan: dict = field(default_factory=dict)
    rollback_enabled: bool = True
    cancel_requested: bool = False
    # P1：低优预热任务（保存模型配置触发，可被用户任务让位）
    priority: str = "user"          # user / prefetch
    # P1：force 取消请求（loading/verifying 中）：vLLM 立即终止，
    # 其余引擎标记善后（加载返回后卸载目标模型）
    force_cancel_requested: bool = False
    # P1：让位锁移交（被新任务接管功能锁时终态跳过释放）
    lock_handover: bool = False
    rollback_state: str = ""        # ""/none/done/failed
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    done_event: threading.Event = field(default_factory=threading.Event,
                                        repr=False, compare=False)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "category": self.category,
            "model_id": self.model_id,
            "source_model": self.source_model,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "plan": self.plan,
            "rollback_enabled": self.rollback_enabled,
            "cancel_requested": self.cancel_requested,
            "priority": self.priority,
            "force_cancel_requested": self.force_cancel_requested,
            "rollback_state": self.rollback_state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(
                (self.finished_at or time.time()) - self.started_at, 1)
            if self.started_at else 0.0,
        }


# ── vLLM 判定与日志进度解析（P1）──────────────────────────────────

# vLLM 日志路径（vllm_service 单例追加写；监控线程只读任务启动后的新增段）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_VLLM_LOG = _PROJECT_ROOT / "logs" / "vllm-server.log"


def _is_vllm_model(model_id: str) -> bool:
    """是否走 vLLM 子进程后端（加载含权重装载+图编译，实测 ~177s）。

    P1 精确化：读模型目录 config.json 的 quantization_config.
    quant_method ∈ {awq, compressed-tensors}（与 dialog_engine.
    _is_awq_model 同口径——py310 无 autoawq/compressed_tensors 包，
    必经 vLLM 子进程）；读不到时回退 deepseek-r1 前缀启发式。

    2026-09-01：config.json 读取经 ModelManager.resolve_model_path 解析
    （用户导入的外部路径模型同样能路由 vLLM），models/<id> 直读仅兜底。
    """
    cfg_path: Path | None = None
    try:
        from .model_manager import get_model_manager
        resolved = get_model_manager().resolve_model_path(model_id)
        if resolved:
            cand = Path(resolved)
            cand = cand if cand.is_dir() else cand.parent
            cand = cand / "config.json"
            if cand.is_file():
                cfg_path = cand
    except Exception:  # noqa: BLE001 - 判定失败不阻断
        pass
    if cfg_path is None:
        cfg_path = _PROJECT_ROOT / "models" / model_id / "config.json"
    try:
        if cfg_path.is_file():
            with open(cfg_path, encoding="utf-8") as f:
                raw = json.load(f)
            qcfg = raw.get("quantization_config")
            if isinstance(qcfg, dict):
                method = str(qcfg.get("quant_method") or "").lower()
            else:
                method = str(raw.get("quant_method") or "").lower()
            if method in ("awq", "compressed-tensors"):
                return True
    except Exception:  # noqa: BLE001 - 判定失败不阻断
        pass
    return model_id.startswith("deepseek-r1")


# vLLM 启动日志阶段信号 → loading 进度锚点（实测 08-25 deepseek 全程
# 177s 时间轴：权重装载 ~10s / torch.compile ~24s / KV+启动 余量；
# 编译缓存命中后整体大幅缩短，插值兜底自然拉齐）
_VLLM_STAGE_ANCHORS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"Loading weights took"), 52),
    (re.compile(r"Model loading took"), 55),
    (re.compile(r"Dynamo bytecode transform"), 58),
    (re.compile(r"Compiling a graph"), 62),
    (re.compile(r"torch\.compile took"), 70),
    (re.compile(r"Available KV cache memory"), 75),
    (re.compile(r"Maximum concurrency"), 80),
    (re.compile(r"Application startup complete|Uvicorn running"), 84),
]
# tqdm 权重装载进度（vLLM 0.26 格式：Completed | n/m）
_VLLM_SHARDS_RE = re.compile(
    r"Loading safetensors checkpoint shards:\s+(\d+)%")
# shards 段映射区间（35→50）
_SHARDS_BASE, _SHARDS_SPAN = 35, 15


def _parse_vllm_log_progress(offset: int) -> int | None:
    """解析 vllm-server.log 指定偏移后的新增内容，返回进度锚点（30-88）。

    只取各信号最后一次出现；tqdm shards 行按百分比线性插值。
    """
    try:
        size = _VLLM_LOG.stat().st_size
        if size <= offset:
            return None
        with open(_VLLM_LOG, "rb") as f:
            f.seek(offset)
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    best: int | None = None
    for pat, anchor in _VLLM_STAGE_ANCHORS:
        if pat.search(chunk):
            best = anchor  # 锚点单调递增，最后一个命中的即最深阶段
    _m = None
    for _m in _VLLM_SHARDS_RE.finditer(chunk):
        pass  # 循环结束后 _m = 最后一次 shards 百分比
    if _m is not None:
        pct = max(0, min(100, int(_m.group(1))))
        shards_pct = _SHARDS_BASE + int(pct * _SHARDS_SPAN / 100)
        best = max(best or 0, shards_pct)
    return best


# ── 预估启发式（P1：历史统计优先，启发式兜底）──────────────────────


def _estimate_seconds(model_id: str, category: str, evict_count: int) -> tuple[int, str]:
    """预估切换耗时（秒）+ 说明文案。

    分档基准（实测口径）：
    - dialog vLLM: 180s（deepseek-r1 W4A16 实测 177s）
    - dialog transformers: 20s（qwen3-vl-4b bf16 加载 ~5-15s）
    - paint diffusers/flux: 30s（klein-9b quanto 加载实测 21s）
    - paint qwen-image GGUF: 60s（流式布局初始化较慢）
    - video H3: 40s（ComfyUI 冷启动含 torch import 实测 ~20s+余量；
      进程已活时秒级短路）
    - video diffusers: 60s（管线装载 + accelerator 启用）
    - 每个额外驱逐模型 +8s（卸载+CUDA 缓存清理）
    """
    if category == "dialog":
        if _is_vllm_model(model_id):
            return 180 + evict_count * 8, "vLLM 引擎启动（权重装载+图编译 ~3 分钟）"
        return 20 + evict_count * 8, "transformers 加载"
    if category == "vision":  # 绘画引擎（mgr/注册表词汇）
        if model_id.startswith("qwen-image"):
            return 60 + evict_count * 8, "GGUF 流式布局初始化"
        return 30 + evict_count * 8, "diffusers 管线加载"
    if category == "video":
        if _is_h3_model(model_id):
            return 40 + evict_count * 8, "ComfyUI 子进程预热（H3 管线）"
        return 60 + evict_count * 8, "diffusers 视频管线加载"
    return 60 + evict_count * 8, ""


# ── video 类目辅助（P2）──────────────────────────────────────────

_H3_MODEL_ID = "minimax-h3"
# H3 台账登记显存（_EXTRA_KNOWN_MODELS 同口径 13GB；权重物理上
# 驻留 ComfyUI 子进程，本进程 pynvml free 已天然反映其占用）
_H3_VRAM_GB = 13.0


def _is_h3_model(model_id: str) -> bool:
    """是否 MiniMax H3（ComfyUI 子进程管线，无本进程 GPU 装载）。"""
    return model_id == _H3_MODEL_ID


def _current_video_model() -> str:
    """video 引擎当前持有模型：diffusers 管线目录名（model_path
    basename，发现层 key 与注册表 id 同名）；无装载但 H3 已登记
    台账 → minimax-h3；否则空串。

    video_engine.get_status()["model"] 是 VideoModel 枚举 value
    （自动装载链 VideoModel(name) 失败时残留默认值），不能作为
    持有判定的真源——model_path（装载目录）才是。
    """
    from .inference.video_engine import get_video_engine
    from .model_manager import get_model_manager
    try:
        st = get_video_engine().get_status()
        if st.get("loaded") and st.get("model_path"):
            return Path(str(st.get("model_path"))).name
    except Exception:  # noqa: BLE001 - 状态查询失败按无装载处理
        pass
    try:
        if any(m.get("model_id") == _H3_MODEL_ID
               for m in get_model_manager().get_loaded_models()):
            return _H3_MODEL_ID
    except Exception:  # noqa: BLE001
        pass
    return ""


class ModelSwitchEngine:
    """模型切换编排引擎（单例）。

    线程模型：submit() 由 API 层调用（事件循环线程），起独立 daemon
    线程执行；任务字典读写经 RLock 保护；WS 广播线程安全。
    """

    _instance: ModelSwitchEngine | None = None
    _instance_lock = threading.Lock()

    # P2 支持的切换类别（mgr/注册表词汇，与引擎适配器对齐）：
    # dialog=对话引擎，vision=绘画引擎，video=视频引擎（H3 子进程
    # 预热 + diffusers 直载，见 _load_model 的 video 特化）
    VALID_CATEGORIES = {"dialog", "vision", "video"}

    def __init__(self) -> None:
        self._tasks: dict[str, SwitchTask] = {}
        self._lock = threading.RLock()
        self._active: dict[str, str] = {}   # category -> 活跃 task_id
        self._broadcast: Callable[[dict], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # P1：加载耗时历史（model_id → 最近 5 次实测 loading 秒数）；
        # P2：system_settings kv 持久化，首用时惰性恢复
        self._load_history: dict[str, deque[float]] = {}
        self._load_history_restored = False

    @classmethod
    def instance(cls) -> ModelSwitchEngine:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def bind(self, broadcast: Callable[[dict], None] | None,
             loop: asyncio.AbstractEventLoop | None) -> None:
        """main.py lifespan 注入：WS 广播器 + 事件循环（锁跨线程释放用）。"""
        self._broadcast = broadcast
        self._loop = loop

    # ═══════════════════════════════════════════════════════════
    #  对外接口
    # ═══════════════════════════════════════════════════════════

    def submit(self, category: str, model_id: str, *, rollback: bool = True,
               feature: str | None = None,
               priority: str = "user") -> dict:
        """提交切换任务，立即返回任务快照（异步执行）。

        Args:
            category: dialog / vision / video
            model_id: 目标模型（必须已下载）
            rollback: 加载失败时尽力回滚至 source_model
            feature:  功能锁名（由 API 层 acquire 后移交，任务线程
                      结束时释放；None = 调用方自行管理锁）
            priority: user（用户显式切换）/ prefetch（保存配置触发的
                      后台预热，可被任何新任务让位）

        让位协议（P1）：同类活跃任务是 prefetch 且未进入不可取消阶段
        时，置 cancel_requested + lock_handover（其终态跳过功能锁释放，
        锁移交给本任务）→ 等 done_event → 接管提交。

        Raises:
            SwitchBusyError: 同类切换任务进行中（单飞行互斥）
            ValueError: 参数非法 / 模型未下载
        """
        if category not in self.VALID_CATEGORIES:
            raise ValueError(
                f"暂不支持的切换类别: {category}（支持 dialog/vision/video）")
        if priority not in ("user", "prefetch"):
            raise ValueError(f"非法 priority: {priority}")
        from .model_manager import get_model_manager
        mgr = get_model_manager()
        if not mgr.resolve_model_path(model_id):
            raise ValueError(f"模型未下载: {model_id}")

        # 让位判定（锁内读快照，锁外等待——_run 的 finally 也要拿锁）
        yield_task: SwitchTask | None = None
        with self._lock:
            active_id = self._active.get(category)
            if active_id:
                active_task = self._tasks.get(active_id)
                if (active_task is not None
                        and active_task.priority == "prefetch"
                        and active_task.status not in _UNCANCELLABLE
                        and active_task.status not in _TERMINAL):
                    active_task.cancel_requested = True
                    active_task.lock_handover = True
                    yield_task = active_task
                else:
                    raise SwitchBusyError(active_id, category)

        if yield_task is not None:
            logger.info("预热任务让位: %s (%s→%s) <- 新任务 %s",
                        yield_task.task_id, yield_task.source_model,
                        yield_task.model_id, model_id)
            if not yield_task.done_event.wait(30.0):
                raise SwitchBusyError(yield_task.task_id, category)

        with self._lock:
            active_id = self._active.get(category)  # 竞态兜底（等待期间被抢先）
            if active_id:
                raise SwitchBusyError(active_id, category)
            task = SwitchTask(
                task_id=uuid.uuid4().hex[:12],
                category=category,
                model_id=model_id,
                rollback_enabled=rollback,
                priority=priority,
            )
            self._tasks[task.task_id] = task
            self._active[category] = task.task_id

        # 快照 source（引擎当前持有模型，提交后不变——功能锁已互斥生成任务）
        if category == "video":
            # video 特化（P2）：diffusers 装载目录名 / H3 台账登记态；
            # get_status()["model"] 是枚举 value（含默认残留），不可靠
            task.source_model = _current_video_model()
        else:
            engine_obj = self._engine_for(category)
            if engine_obj is not None:
                try:
                    task.source_model = engine_obj.get_status().get("model") or ""
                except Exception:  # noqa: BLE001 - 状态查询失败不阻断提交
                    task.source_model = ""

        tag = "后台预热" if priority == "prefetch" else "切换"
        logger.info("模型%s任务已提交: %s → %s (%s, task=%s)",
                    tag, task.source_model or "(空)", model_id, category,
                    task.task_id)
        threading.Thread(
            target=self._run, args=(task, feature),
            daemon=True, name=f"switch-{category}-{task.task_id}",
        ).start()
        return task.to_dict()

    def submit_and_wait(self, category: str, model_id: str,
                        timeout: float = 600.0, *,
                        rollback: bool = True,
                        feature: str | None = None) -> dict:
        """同步语义提交（/models/load 兼容改道用）：阻塞至终态或超时。"""
        info = self.submit(category, model_id, rollback=rollback,
                           feature=feature)
        task_id = info["task_id"]
        with self._lock:
            task = self._tasks[task_id]
        if not task.done_event.wait(timeout):
            task.message = f"等待超时（{timeout:.0f}s），任务仍在后台执行"
            return task.to_dict()
        return task.to_dict()

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    def list_tasks(self, limit: int = 20) -> list[dict]:
        """最近任务列表（新在前），供前端任务面板/排查用。"""
        with self._lock:
            items = sorted(
                self._tasks.values(),
                key=lambda t: t.created_at, reverse=True)[:limit]
            return [t.to_dict() for t in items]

    def get_active_task(self, category: str) -> dict | None:
        """某类别当前活跃任务快照（无则 None）——prefetch 让位判定用。"""
        with self._lock:
            tid = self._active.get(category)
            task = self._tasks.get(tid) if tid else None
            return task.to_dict() if task else None

    def cancel(self, task_id: str, *, force: bool = False) -> tuple[bool, str]:
        """请求取消。

        取消语义（P1）：
        - pending/planning：立即取消（尚未触碰 GPU）
        - unloading：可取消（旧模型已卸但可按需恢复，不强行回滚）
        - loading/verifying + force=False：拒绝——引擎半初始化状态
          （vLLM 子进程装载中 kill 有端口残留/显存泄漏风险），如实
          告知等待完成
        - loading/verifying + force=True（用户最高权威）：
          * vLLM 模型：调 vllm_service.stop() 自杀协议真终止——
            start() 轮询循环自查 _cancel_requested → _kill_locked
            回收子进程 → ensure_loaded False → 任务转 cancelled
            （不回滚，显存已随进程销毁释放）
          * transformers/diffusers：from_pretrained 是不可安全中断的
            阻塞调用——标记 force_cancel_requested 善后：加载返回后
            任务线程立即卸载目标模型并转 cancelled（如实告知等待）
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False, "任务不存在"
            if task.status in _TERMINAL:
                return False, f"任务已结束（{task.status}）"
            if task.status in _UNCANCELLABLE:
                if not force:
                    return False, ("模型加载中不可中断（引擎半初始化风险），"
                                   "请等待完成；如需强制终止可带 force=true "
                                   "（vLLM 立即终止，其余引擎加载完成后卸载）")
                task.force_cancel_requested = True
            else:
                task.cancel_requested = True
        # vLLM 真终止（锁外调用——stop 会拿 vllm_service._lock）。
        # 仅不可取消阶段执行：此时 vLLM 正被本任务启动中，杀的是
        # 半启动进程；若任务尚在 planning（可取消路径），vLLM 若在
        # 跑则是旧模型的服务，绝不能误杀。
        if (force and task.status in _UNCANCELLABLE
                and _is_vllm_model(task.model_id)
                and task.category == "dialog"):
            try:
                from ..engines.vllm_service import get_vllm_service
                get_vllm_service().stop()
                logger.info("切换任务 force 取消: %s（vLLM 子进程已终止）",
                            task_id)
                return True, "已强制终止 vLLM 引擎，任务即将转为已取消"
            except Exception as exc:  # noqa: BLE001 - stop 失败仍保留标记善后
                logger.warning("vLLM 强杀失败（转标记善后）: %s", exc)
        logger.info("切换任务取消请求: %s (%s, force=%s)",
                    task_id, task.status, force)
        if force:
            return True, ("已请求强制取消（引擎加载调用不可中断，"
                          "加载完成后将立即卸载目标模型）")
        return True, "已请求取消"

    # ═══════════════════════════════════════════════════════════
    #  执行主体（后台线程）
    # ═══════════════════════════════════════════════════════════

    def _run(self, task: SwitchTask, feature: str | None) -> None:
        task.started_at = time.time()
        unloaded_source = False
        try:
            # ① Plan：显存账 + 驱逐模拟 + 预估耗时
            self._set_stage(task, "planning", "评估显存与驱逐计划", 5)
            task.plan = self._build_plan(task)
            self._emit(task)
            if task.cancel_requested:
                raise _Cancelled()
            if not task.plan.get("fits"):
                raise RuntimeError(task.plan.get("plan_note") or "显存不足")

            # ② 幂等短路：目标已是当前模型
            if task.source_model == task.model_id:
                ok, err = self._verify(task)
                if not ok:
                    raise RuntimeError(f"目标模型已加载但验证失败: {err}")
                task.message = "目标模型已是当前加载状态（幂等）"
                task.status, task.stage, task.progress = "done", "完成", 100
                return

            # ③ Unload：同类旧模型（引擎持有一致性）
            if task.source_model:
                self._set_stage(task, "unloading",
                                f"卸载 {task.source_model}", 15)
                self._unload_model(task.category, task.source_model)
                unloaded_source = True
                if task.cancel_requested:
                    raise _Cancelled()

            # ④ Load：video 类别走特化适配（H3 预热/diffusers 直载），
            # 其经 ModelManager.ensure_loaded 通用链（内含
            # allocate_memory 真实驱逐）。监控线程细化 loading 进度：
            # vLLM 解析日志阶段信号，其余按 est_seconds 时间插值
            # （30→88 平滑爬升）
            self._set_stage(task, "loading",
                            f"加载 {task.model_id}", _LOAD_PROGRESS_MIN)
            t_load0 = time.time()
            stop_monitor = threading.Event()
            monitor = threading.Thread(
                target=self._monitor_loading, args=(task, stop_monitor),
                daemon=True, name=f"switch-monitor-{task.task_id}")
            monitor.start()
            try:
                loaded = self._load_model(task.category, task.model_id)
            finally:
                stop_monitor.set()
                monitor.join(timeout=3.0)
            loading_s = time.time() - t_load0

            # force 善后（P1）：加载期间用户强制取消——加载调用已返回，
            # 立即卸载目标模型回收显存（不回滚：用户意图是终止）
            if task.force_cancel_requested:
                try:
                    self._unload_model(task.category, task.model_id)
                except Exception:  # noqa: BLE001 - 善后失败如实入消息
                    logger.warning("force 善后卸载失败: %s", task.model_id,
                                   exc_info=True)
                task.status, task.stage = "cancelled", "已取消"
                task.progress, task.message = 100, (
                    f"用户强制取消，已卸载目标模型 {task.model_id}（加载耗时 "
                    f"{loading_s:.0f}s）")
                return

            # ⑤ Verify：引擎状态 == 目标模型且 ready
            self._set_stage(task, "verifying", "验证引擎就绪", 90)
            if not loaded:
                raise RuntimeError(
                    self._load_error(task.category, task.model_id))
            ok, err = self._verify(task)
            if not ok:
                raise RuntimeError(f"加载后验证失败: {err}")

            # 成功：记录实测加载耗时（P1 历史预估数据源）
            self._record_load_history(task.model_id, loading_s)
            task.status, task.stage, task.progress = "done", "完成", 100
            task.message = task.plan.get("est_note") or "切换完成"
            if task.priority == "prefetch":
                task.message = f"后台预热完成（{loading_s:.0f}s）"
        except _Cancelled:
            task.status, task.stage, task.progress = "cancelled", "已取消", 100
            if task.lock_handover:
                task.message = "预热任务让位给新切换请求"
            else:
                task.message = ("用户取消（loading 前阶段）"
                                + ("（预热）" if task.priority == "prefetch" else ""))
        except Exception as exc:  # noqa: BLE001 - 统一失败路径
            # force 取消引发的加载失败 → cancelled 语义（不回滚：
            # vLLM 进程已杀显存已回收；transformers 路径同样不回滚，
            # 保持释放态由用户决定下一步）
            if task.force_cancel_requested:
                task.status, task.stage = "cancelled", "已取消"
                task.progress, task.message = 100, (
                    f"用户强制取消：{exc}（引擎已终止，显存已回收）")
                return
            task.status, task.error = "failed", str(exc)
            task.stage = "失败"
            logger.warning("切换任务失败 (%s→%s): %s",
                           task.source_model, task.model_id, exc)
            # 仅真正卸载过 source 才回滚（2026-08-25 实测踩坑：plan
            # 阶段 fits=False 等未动状态的失败，source 仍在装——回滚
            # 会防御性卸载目标 no-op 后重载 source，无谓的 allocate/
            # 驱逐污染当前状态）
            if unloaded_source:
                self._rollback(task)
            else:
                task.rollback_state = "none"
        finally:
            task.finished_at = time.time()
            with self._lock:
                if self._active.get(task.category) == task.task_id:
                    self._active.pop(task.category, None)
                task.done_event.set()
            if feature and not task.lock_handover:
                self._release_feature_lock(feature)
            self._emit_final(task)
            logger.info("切换任务终态: %s %s→%s status=%s duration=%.1fs",
                        task.task_id, task.source_model or "(空)",
                        task.model_id, task.status,
                        task.finished_at - task.started_at)

    # ── 加载 / 卸载统一入口（P2：video 特化分派）──────────────

    def _load_model(self, category: str, model_id: str) -> bool:
        """加载目标模型：video 走特化适配，其余走 ModelManager
        ensure_loaded 通用链（内含 allocate_memory 真实驱逐）。

        video 特化原因：ModelManager.ensure_loaded 的引擎调用契约
        是 load_fn(model_id) 单参，而 video_engine.load_model 的
        签名是 (model_path, model, device)——通用链对 video 类别
        不可用（from_pretrained(model_id) 必然失败）；且 H3 根本
        不经 video_engine（ComfyUI 子进程管线）。
        """
        from .model_manager import get_model_manager
        if category != "video":
            return get_model_manager().ensure_loaded(category, model_id)
        return self._load_video(model_id)

    def _load_video(self, model_id: str) -> bool:
        """video 类别加载（P2）。

        - minimax-h3：ComfyUI 子进程冷启动预热（_ensure_running，
          实测 ~20s 含 torch import；进程已活秒级通过）——权重
          按工作流引用按需上卡，本进程无 GPU 装载。成功后
          register_external_load 登记台账（available 端点 ready 态
          + scheduler 驱逐语义）。
        - diffusers 布局（wan22-ti2v-5b 等）：allocate_memory 显存
          闸门 → video_engine.load_model(path, enum) 直载 →
          register_external_load 记账；失败回滚预留量（对称）。

        Raises:
            RuntimeError: 管线未就绪 / 未下载 / 显存不足 / 引擎加载失败
        """
        from .model_manager import get_model_manager
        mgr = get_model_manager()

        if _is_h3_model(model_id):
            from .inference.h3_engine import get_h3_engine, h3_available
            if not h3_available():
                raise RuntimeError(
                    "MiniMax H3 管线未就绪（ComfyUI 或权重缺失，"
                    "请确认 tools/ComfyUI_windows_portable 与 "
                    "models/video_gen/h3 权重完整）")
            # 冷启动预热（同进程调用私有方法：编排层不重写引擎逻辑）
            get_h3_engine()._ensure_running(None)
            mgr.register_external_load(
                "video", model_id,
                str(mgr.resolve_model_path(model_id) or ""), _H3_VRAM_GB)
            return True

        # diffusers 视频模型直载
        path = mgr.resolve_model_path(model_id)
        if path is None:
            raise RuntimeError(f"模型未下载: {model_id}")
        required = mgr.estimate_vram_gb(model_id, "video")
        if not mgr.allocate_memory(required):
            raise RuntimeError(
                mgr.last_error or f"显存不足: 需 {required:.1f}GB")
        from .inference.video_engine import get_video_engine
        try:
            from ..data.models import VideoModel
            try:
                enum = VideoModel(model_id)
            except ValueError:
                enum = VideoModel.COGVIDEOX_2B  # 未登记目录名：默认枚举
            ok = get_video_engine().load_model(path, enum)
        except Exception as exc:  # noqa: BLE001
            mgr.rollback_allocation(required)
            raise RuntimeError(f"视频引擎加载失败: {exc}") from exc
        if not ok:
            mgr.rollback_allocation(required)
            raise RuntimeError(f"视频引擎加载失败: {model_id}")
        # 记账转换（2026-08-25 e2e 实测双重记账坑）：allocate 的临时
        # 预留就是台账条目的预留（ensure_loaded 通用链同语义——加载
        # 成功直接写台账不再加 reserved）。register_external_load 会
        # 再 +reserved，不先撤临时预留会把一个模型记两遍账（实测
        # ltx 12GB 装载后 reserved=24，驱逐后残留 12GB 假显存占用）
        mgr.rollback_allocation(required)
        mgr.register_external_load("video", model_id, path, required)
        return True

    def _unload_model(self, category: str, model_id: str) -> None:
        """卸载统一入口：H3 先 POST /free 下卡权重（ComfyUI 进程
        保留热启动）再清台账；其余走 ModelManager.unload_model
        （video diffusers 条目会回调 video_engine.unload_model 引擎
        自定义契约，幂等无害）。"""
        from .model_manager import get_model_manager
        if category == "video" and _is_h3_model(model_id):
            try:
                from .inference.h3_engine import get_h3_engine
                get_h3_engine().unload()
            except Exception as exc:  # noqa: BLE001 - /free 失败不阻断
                logger.debug("H3 权重卸载跳过: %s", exc)
            # 引擎切换驱逐 = 腾地方给新引擎：/free 之外直接杀 ComfyUI
            # 进程（CUDA context + torch 常驻一并释放；冷启动 ~40s，
            # 2026-08-31 抢占治理）。外部手动起的实例不受影响。
            try:
                from .inference.comfy_proc import get_comfy_proc
                get_comfy_proc().shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.debug("ComfyUI 进程终止跳过: %s", exc)
            get_model_manager().unload_model(model_id)
            return
        get_model_manager().unload_model(model_id)

    @staticmethod
    def _load_error(category: str, model_id: str) -> str:
        """加载返回 False 时的错误文案（mgr.last_error 优先）。"""
        from .model_manager import get_model_manager
        if category == "video":
            return f"视频引擎加载失败: {model_id}"
        return get_model_manager().last_error or "加载失败"

    def holds_model(self, category: str, model_id: str) -> bool:
        """引擎当前是否已持有目标模型（prefetch 幂等判定用）。

        video 特化：diffusers 装载目录名 / H3 台账登记态
        （get_status()["model"] 枚举 value 含默认残留，不可靠）。
        """
        try:
            if category == "video":
                return _current_video_model() == model_id
            engine_obj = self._engine_for(category)
            if engine_obj is None:
                return False
            return engine_obj.get_status().get("model") == model_id
        except Exception:  # noqa: BLE001 - 状态查询失败按未持有
            return False

    # ── loading 进度监控（P1）──────────────────────────────────

    def _monitor_loading(self, task: SwitchTask,
                         stop_event: threading.Event) -> None:
        """loading 阶段进度细化（daemon 线程，主线程 ensure_loaded 返回后停）。

        - vLLM 模型：解析 vllm-server.log 任务启动后的新增日志段，
          阶段信号（tqdm shards / torch.compile / KV cache）映射
          30→88；解析无信号时回退时间插值
        - 其余模型：按 est_seconds 时间插值平滑爬升（封顶 88，
          verify 阶段由主线程跳 90）

        进度只增不减（max 保护）；task 简单字段赋值 GIL 原子，
        _emit 线程安全。
        """
        est = max(1.0, float(task.plan.get("est_seconds", 60) or 60))
        t0 = time.time()
        use_vllm = (_is_vllm_model(task.model_id)
                    and task.category == "dialog")
        log_offset = 0
        if use_vllm:
            try:
                log_offset = _VLLM_LOG.stat().st_size
            except OSError:
                use_vllm = False
        while not stop_event.wait(2.0):
            if task.status != "loading" or task.force_cancel_requested:
                return
            pct: int | None = None
            if use_vllm:
                pct = _parse_vllm_log_progress(log_offset)
            if pct is None:
                # 时间插值兜底（非 vLLM / 尚无日志信号）
                elapsed = time.time() - t0
                pct = _LOAD_PROGRESS_MIN + int(
                    (_LOAD_PROGRESS_MAX - _LOAD_PROGRESS_MIN)
                    * min(1.0, elapsed / est))
            pct = max(_LOAD_PROGRESS_MIN,
                      min(_LOAD_PROGRESS_MAX, pct))
            if pct > task.progress:
                task.progress = pct
                self._emit(task)

    def _record_load_history(self, model_id: str, seconds: float) -> None:
        """记录实测加载耗时（最近 5 次），供后续预估优先采用。

        P2：追加后落库 system_settings（跨重启保留）。
        """
        if seconds <= 0:
            return
        with self._lock:
            self._restore_load_history()
            hist = self._load_history.setdefault(
                model_id, deque(maxlen=5))
            hist.append(seconds)
            self._persist_load_history()

    def _estimate_seconds_from_history(
            self, model_id: str) -> tuple[int, str] | None:
        """历史均值预估（有 ≥1 次实测记录时优先于启发式）。"""
        with self._lock:
            self._restore_load_history()
            hist = self._load_history.get(model_id)
            if not hist:
                return None
            avg = sum(hist) / len(hist)
            return int(round(avg)), f"按最近 {len(hist)} 次实测均值预估"

    # 加载耗时历史的 system_settings kv key（P2 持久化）
    _LOAD_HISTORY_KEY = "model_switch.load_history"

    def _restore_load_history(self) -> None:
        """惰性从 system_settings 恢复历史（首用时一次）。"""
        if self._load_history_restored:
            return
        self._load_history_restored = True
        try:
            from ..data.database import get_db_safe
            db = get_db_safe()
            if db is None:
                return
            row = db.query_one(
                "SELECT value FROM system_settings WHERE key=?",
                (self._LOAD_HISTORY_KEY,))
            if not row:
                return
            raw = json.loads(row["value"])
            if not isinstance(raw, dict):
                return
            for mid, vals in raw.items():
                if isinstance(vals, list):
                    self._load_history[mid] = deque(
                        (float(v) for v in vals
                         if isinstance(v, (int, float))), maxlen=5)
            logger.info("加载耗时历史已恢复: %d 个模型",
                        len(self._load_history))
        except Exception:  # noqa: BLE001 - 历史缺失/损坏不阻断
            logger.debug("加载耗时历史恢复跳过", exc_info=True)

    def _persist_load_history(self) -> None:
        """历史落库（UPSERT；失败不影响任务，下次成功时重写）。"""
        try:
            from ..data.database import get_db_safe
            db = get_db_safe()
            if db is None:
                return
            payload = {mid: [round(x, 1) for x in vals]
                       for mid, vals in self._load_history.items() if vals}
            db.sql(
                "INSERT INTO system_settings (key, value, updated_at)"
                " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
                " value=excluded.value, updated_at=excluded.updated_at",
                (self._LOAD_HISTORY_KEY,
                 json.dumps(payload, ensure_ascii=False), time.time()))
        except Exception:  # noqa: BLE001
            logger.debug("加载耗时历史落库跳过", exc_info=True)

    def _rollback(self, task: SwitchTask) -> None:
        """失败回滚：尽力重载 source_model（vLLM 回滚即再等一次全量加载）。

        P1 完善：回滚前防御性清理目标模型残留——ensure_loaded 失败
        路径台账原子不写入，但引擎侧可能残留半初始化对象（load_model
        中途异常），先 unload_model（幂等：台账无条目即 no-op）确保
        干净基线再重载旧模型。
        """
        if (not task.rollback_enabled or not task.source_model
                or task.source_model == task.model_id):
            task.rollback_state = "none"
            return
        task.stage = "回滚中"
        self._emit(task)
        # 防御性清理目标残留（幂等；video 类别含 H3 /free 路径）
        try:
            self._unload_model(task.category, task.model_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("回滚前目标残留清理跳过: %s", exc)
        try:
            ok = self._load_model(task.category, task.source_model)
            task.rollback_state = "done" if ok else "failed"
            if ok:
                task.message = f"已回滚至 {task.source_model}"
            else:
                task.message = f"回滚至 {task.source_model} 失败（显存已释放，可手动加载）"
        except Exception as exc:  # noqa: BLE001
            task.rollback_state = "failed"
            task.message = f"回滚异常: {exc}（显存已释放，可手动加载）"
        logger.info("切换回滚: task=%s state=%s", task.task_id, task.rollback_state)

    # ── Plan ───────────────────────────────────────────────────

    def _build_plan(self, task: SwitchTask) -> dict:
        """切换前算账：显存需求/驱逐清单/预估耗时（展示性预估）。

        真实驱逐由 ensure_loaded → allocate_memory 按记账优先级执行；
        本清单仅为预估展示（同类旧模型最先，其余按显存降序）。
        """
        from .model_manager import get_model_manager
        mgr = get_model_manager()
        required = mgr.estimate_vram_gb(task.model_id, task.category)
        gpu = mgr.get_gpu_status()
        total = float(gpu.get("vram_total_gb", 0.0))
        free = float(gpu.get("vram_free_gb", 0.0))
        loaded = mgr.get_loaded_models()

        # 幂等短路（2026-08-25 e2e 实测踩坑）：目标已在台账 = 无需
        # 加载/驱逐/显存——若按"新加载"算账会把已占显存的目标自身
        # 误判 fits=False（实测 flux2-klein-9b 已加载时 free 仅 4.8GB
        # < required 10.5GB 直接失败）
        if any(m.get("model_id") == task.model_id for m in loaded):
            return {
                "target": task.model_id,
                "category": task.category,
                "vram_required_gb": round(required, 1),
                "vram_total_gb": round(total, 1),
                "vram_free_gb": round(free, 1),
                "evict": [],
                "fits": True,
                "plan_note": "",
                "est_seconds": 0,
                "est_note": "目标已是当前加载模型（幂等）",
            }

        # H3 特化（2026-08-25 e2e D1 实测）：权重驻留 ComfyUI 子进程
        # 按工作流引用按需上卡（DynamicVRAM 分时换载，采样期峰值
        # ~12GB 由生成期调度腾挪）——切换时刻本进程无 GPU 装载，
        # 13GB 名义值仅作台账展示，plan 不据此算账（实测 ltx 台账
        # 8GB + free 4.3GB 凑不齐 13GB 误判 fits=False）。fits 以
        # 管线就绪（ComfyUI + 权重四件齐全）判定，evict 置空。
        if _is_h3_model(task.model_id):
            from .inference.h3_engine import h3_available
            est = self._estimate_seconds_from_history(task.model_id)
            if est is None:
                est = _estimate_seconds(task.model_id, task.category, 0)
            est_s, est_note = est
            fits = h3_available()
            return {
                "target": task.model_id,
                "category": task.category,
                "vram_required_gb": round(required, 1),
                "vram_total_gb": round(total, 1),
                "vram_free_gb": round(free, 1),
                "evict": [],
                "fits": fits,
                "plan_note": "" if fits else
                    "MiniMax H3 管线未就绪（ComfyUI 或权重缺失）",
                "est_seconds": est_s,
                "est_note": est_note,
            }

        evict: list[dict] = []
        acc = free
        # 同类旧模型最先卸（引擎持有一致性要求）
        for m in sorted(
            loaded,
            key=lambda x: (x.get("model_id") != task.source_model,
                           -float(x.get("vram_gb", 0.0))),
        ):
            if acc >= required:
                break
            if m.get("model_id") == task.model_id:
                continue
            evict.append({
                "model_id": m.get("model_id", ""),
                "category": m.get("category", ""),
                "vram_gb": round(float(m.get("vram_gb", 0.0)), 1),
            })
            acc += float(m.get("vram_gb", 0.0))

        est = self._estimate_seconds_from_history(task.model_id)
        if est is not None:
            est_s, est_note = est
            est_s += len(evict) * 8
        else:
            est_s, est_note = _estimate_seconds(
                task.model_id, task.category, len(evict))
        fits = required <= total and acc >= required
        plan_note = ""
        if required > total:
            plan_note = (f"显存不足：{task.model_id} 需 {required:.1f}GB，"
                         f"超过总显存 {total:.1f}GB（物理装不下）")
        elif acc < required:
            plan_note = (f"显存不足：需 {required:.1f}GB，"
                         f"驱逐全部可卸模型后仅 {acc:.1f}GB 可用")
        return {
            "target": task.model_id,
            "category": task.category,
            "vram_required_gb": round(required, 1),
            "vram_total_gb": round(total, 1),
            "vram_free_gb": round(free, 1),
            "evict": evict,
            "fits": fits,
            "plan_note": plan_note,
            "est_seconds": est_s,
            "est_note": est_note,
        }

    # ── 验证 / 引擎解析 ────────────────────────────────────────

    @staticmethod
    def _engine_for(category: str) -> Any:
        """引擎单例适配器：dialog/vision(绘画)/video。"""
        if category == "dialog":
            from .inference.dialog_engine import get_dialog_engine
            return get_dialog_engine()
        if category == "vision":
            from .inference.paint_engine import get_paint_engine
            return get_paint_engine()
        if category == "video":
            from .inference.video_engine import get_video_engine
            return get_video_engine()
        return None

    def _verify(self, task: SwitchTask) -> tuple[bool, str]:
        """切换后验证：引擎 ready 且持有模型 == 目标。"""
        if task.category == "video":
            return self._verify_video(task)
        engine_obj = self._engine_for(task.category)
        if engine_obj is None:
            return False, f"无 {task.category} 适配器"
        try:
            st = engine_obj.get_status()
        except Exception as exc:  # noqa: BLE001
            return False, f"引擎状态查询异常: {exc}"
        if st.get("state") != "ready":
            return False, str(st.get("last_error") or "引擎未就绪")
        if st.get("model") != task.model_id:
            return False, f"引擎持有 {st.get('model')!r} 与目标 {task.model_id!r} 不符"
        return True, ""

    def _verify_video(self, task: SwitchTask) -> tuple[bool, str]:
        """video 验证（P2）：video_engine.get_status() 无 state 字段
        （loaded/model_path 口径不同）——H3 验证 ComfyUI 服务存活，
        diffusers 验证管线装载目录 == 目标。"""
        if _is_h3_model(task.model_id):
            from .inference.h3_engine import get_h3_engine
            try:
                if get_h3_engine().is_alive():
                    return True, ""
            except Exception as exc:  # noqa: BLE001
                return False, f"H3 服务探测失败: {exc}"
            return False, "ComfyUI 服务未就绪"
        from .inference.video_engine import get_video_engine
        try:
            st = get_video_engine().get_status()
        except Exception as exc:  # noqa: BLE001
            return False, f"引擎状态查询异常: {exc}"
        if not st.get("loaded") or st.get("fallback"):
            return False, "视频引擎未就绪（管线未装载）"
        cur = Path(str(st.get("model_path") or "")).name
        if cur != task.model_id:
            return False, f"引擎持有 {cur!r} 与目标 {task.model_id!r} 不符"
        return True, ""

    # ── 工具 ───────────────────────────────────────────────────

    @staticmethod
    def _set_stage(task: SwitchTask, status: str, stage: str,
                   progress: int) -> None:
        task.status, task.stage, task.progress = status, stage, progress
        logger.info("切换进度: %s %s %d%%", task.task_id, stage, progress)

    def _emit(self, task: SwitchTask) -> None:
        """广播进度事件（task_progress 前端全局接收，module=model_switch）。"""
        bc = self._broadcast
        if bc is None:
            return
        try:
            bc({"type": "task_progress",
                "data": {"module": "model_switch", **task.to_dict()}})
        except Exception:  # noqa: BLE001 - 广播失败不阻断任务
            logger.debug("切换进度广播失败", exc_info=True)

    def _emit_final(self, task: SwitchTask) -> None:
        """终态事件：done→task_complete；failed→task_error；
        cancelled→task_complete（前端全局任务停止跟踪，学 lora 先例）。"""
        bc = self._broadcast
        if bc is None:
            return
        base = {"module": "model_switch", "task_id": task.task_id,
                "category": task.category, "model_id": task.model_id,
                "source_model": task.source_model,
                "duration_s": round(task.finished_at - task.started_at, 1)
                if task.started_at else 0.0}
        try:
            if task.status == "done":
                bc({"type": "task_complete", "data": base})
            elif task.status == "failed":
                bc({"type": "task_error",
                    "data": {**base, "error": task.error,
                             "rollback_state": task.rollback_state,
                             "message": task.message}})
            else:
                bc({"type": "task_complete", "data": base})
        except Exception:  # noqa: BLE001
            logger.debug("切换终态广播失败", exc_info=True)

    def _release_feature_lock(self, feature: str) -> None:
        """跨线程释放功能锁（API 层 acquire → 任务线程移交释放）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.warning("事件循环不可用，功能锁 %s 未释放（重启自愈）", feature)
            return
        try:
            from ..middleware.feature_lock import get_feature_lock
            fut = asyncio.run_coroutine_threadsafe(
                get_feature_lock().release(feature), loop)
            fut.result(timeout=5)
        except Exception as exc:  # noqa: BLE001
            logger.warning("功能锁释放失败 (%s): %s", feature, exc)


def get_switch_engine() -> ModelSwitchEngine:
    """获取切换引擎单例。"""
    return ModelSwitchEngine.instance()
