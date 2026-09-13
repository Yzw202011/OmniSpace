"""OmniSpace AI v2.3.1 LoRA 增量微调服务（TASK-038 / TASK-053 训练加速）。

职责：
- 训练数据准备：合并 ChromaDB 知识（QA 格式，try import knowledge_service 容错）
  + 行为偏好对（behavior_service.build_training_pairs()），统一
  {"instruction","input","output"} 格式，MD5 去重，90/10 划分，≥100 条才允许训练。
- 触发条件（规格 §3.3）：数据 ≥100 + GPU 空闲（无功能锁占用）+ 无并发训练。
- 训练执行：真实 peft QLoRA 管线——BitsAndBytesConfig 4bit + LoRA(r=16, alpha=32,
  dropout=0.05)，lr 2e-5，epochs 3（可配），batch 1，max_seq 512，每 epoch 存
  检查点，loss 实时回调；训练前确认空闲显存 ≥10GB；基座不存在 → 任务标记
  failed 并说明，不崩溃。
- 训练加速（TASK-053 v2.3.1）：
  1) 注意力实现自动探测 flash_attention_2（包可用+计算能力≥8.0）→ sdpa → eager；
  2) 优化器 paged_adamw_8bit（bitsandbytes 8-bit 分页 AdamW，省显存）；
  3) DataLoader pin_memory=True + num_workers 平台自适应（win32 闭包 collator
     不可 pickle 故默认 0，其余平台 2）；
  4) 学习率调度 cosine_with_restarts（重启周期可配）；
  5) 空闲显存 ≥14/22GB 时保守上调梯度累积/批量（auto_batch 可关）；
  6) DeepSpeed ZeRO-2 可选启用（deepspeed 包可导入时；ZeRO-3 与 4bit 冲突禁用）。
  全部加速项带探测/降级，加速配置写入版本 meta.json["acceleration"] 留痕。
- 队列：进程内 PriorityQueue 模拟 Celery 语义（high/medium/low/background 四级），
  专属后台工作线程串行执行。

  Celery 迁移路径（对齐 TASK-038 "队列: Celery低优先级"）：
    1. trigger_finetune() 内 self._queue.put(...) 替换为
       lora_training_task.apply_async(kwargs={"config": cfg}, queue=q)，
       q 映射：high→'high_priority'，medium→'default'，low→'low_priority'，
       background→'low_priority'（+priority 参数）；
    2. _run_task() 主体平移到 @celery_app.task(queue='low_priority') 装饰的
       lora_training_task(config: dict)；
    3. ws_broadcaster 与 feature_lock 当前为进程内单例，迁移后改走
       Redis pub/sub 与分布式锁；训练/评估/版本管理逻辑无需改动。

- 版本管理：models/lora/v1, v2...（adapter_model.bin + adapter_config.json +
  meta.json{version,created_at,quality_score,data_count,base_model}），最多保留
  10 版；增量训练在上一版 adapter 基础上继续（PeftModel.from_pretrained 加载后
  is_trainable=True 续训）；支持 rollback / merge / get_current。
- 质量评估：验证集困惑度 + 样本生成质量启发式打分；通过 → 注册新版本，
  未通过 → 保留旧版并标记 pending_review。
- 训练期间经注入的 ws_broadcaster 推送 {"type":"status","module":"learn",...}
  事件，并持有 training 功能锁（feature_lock，规格 §6.1 互斥）。

资源约束（规格 §3.3：GPU30% / CPU50% / 内存50%）实现说明：
  通过 batch_size=1、max_seq=512、gradient_accumulation_steps=4 控制单步显存
  与内存占用；config["step_throttle_ms"]>0 时每个 optimizer step 后主动
  sleep，降低 GPU 占空比（近似 30% 限频）；每 epoch 结束 torch.cuda.empty_cache()
  （可由 config["empty_cache_per_epoch"] 关闭）。以上保证 16GB 显存内不 OOM。

单例用法::

    from backend.services.lora_training_service import get_lora_training_service
    svc = get_lora_training_service()
    if svc.should_trigger_finetune():
        svc.trigger_finetune(priority="low")
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import asyncio
import gc
import hashlib
import importlib
import json
import logging
import queue
import random
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import DATA_DIR, MODELS_DIR
from ..data.database import get_db_safe
from ..middleware.feature_lock import get_feature_lock
from .priority import Priority
from .vram_policy import TRAINING_MIN_FREE_GB

logger = logging.getLogger("omnispace.lora_training")


def _try_import(name: str) -> Any:
    """容错导入可选依赖。"""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _log_event(module: str, event: str, friendly: str, *,
               level: str = "info", detail: str = "") -> None:
    """大白话事件日志（2026-08-21 日志可视化；失败静默不影响训练）。"""
    try:
        from .event_log import log_event
        log_event(module, event, friendly, level=level, detail=detail)
    except Exception:  # noqa: BLE001
        pass


# ── 模块级注入点：WebSocket 广播器 ──────────────────────────────
# 由上层（main/websocket 服务）注入 callable(dict)，把训练事件推给前端。
ws_broadcaster: Callable[[dict], None] | None = None


def set_ws_broadcaster(fn: Callable[[dict], None] | None) -> None:
    """注入 WebSocket 广播器（None 表示禁用推送）。"""
    global ws_broadcaster
    ws_broadcaster = fn


def _broadcast(event: str, data: dict) -> None:
    """推送 {"type":"status","module":"learn","data":{"event":...}} 事件。"""
    fn = ws_broadcaster
    if fn is None:
        return
    try:
        fn({"type": "status", "module": "learn",
            "data": {"event": event, **data}})
    except Exception:  # noqa: BLE001 - 推送异常不影响训练
        pass


# ── 常量 ────────────────────────────────────────────────────────
MIN_TRAINING_SAMPLES = 100          # 触发微调的最少样本（规格 §3.3 / TASK-035）
MIN_FREE_VRAM_GB = TRAINING_MIN_FREE_GB  # 训练前空闲显存下限（批1 搬家单源，别名保留）
MAX_VERSIONS = 10                   # 最多保留版本数
LORA_DIR = MODELS_DIR / "lora"      # 版本存储根
CURRENT_FILE = LORA_DIR / "current.json"
TRAIN_DATA_DIR = DATA_DIR / "training"
DEFAULT_BASE_MODEL = MODELS_DIR / "qwen3-vl-4b"   # 默认基座（可用 config 覆盖）
EVAL_PPL_MAX_SAMPLES = 32           # 困惑度评估最多取样数
EVAL_GEN_SAMPLES = 3                # 生成质量评估取样数
EVAL_PASS_SCORE = 0.5               # 质量评估通过阈值

# §4.3 自适应：连续质量下降暂停自动训练
QUALITY_DECLINE_WINDOW = 4          # 检测窗口：最近 4 次完成训练的质量分
QUALITY_DECLINE_STREAK = 3          # 连续 3 次下降 → 暂停自动训练

# 训练任务优先级（对齐 Celery 队列语义）
PRIORITY_LEVELS = {"high": 0, "medium": 1, "low": 2, "background": 3}

# 本服务在全局调度中的优先级（文档 §8.4.2：P2 LoRA训练，
# 低于 P0 用户操作 / P1 模型预加载，高于 P3 浏览器学习等后台任务）
GLOBAL_PRIORITY = Priority.P2_TRAINING

# ── 用户训练默认参数（SET-015）───────────────────────────────────
# GET/PUT /learn/train/defaults 持久化到 system_settings
# key="learn.train_defaults"；trigger_finetune/外部触发合并顺序：
# DEFAULT_TRAIN_CONFIG < 用户默认值 < 显式 config。5s 缓存避免热路径读库。
_TRAIN_DEFAULTS_KEY = "learn.train_defaults"
# 允许用户覆盖的字段白名单（仅超参，不含路径/开关类）
_TRAIN_DEFAULTS_FIELDS = ("lora_rank", "lora_alpha", "lora_dropout",
                          "learning_rate", "epochs", "batch_size",
                          "gradient_accumulation_steps", "max_seq_length")
_train_defaults_cache: dict = {"value": None, "ts": 0.0}


def get_train_defaults() -> dict:
    """读取用户训练默认参数（无覆盖 → DEFAULT_TRAIN_CONFIG 相关子集）。"""
    now = time.time()
    if _train_defaults_cache["value"] is not None \
            and now - _train_defaults_cache["ts"] < 5.0:
        return dict(_train_defaults_cache["value"])
    base = {k: DEFAULT_TRAIN_CONFIG[k] for k in _TRAIN_DEFAULTS_FIELDS
            if k in DEFAULT_TRAIN_CONFIG}
    try:
        db = get_db_safe()
        if db is not None:
            row = db.query_one(
                "SELECT value FROM system_settings WHERE key=?",
                (_TRAIN_DEFAULTS_KEY,))
            if row:
                import json as _json
                user = _json.loads(row["value"])
                if isinstance(user, dict):
                    for k in _TRAIN_DEFAULTS_FIELDS:
                        if k in user:
                            base[k] = user[k]
    except Exception:  # noqa: BLE001 - 读库失败按内置默认
        pass
    _train_defaults_cache.update({"value": dict(base), "ts": now})
    return base


def set_train_defaults(updates: dict) -> dict:
    """写入用户训练默认参数（仅白名单字段），返回生效后的完整默认值。"""
    import json as _json

    current = get_train_defaults()
    for k in _TRAIN_DEFAULTS_FIELDS:
        if k in updates:
            current[k] = updates[k]
    db = get_db_safe()
    if db is not None:
        db.sql(
            "INSERT INTO system_settings (key, value, updated_at)"
            " VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (_TRAIN_DEFAULTS_KEY, _json.dumps(current, ensure_ascii=False),
             time.time()))
    _train_defaults_cache.update({"value": dict(current),
                                  "ts": time.time()})
    return current


# 默认训练配置（TASK-038 训练参数 / TASK-053 训练加速）
DEFAULT_TRAIN_CONFIG: dict[str, Any] = {
    "base_model": str(DEFAULT_BASE_MODEL),
    "lora_rank": 16,
    "lora_alpha": 32,
    "lora_dropout": 0.05,
    "learning_rate": 2e-5,
    "epochs": 3,
    "batch_size": 1,
    "gradient_accumulation_steps": 4,
    "warmup_steps": 10,
    "weight_decay": 0.01,
    "max_seq_length": 512,
    "logging_steps": 5,
    "empty_cache_per_epoch": True,
    # >0 时每个 optimizer step 后 sleep，降低 GPU 占空比（资源约束限频）
    "step_throttle_ms": 0,
    # ── TASK-053 训练加速 ────────────────────────────────────
    # 注意力实现：auto=探测 flash_attention_2（包可用+Ampere及以上）→sdpa→eager
    "attn_implementation": "auto",
    # 学习率调度：cosine_with_restarts（规格要求；restarts 周期数可调）
    "lr_scheduler_type": "cosine_with_restarts",
    "lr_restart_cycles": 3,
    # 优化器：8-bit 分页 AdamW（bitsandbytes），可切 adamw_torch 对照
    "optim": "paged_adamw_8bit",
    # DataLoader：pin_memory 加速 H2D 拷贝；num_workers 默认随平台
    # （Windows 上 >0 需 pickle 数据集/collator，闭包 collator 不可 pickle，
    #  故 win32 默认 0，其他平台默认 2；均可被 config 显式覆盖）
    "dataloader_num_workers": -1,     # -1 = 平台自适应
    "dataloader_pin_memory": True,
    # 显存自适应：空闲显存充裕时保守上调 batch/累积步（仍受 16GB 基线约束）
    "auto_batch": True,
    # DeepSpeed ZeRO（0=关闭；QLoRA 4bit 与 ZeRO-3 分片冲突，仅允许 ZeRO-2，
    # 且仅在 deepspeed 包可导入时启用，否则记录跳过）
    "deepspeed_zero": 0,
}

# train_tasks 表实际列（DB 更新时过滤）
_TRAIN_TASK_COLUMNS = {
    "base_model", "lora_rank", "lora_alpha", "learning_rate", "epochs",
    "dataset_path", "status", "progress", "updated_at",
}


class TrainingFailed(RuntimeError):
    """训练失败（基座缺失/依赖缺失/显存不足/训练异常）——任务标记 failed，不崩溃。"""


class TrainingCancelled(RuntimeError):
    """用户强制取消训练（铁律：用户操作拥有最高权限）——任务标记 cancelled。

    与 TrainingFailed 严格区分：取消不是失败，不落 error 状态、不注册
    半成品 adapter 版本。
    """


class LoRATrainingService:
    """LoRA 增量微调服务单例。

    线程模型：trigger_finetune() 入队（PriorityQueue），专属 daemon 工作线程
    串行出队执行 train() → evaluate() → 注册/待审核。
    """

    _instance: LoRATrainingService | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._queue: queue.PriorityQueue[tuple[int, int, dict]] = queue.PriorityQueue()
        self._seq = 0                          # 同优先级 FIFO 序号
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._training_task_id: str | None = None
        self._cancel_flags: set[str] = set()   # 强制取消的任务 ID（运行时中断）
        self._dataset: dict | None = None   # prepare_training_data 缓存
        self._last_train_data: dict | None = None  # 最近训练实际使用的数据集（供评估取验证集）
        self._mem_tasks: dict[str, dict] = {}  # DB 不可用时的任务镜像
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock_via_fallback = False

    # ═══════════════════════════════════════════════════════════
    #  强制取消（铁律：用户操作拥有最高权限，点击取消必须真实中断）
    # ═══════════════════════════════════════════════════════════

    def request_cancel(self, task_id: str) -> None:
        """请求强制取消：置位运行时中断标志。

        由 API 层 /learn/tasks/{id}/cancel 在落库 cancelled 后调用。
        训练线程经 TrainerCallback.on_step_end 检测标志并置
        control.should_training_stop=True（HF 官方中断通道），
        在下一个 step 边界干净退出训练循环。
        """
        with self._state_lock:
            self._cancel_flags.add(task_id)
        logger.info("训练取消请求已置位（将在下一步边界中断）: %s", task_id)

    def _is_cancelled(self, task_id: str) -> bool:
        """检查任务是否被请求取消。"""
        with self._state_lock:
            return task_id in self._cancel_flags

    def _clear_cancel(self, task_id: str) -> None:
        """清除取消标志（任务收敛后调用，防泄漏）。"""
        with self._state_lock:
            self._cancel_flags.discard(task_id)

    @classmethod
    def instance(cls) -> LoRATrainingService:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ═══════════════════════════════════════════════════════════
    #  训练数据准备（TASK-038 prepare_training_data）
    # ═══════════════════════════════════════════════════════════

    def prepare_training_data(self, force: bool = False) -> dict:
        """合并知识库 QA + 行为偏好对，统一格式/去重/90:10 划分。

        Returns:
            {"train": [...], "valid": [...], "total": int,
             "sources": {"knowledge": n, "behavior": n},
             "sufficient": bool, "prepared_at": float}
        """
        if self._dataset is not None and not force:
            return self._dataset

        samples: list[dict] = []
        sources = {"knowledge": 0, "behavior": 0}

        # 来源 1：ChromaDB 知识（QA 格式，knowledge_service 容错）
        try:
            from .knowledge_service import get_knowledge_service
            ks = get_knowledge_service()
            page = 1
            while page <= 50:                    # 上限保护（≤10000 条）
                batch = ks.list_knowledge(page=page, page_size=200)
                items = batch.get("items", [])
                for it in items:
                    output = (it.get("content") or "").strip()
                    if not output:
                        continue
                    topic = (it.get("topic") or "").strip()
                    instruction = (f"请解释「{topic}」相关的以下知识点"
                                   if topic else "请解释以下知识点")
                    samples.append({
                        "instruction": instruction,
                        "input": (it.get("title") or it.get("type") or "").strip(),
                        "output": output,
                    })
                    sources["knowledge"] += 1
                total = int(batch.get("total", 0) or 0)
                if not items or page * 200 >= total:
                    break
                page += 1
        except Exception as exc:  # noqa: BLE001 - 数据源缺失不致命
            logger.info("知识库训练样本不可用，跳过: %s", exc)

        # 来源 2：行为学习偏好对
        try:
            from .behavior_service import get_behavior_service
            for p in get_behavior_service().build_training_pairs():
                output = (p.get("output") or "").strip()
                if not output:
                    continue
                samples.append({
                    "instruction": (p.get("instruction")
                                    or "请根据输入生成内容").strip(),
                    "input": (p.get("input") or "").strip(),
                    "output": output,
                })
                sources["behavior"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.info("行为偏好训练样本不可用，跳过: %s", exc)

        # 去重（instruction+input+output 的 MD5）
        seen: set[str] = set()
        uniq: list[dict] = []
        for s in samples:
            key = hashlib.md5(
                (s["instruction"] + "\x00" + s["input"] + "\x00"
                 + s["output"]).encode("utf-8")).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(s)

        # 90/10 划分（固定种子，可复现）
        rng = random.Random(42)
        rng.shuffle(uniq)
        n_valid = max(1, int(len(uniq) * 0.1)) if uniq else 0
        valid, train = uniq[:n_valid], uniq[n_valid:]

        self._dataset = {
            "train": train, "valid": valid, "total": len(uniq),
            "sources": sources,
            "sufficient": len(uniq) >= MIN_TRAINING_SAMPLES,
            "prepared_at": time.time(),
        }
        logger.info("训练数据准备完成: 共 %d 条（知识 %d + 行为 %d），"
                    "训练 %d / 验证 %d",
                    len(uniq), sources["knowledge"], sources["behavior"],
                    len(train), len(valid))
        return self._dataset

    # ═══════════════════════════════════════════════════════════
    #  触发判定与入队（规格 §3.3 训练触发条件检测）
    # ═══════════════════════════════════════════════════════════

    def should_trigger_finetune(self, config: dict | None = None) -> bool:
        """触发条件：数据 ≥100 + GPU 空闲（无功能锁占用）+ 无并发训练。

        config 含 dataset_path 时按外部数据集判定充分性（API 显式指定场景）。
        """
        with self._state_lock:
            if self._training_task_id is not None:
                return False
        if not self._queue.empty():
            return False
        if get_feature_lock().active_feature is not None:
            return False
        data = None
        if config and config.get("dataset_path"):
            data = self._load_external_dataset(str(config["dataset_path"]))
        if data is None:
            data = self._dataset or self.prepare_training_data()
        return data["total"] >= MIN_TRAINING_SAMPLES

    def trigger_finetune(self, config: dict | None = None,
                         priority: str = "low") -> str | None:
        """入队一次微调任务，返回 task_id；条件不满足返回 None。

        Celery 语义对齐：本方法等价于
        lora_training_task.apply_async(kwargs={"config": cfg}, queue=priority)。
        """
        # 尽可能捕获事件循环（API async 上下文触发时），供功能锁协程回投
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        cfg = {**DEFAULT_TRAIN_CONFIG, **get_train_defaults(),
               **(config or {})}
        # 审计 P0-6 服务层二次 clamp：API 层 Field 校验之外的调用路径
        # （自动触发/内部调用）同样必须受硬件安全边界约束，越界 clamp 并留痕。
        cfg = self._clamp_hyperparams(cfg)
        if not self.should_trigger_finetune(config=cfg):
            return None
        task_id = uuid.uuid4().hex
        # 外部数据集优先落记录；否则落盘自动构建数据集供审计复用
        dataset_path = (cfg.get("dataset_path") or "").strip() \
            or self._dump_dataset(task_id)
        now = time.time()
        record = {
            "id": task_id, "base_model": cfg["base_model"],
            "lora_rank": cfg["lora_rank"], "lora_alpha": cfg["lora_alpha"],
            "learning_rate": cfg["learning_rate"], "epochs": cfg["epochs"],
            "dataset_path": dataset_path, "status": "queued",
            "progress": 0.0, "created_at": now, "updated_at": now,
        }
        db = get_db_safe()
        if db is not None:
            try:
                db.insert("train_tasks", record)
            except Exception as exc:  # noqa: BLE001
                logger.warning("训练任务落库失败，降级内存镜像: %s", exc)
                self._mem_tasks[task_id] = record
        else:
            self._mem_tasks[task_id] = record

        with self._state_lock:
            self._seq += 1
            seq = self._seq
        level = PRIORITY_LEVELS.get(priority, PRIORITY_LEVELS["low"])
        self._queue.put((level, seq, {"task_id": task_id, "config": cfg}))
        self._ensure_worker()
        _broadcast("training_queued", {"task_id": task_id, "priority": priority,
                                       "global_priority": GLOBAL_PRIORITY.name})
        logger.info("微调任务入队: %s (priority=%s, global=%s)",
                    task_id, priority, GLOBAL_PRIORITY.name)
        return task_id

    # ── 超参边界（审计 P0-6 服务层二次保护）───────────────────────
    # 与 api 层 TrainTaskCreate 的 Field 约束一致：
    #   epochs 1~50 / lora_rank 4~64 / learning_rate (0, 1e-3]
    _HP_BOUNDS = {
        "epochs": (1, 50, int),
        "lora_rank": (4, 64, int),
        "learning_rate": (1e-7, 1e-3, float),
    }

    @classmethod
    def _clamp_hyperparams(cls, cfg: dict) -> dict:
        """把超参钳制到硬件安全边界内；发生钳制时记录 warning 留痕。

        防线说明：API 层 pydantic Field 只覆盖 HTTP 入口；自动触发、
        内部调用、DB 重放等路径直接进入本服务，必须在入队前二次钳制，
        防止 epochs=500 / rank=256 之类的危险配置真实落到 GPU 上。
        """
        out = dict(cfg)
        for key, (lo, hi, cast) in cls._HP_BOUNDS.items():
            raw = out.get(key)
            try:
                val = cast(raw)
            except (TypeError, ValueError):
                val = cast(DEFAULT_TRAIN_CONFIG.get(key, lo))
            clamped = max(lo, min(val, hi))
            if clamped != val:
                logger.warning("训练超参越界已钳制: %s %r -> %r (边界 [%s, %s])",
                               key, raw, clamped, lo, hi)
            out[key] = clamped
        return out

    @staticmethod
    def _is_allowed_dataset_path(path: Path) -> bool:
        """审计 P1-5：训练集路径白名单——仅允许训练数据目录内的文件。

        允许根：DATA_DIR/training（含 uploads 子目录）。
        拒绝一切指向系统目录/用户目录/模型权目录的外部路径，
        防止经 /learn/train 的 dataset_path 读取任意文件内容
        （路径穿越 + 信息泄露）。
        """
        try:
            resolved = path.resolve()
            root = TRAIN_DATA_DIR.resolve()
            return resolved == root or root in resolved.parents
        except OSError:
            return False

    @classmethod
    def _load_external_dataset(cls, dataset_path: str) -> dict | None:
        """加载 API 上传的外部 JSONL 数据集（{"instruction","input","output"} 每行一条）。

        与 prepare_training_data 相同的 90/10 划分与去重逻辑；
        路径为空/文件不存在/解析失败时返回 None（回退自动构建）。
        审计 P1-5：路径必须在训练数据目录白名单内，否则拒绝加载。
        """
        if not dataset_path:
            return None
        path = Path(dataset_path)
        if not cls._is_allowed_dataset_path(path):
            logger.warning("外部训练集路径越出白名单，拒绝加载: %s", path)
            return None
        if not path.is_file():
            logger.warning("外部训练集不存在，回退自动构建: %s", path)
            return None
        samples: list[dict] = []
        try:
            with open(path, encoding="utf-8") as f:
                for ln, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug("外部训练集第 %d 行非 JSON，跳过", ln)
                        continue
                    output = str(obj.get("output") or "").strip()
                    if not output:
                        continue
                    samples.append({
                        "instruction": str(obj.get("instruction")
                                           or "请根据输入生成内容").strip(),
                        "input": str(obj.get("input") or "").strip(),
                        "output": output,
                    })
        except OSError as exc:
            logger.warning("外部训练集读取失败，回退自动构建: %s", exc)
            return None

        # 去重 + 90/10 划分（与 prepare_training_data 一致，固定种子）
        seen: set[str] = set()
        uniq: list[dict] = []
        for s in samples:
            key = hashlib.md5(
                (s["instruction"] + "\x00" + s["input"] + "\x00"
                 + s["output"]).encode("utf-8")).hexdigest()
            if key not in seen:
                seen.add(key)
                uniq.append(s)
        rng = random.Random(42)
        rng.shuffle(uniq)
        n_valid = max(1, int(len(uniq) * 0.1)) if uniq else 0
        data = {
            "train": uniq[n_valid:], "valid": uniq[:n_valid],
            "total": len(uniq),
            "sources": {"external": len(uniq)},
            "sufficient": len(uniq) >= MIN_TRAINING_SAMPLES,
            "prepared_at": time.time(),
        }
        logger.info("外部训练集加载完成: %s → %d 条（训练 %d / 验证 %d）",
                    path, len(uniq), len(data["train"]), len(data["valid"]))
        return data

    def _dump_dataset(self, task_id: str) -> str:
        """把当前训练集落盘为 JSONL（供 Trainer 与审计复用）。"""
        data = self._dataset or self.prepare_training_data()
        TRAIN_DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = TRAIN_DATA_DIR / f"dataset_{task_id}.jsonl"
        try:
            with open(path, "w", encoding="utf-8") as f:
                for s in data["train"] + data["valid"]:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("训练集落盘失败: %s", exc)
            return ""
        return str(path)

    def _ensure_worker(self) -> None:
        """确保专属后台工作线程在运行。"""
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop, daemon=True, name="lora-training")
            self._worker.start()

    def _worker_loop(self) -> None:
        """串行消费训练队列（一次仅一个训练任务，天然满足无并发约束）。"""
        while True:
            item = self._queue.get()
            try:
                self._run_task(item[2])
            except Exception as exc:  # noqa: BLE001 - 工作线程不崩溃
                logger.error("训练工作线程异常: %s", exc)
            finally:
                self._queue.task_done()

    # ═══════════════════════════════════════════════════════════
    #  任务执行（功能锁 + 训练 + 评估 + 注册）
    # ═══════════════════════════════════════════════════════════

    def _run_task(self, task: dict) -> None:
        task_id = task["task_id"]
        cfg = task["config"]
        # 审计 R3-BE1：排队期间被 /learn/tasks/{id}/cancel 取消的任务
        # 出队后直接跳过，保证排队任务的取消真实生效
        db = get_db_safe()
        if db is not None:
            try:
                row = db.query_one(
                    "SELECT status FROM train_tasks WHERE id=?", (task_id,))
                if row is not None and row.get("status") == "cancelled":
                    logger.info("训练任务已取消，跳过执行: %s", task_id)
                    _broadcast("training_cancelled", {"task_id": task_id,
                                                      "reason": "queued"})
                    self._clear_cancel(task_id)
                    return
            except Exception:  # noqa: BLE001 - 查询失败不阻断训练
                pass
        with self._state_lock:
            self._training_task_id = task_id
        self._update_task(task_id, status="training")
        _broadcast("training_started", {"task_id": task_id,
                                        "base_model": cfg["base_model"]})
        _log_event(
            "training", "training_started",
            f"知识训练开始了（任务 {task_id}）：AI 正在学习你的资料，"
            f"基于模型「{cfg['base_model']}」",
            level="info", detail=f"task={task_id}")

        if not self._acquire_training_lock(task_id):
            self._update_task(task_id, status="error")
            _broadcast("training_failed",
                       {"task_id": task_id, "error": "无法获取 training 功能锁"})
            with self._state_lock:
                self._training_task_id = None
            return

        try:
            result = self.train(cfg, progress_cb=self._make_progress_cb(task_id),
                                task_id=task_id)
            version = result["version"]
            self._update_task(task_id, status="evaluating", progress=0.95)
            _broadcast("training_evaluating",
                       {"task_id": task_id, "version": version})
            report = self.evaluate(version)
            passed = bool(report.get("passed"))
            # 评估完成后的取消检查：评估期间用户取消同样生效（铁律：
            # 用户操作拥有最高权限），已评估版本不注册、不切 current
            if self._is_cancelled(task_id):
                self._update_task(task_id, status="cancelled")
                _broadcast("training_cancelled", {"task_id": task_id,
                                                  "reason": "evaluating",
                                                  "version": version})
                _log_event("training", "training_cancelled",
                           f"训练任务在评估阶段被取消（任务 {task_id}）",
                           level="warning", detail=f"task={task_id}")
                return
            if passed:
                self._set_current(version)
                # R2-B04 自主进化闭环：新版本生效后通知对话引擎热更新
                # 知识 LoRA（引擎未加载时跳过，下次 load_model 自动挂载；
                # 异常不影响训练结果）
                try:
                    from .inference.dialog_engine import get_dialog_engine
                    get_dialog_engine().refresh_knowledge_lora()
                except Exception:  # noqa: BLE001
                    pass
            self._update_task(task_id, status="done", progress=1.0)
            _broadcast("training_completed", {
                "task_id": task_id, "version": version,
                "quality_score": report.get("quality_score"),
                "passed": passed,
                "status": "registered" if passed else "pending_review",
            })
            _log_event(
                "training", "training_completed",
                f"知识训练完成了（版本 {version}），AI 学会了新资料"
                + ("，新版本已生效" if passed
                   else "。质量分还不够，这个版本先存档备用，没有启用"),
                level="success" if passed else "warning",
                detail=(f"task={task_id}, version={version}, "
                        f"score={report.get('quality_score')}"))
        except TrainingCancelled as exc:
            # 用户强制取消：真实中断（step 边界退出训练循环，未保存半成品）
            logger.info("训练任务被用户强制取消: %s: %s", task_id, exc)
            self._update_task(task_id, status="cancelled")
            _broadcast("training_cancelled", {"task_id": task_id,
                                              "reason": "training"})
            _log_event("training", "training_cancelled",
                       f"训练任务被你手动取消了（任务 {task_id}），"
                       "显存已释放，没有保存半成品",
                       level="warning", detail=f"task={task_id}, {exc}")
        except TrainingFailed as exc:
            logger.error("训练失败: %s: %s", task_id, exc)
            self._update_task(task_id, status="error")
            _broadcast("training_failed", {"task_id": task_id, "error": str(exc)})
            _log_event("training", "training_failed",
                       f"训练失败了：{exc}。可以重试一次；"
                       "如果反复失败，请检查模型文件是否完整",
                       level="error", detail=f"task={task_id}")
        except Exception as exc:  # noqa: BLE001 - 未预期异常同失败处理
            logger.exception("训练未预期异常: %s", task_id)
            self._update_task(task_id, status="error")
            _broadcast("training_failed",
                       {"task_id": task_id, "error": f"未预期异常: {exc}"})
            _log_event("training", "training_failed",
                       f"训练遇到意外错误：{exc}。可以重试一次",
                       level="error",
                       detail=f"task={task_id}, type={type(exc).__name__}")
        finally:
            self._release_training_lock()
            self._clear_cancel(task_id)
            with self._state_lock:
                self._training_task_id = None

    def _make_progress_cb(self, task_id: str) -> Callable[[dict], None]:
        """训练进度回调：更新 train_tasks.progress + 推送 training_progress。"""
        def cb(info: dict) -> None:
            step = info.get("step", 0)
            max_steps = max(1, info.get("max_steps", 1))
            progress = min(0.94, 0.05 + 0.9 * step / max_steps)
            self._update_task(task_id, progress=round(progress, 4))
            _broadcast("training_progress", {
                "task_id": task_id, "loss": info.get("loss"),
                "epoch": info.get("epoch"), "step": step,
                "max_steps": max_steps, "progress": round(progress, 4),
            })
        return cb

    def _update_task(self, task_id: str, **fields: Any) -> None:
        """更新任务记录（DB 优先，内存镜像兜底）。"""
        fields["updated_at"] = time.time()
        db = get_db_safe()
        if db is not None:
            try:
                db_fields = {k: v for k, v in fields.items()
                             if k in _TRAIN_TASK_COLUMNS}
                db.update("train_tasks", db_fields, "id=?", (task_id,))
            except Exception as exc:  # noqa: BLE001
                logger.debug("训练任务更新落库失败: %s", exc)
        if task_id in self._mem_tasks:
            self._mem_tasks[task_id].update(fields)

    # ── 功能锁（training）────────────────────────────────────────

    def _acquire_training_lock(self, task_id: str) -> bool:
        """获取 training 功能锁；事件循环可用时回投协程，否则同步降级。"""
        mgr = get_feature_lock()
        self._lock_via_fallback = False
        loop = self._loop
        if loop is not None:
            try:
                if not loop.is_closed() and loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(
                        mgr.acquire("training", task_id=task_id), loop)
                    return bool(fut.result(timeout=10))
            except Exception as exc:  # noqa: BLE001
                logger.warning("经事件循环获取 training 锁失败，走同步降级: %s", exc)
        # 同步降级：单机本地运行（规格 §14 约束1）。批1 多卡地基
        # （2026-09-05）起经 acquire_sync 与异步路径共享同一份按域
        # 状态，不再直写 _holder 私有字段（语义与 acquire 一致：同域
        # 跨功能互斥、同功能可重入）。
        if not mgr.acquire_sync("training", task_id=task_id):
            return False
        self._lock_via_fallback = True
        return True

    def _release_training_lock(self) -> None:
        """释放 training 功能锁（与获取路径对称）。"""
        mgr = get_feature_lock()
        if not self._lock_via_fallback:
            loop = self._loop
            if loop is not None and not loop.is_closed() and loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(
                        mgr.release("training"), loop)
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning("经事件循环释放 training 锁失败: %s", exc)
        mgr.release_sync("training")

    # ═══════════════════════════════════════════════════════════
    #  训练执行（真实 peft QLoRA 管线，TASK-038 train）
    # ═══════════════════════════════════════════════════════════

    def train(self, config: dict | None = None,
              progress_cb: Callable[[dict], None] | None = None,
              task_id: str = "") -> dict:
        """QLoRA 增量训练。

        管线：BitsAndBytesConfig 4bit 基座 → prepare_model_for_kbit_training →
        LoraConfig(r=16, alpha=32, dropout=0.05)（有上一版则 PeftModel 加载续训）
        → Trainer（lr 2e-5, epochs 3, batch 1, max_seq 512, 每 epoch 检查点,
        loss 实时回调）→ 保存新 adapter 版本目录。

        Args:
            task_id: 任务 ID（非空时启用强制取消中断）。

        Returns:
            {"version": "v3", "version_dir": str, "data_count": int,
             "base_model": str, "train_loss": float}

        Raises:
            TrainingFailed: 基座不存在/依赖缺失/数据不足/显存不足/训练异常。
            TrainingCancelled: 用户强制取消。
        """
        cfg = {**DEFAULT_TRAIN_CONFIG, **get_train_defaults(),
               **(config or {})}

        # ── 前置校验：基座存在性 ────────────────────────────────
        base_dir = Path(cfg["base_model"])
        has_weights = base_dir.is_dir() and (
            any(base_dir.glob("*.safetensors"))
            or any(base_dir.glob("*.bin"))
            or any(base_dir.glob("*.gguf"))
            or (base_dir / "model.safetensors.index.json").is_file())
        if not has_weights:
            raise TrainingFailed(
                f"基座模型不存在或权重未下载完成: {base_dir}。"
                f"请将语言模型放入该目录后重试（默认 models/qwen3-vl-4b，"
                f"可用 config['base_model'] 指定其他目录）")

        # ── 前置校验：训练数据量 ────────────────────────────────
        # 显式 dataset_path（API 上传的外部 JSONL）优先于自动构建数据集
        data = self._load_external_dataset(cfg.get("dataset_path") or "")
        if data is None:
            data = self._dataset or self.prepare_training_data()
        if data["total"] < MIN_TRAINING_SAMPLES:
            raise TrainingFailed(
                f"训练数据不足：当前 {data['total']} 条，"
                f"需要 ≥{MIN_TRAINING_SAMPLES} 条")
        # 记录本次训练实际使用的数据集，供 evaluate() 取验证集
        # （外部数据集场景 self._dataset 为空，评估不能因此降级）
        self._last_train_data = data

        # ── 前置校验：依赖 ─────────────────────────────────────
        torch = _try_import("torch")
        transformers = _try_import("transformers")
        peft = _try_import("peft")
        if torch is None or transformers is None or peft is None:
            raise TrainingFailed(
                "训练依赖缺失（torch/transformers/peft 任一不可导入）")

        # ── 显存保护：训练前确认空闲 ≥10GB ─────────────────────
        if not torch.cuda.is_available():
            raise TrainingFailed("CUDA 不可用：QLoRA 训练需要 NVIDIA GPU")
        try:
            free_bytes, total_bytes = torch.cuda.mem_get_info()
            free_gb = free_bytes / (1024 ** 3)
        except Exception as exc:  # noqa: BLE001
            raise TrainingFailed(f"无法读取显存状态: {exc}") from exc
        if free_gb < MIN_FREE_VRAM_GB:
            raise TrainingFailed(
                f"空闲显存不足：{free_gb:.1f}GB < {MIN_FREE_VRAM_GB}GB，"
                f"请先释放其他模型后再训练")
        logger.info("训练前显存: 空闲 %.1fGB / 总量 %.1fGB",
                    free_gb, total_bytes / (1024 ** 3))

        try:
            return self._train_impl(cfg, data, torch, transformers, peft,
                                    progress_cb, free_gb, task_id)
        except (TrainingFailed, TrainingCancelled):
            raise
        except Exception as exc:  # noqa: BLE001 - 统一为 TrainingFailed
            raise TrainingFailed(f"训练过程异常: {exc}") from exc

    # ── TASK-053 训练加速：探测与自适应 ─────────────────────────────

    @staticmethod
    def _probe_attn_impl(torch: Any, requested: str = "auto") -> str:
        """探测注意力实现：flash_attention_2 → sdpa → eager。

        FA2 条件：flash_attn 包可导入 且 CUDA 计算能力 ≥8.0（Ampere+；
        RTX 30/40/50 全系满足）。requested 非 auto 时按请求值原样返回
        （加载失败时 _load_base_model 仍会自动降级）。
        """
        if requested and requested != "auto":
            return requested
        try:
            if _try_import("flash_attn") is not None \
                    and torch.cuda.is_available():
                major, _minor = torch.cuda.get_device_capability()
                if major >= 8:
                    logger.info("注意力实现: flash_attention_2（探测通过）")
                    return "flash_attention_2"
        except Exception:  # noqa: BLE001
            pass
        # sdpa：PyTorch 内置 scaled_dot_product_attention，
        # 内核自动选 flash/mem_efficient，无需三方包
        logger.info("注意力实现: sdpa（flash_attn 不可用，FA2 跳过）")
        return "sdpa"

    @staticmethod
    def _auto_tune_batch(cfg: dict, free_gb: float) -> dict:
        """显存自适应批量（TASK-053 要求5，硬件安全优先）。

        保守策略（基线 16GB 卡约束）：
          空闲 ≥22GB → batch 2 / accum 8（有效批量 16）
          空闲 ≥14GB → batch 1 / accum 8（有效批量 8）
          其余保持默认 batch 1 / accum 4。
        auto_batch=False 或 config 已显式给出非默认值时不改动。
        """
        if not cfg.get("auto_batch", True):
            return cfg
        tuned = dict(cfg)
        if free_gb >= 22.0 and int(cfg.get("batch_size", 1)) == 1:
            tuned["batch_size"] = 2
            tuned["gradient_accumulation_steps"] = max(
                8, int(cfg.get("gradient_accumulation_steps", 4)))
        elif free_gb >= 14.0:
            tuned["gradient_accumulation_steps"] = max(
                8, int(cfg.get("gradient_accumulation_steps", 4)))
        if tuned != cfg:
            logger.info("显存自适应批量: 空闲 %.1fGB → batch=%d accum=%d",
                        free_gb, tuned["batch_size"],
                        tuned["gradient_accumulation_steps"])
        return tuned

    @staticmethod
    def _resolve_num_workers(cfg: dict) -> int:
        """解析 dataloader_num_workers（-1=平台自适应：win32→0，其他→2）。

        Windows 上 DataLoader worker 走 spawn，需 pickle 数据集与 collator；
        本项目 collator 是闭包（不可 pickle），故 win32 强制 0，
        pin_memory 仍提供主要 H2D 加速收益。
        """
        import sys
        n = int(cfg.get("dataloader_num_workers", -1))
        if n >= 0:
            if sys.platform == "win32" and n > 0:
                logger.warning("Windows 平台 num_workers>0 需可 pickle 的 "
                               "collator，已按 config 显式值 %d 执行", n)
            return n
        return 0 if sys.platform == "win32" else 2

    def _train_impl(self, cfg: dict, data: dict, torch: Any,
                    transformers: Any, peft: Any,
                    progress_cb: Callable[[dict], None] | None,
                    free_gb: float = 0.0,
                    task_id: str = "") -> dict:
        """QLoRA 训练实现（train() 的内部拆分，便于独立测试）。

        task_id 非空时挂载强制取消回调（每个 step 边界检测中断标志，
        用户取消在下一步边界干净退出训练循环）。
        """
        base_dir = str(cfg["base_model"])
        max_seq = int(cfg["max_seq_length"])
        # TASK-053：注意力实现探测 + 显存自适应批量 + DataLoader 参数
        attn_impl = self._probe_attn_impl(
            torch, str(cfg.get("attn_implementation", "auto")))
        cfg = self._auto_tune_batch(cfg, free_gb)
        num_workers = self._resolve_num_workers(cfg)
        pin_memory = bool(cfg.get("dataloader_pin_memory", True))

        # 4bit 量化配置（QLoRA）
        bnb_config = transformers.BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
        )

        tokenizer = transformers.AutoTokenizer.from_pretrained(
            base_dir, trust_remote_code=True)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        # 基座加载（VL 模型优先 CausalLM 接口，失败回退通用 AutoModel）
        base_model = self._load_base_model(transformers, base_dir, bnb_config,
                                           attn_impl=attn_impl)
        base_model = peft.prepare_model_for_kbit_training(base_model)
        base_model.config.use_cache = False

        # 增量训练：存在当前版本 adapter 时在其基础上继续
        lora_config = peft.LoraConfig(
            r=int(cfg["lora_rank"]),
            lora_alpha=int(cfg["lora_alpha"]),
            lora_dropout=float(cfg["lora_dropout"]),
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
            task_type="CAUSAL_LM",
        )
        current = self.get_current()
        if current:
            adapter_dir = str(LORA_DIR / current)
            logger.info("增量训练：加载上一版 adapter %s 续训", current)
            model = peft.PeftModel.from_pretrained(
                base_model, adapter_dir, is_trainable=True)
        else:
            model = peft.get_peft_model(base_model, lora_config)

        # 数据集 tokenize（prompt 部分 labels=-100 掩码）
        train_feats = self._tokenize(tokenizer, data["train"], max_seq)
        collator = self._make_collator(tokenizer)

        task_tag = time.strftime("%Y%m%d_%H%M%S")
        ckpt_dir = LORA_DIR / "checkpoints" / task_tag
        # TASK-053：学习率调度器（cosine_with_restarts，重启周期可配）
        scheduler_type = str(cfg.get("lr_scheduler_type",
                                     "cosine_with_restarts"))
        scheduler_kwargs: dict[str, Any] = {}
        if scheduler_type == "cosine_with_restarts":
            scheduler_kwargs["num_cycles"] = max(
                1, int(cfg.get("lr_restart_cycles", 3)))
        # TASK-053：DeepSpeed ZeRO-2（可选；QLoRA 4bit 与 ZeRO-3 冲突，禁用）
        ds_zero = int(cfg.get("deepspeed_zero", 0) or 0)
        ds_config: dict | None = None
        if ds_zero > 0:
            if ds_zero >= 3:
                logger.warning("QLoRA 4bit 量化与 ZeRO-3 参数分片冲突，"
                               "已降级 ZeRO-2")
                ds_zero = 2
            if _try_import("deepspeed") is not None:
                ds_config = {
                    "zero_optimization": {"stage": 2},
                    "fp16": {"enabled": True},
                    "train_micro_batch_size_per_gpu": int(cfg["batch_size"]),
                    "gradient_accumulation_steps":
                        int(cfg["gradient_accumulation_steps"]),
                }
                logger.info("DeepSpeed ZeRO-%d 已启用", ds_zero)
            else:
                logger.warning("deepspeed 包不可用（Windows 离线环境常见），"
                               "ZeRO 跳过，继续常规训练")
        args_kwargs: dict[str, Any] = dict(
            output_dir=str(ckpt_dir),
            per_device_train_batch_size=int(cfg["batch_size"]),
            gradient_accumulation_steps=int(cfg["gradient_accumulation_steps"]),
            learning_rate=float(cfg["learning_rate"]),
            num_train_epochs=int(cfg["epochs"]),
            warmup_steps=int(cfg["warmup_steps"]),
            weight_decay=float(cfg["weight_decay"]),
            logging_steps=int(cfg["logging_steps"]),
            save_strategy="epoch",               # 每 epoch 存检查点
            fp16=True,
            gradient_checkpointing=True,
            optim=str(cfg.get("optim", "paged_adamw_8bit")),  # TASK-053 8bit
            lr_scheduler_type=scheduler_type,                 # TASK-053
            dataloader_num_workers=num_workers,               # TASK-053
            dataloader_pin_memory=pin_memory,                 # TASK-053
            report_to=[],
            remove_unused_columns=False,
        )
        if scheduler_kwargs:
            args_kwargs["lr_scheduler_kwargs"] = scheduler_kwargs
        if ds_config is not None:
            args_kwargs["deepspeed"] = ds_config
        args = transformers.TrainingArguments(**args_kwargs)

        callbacks = [self._loss_callback(progress_cb)]
        if task_id:
            callbacks.append(self._cancel_callback(task_id))
        if cfg.get("empty_cache_per_epoch", True):
            callbacks.append(self._cache_cleanup_callback(torch))
        throttle_ms = int(cfg.get("step_throttle_ms", 0) or 0)
        if throttle_ms > 0:
            callbacks.append(self._throttle_callback(throttle_ms))

        trainer = transformers.Trainer(
            model=model, args=args,
            train_dataset=train_feats, data_collator=collator,
            callbacks=callbacks,
        )
        train_result = trainer.train()

        # 强制取消检查：should_training_stop 触发的"正常"返回若源于取消，
        # 不保存半成品 adapter、不注册版本——取消即彻底废弃本次产物
        if task_id and self._is_cancelled(task_id):
            del trainer, model, base_model
            gc.collect()
            try:
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            raise TrainingCancelled(f"任务已被用户取消: {task_id}")

        # 保存新版本（adapter_model.bin + adapter_config.json）
        version_dir, version = self._next_version_dir()
        model.save_pretrained(str(version_dir), safe_serialization=False)
        meta = {
            "version": version,
            "created_at": time.time(),
            "quality_score": None,               # evaluate() 回填
            "status": "pending_evaluation",
            "data_count": data["total"],
            "base_model": base_dir,
            "config": {k: cfg[k] for k in
                       ("lora_rank", "lora_alpha", "lora_dropout",
                        "learning_rate", "epochs", "max_seq_length")},
            "acceleration": {                 # TASK-053 加速配置留痕
                "attn_implementation": attn_impl,
                "lr_scheduler_type": scheduler_type,
                "optim": str(cfg.get("optim", "paged_adamw_8bit")),
                "batch_size": int(cfg["batch_size"]),
                "gradient_accumulation_steps":
                    int(cfg["gradient_accumulation_steps"]),
                "dataloader_num_workers": num_workers,
                "dataloader_pin_memory": pin_memory,
                "deepspeed_zero": ds_zero if ds_config else 0,
            },
            "incremental_from": current or "",
            "train_loss": float(getattr(train_result, "training_loss", 0.0)),
        }
        (version_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self._prune_versions()

        # 训练后释放训练态显存（adapter 保存完毕即可卸载基座）
        del trainer, model, base_model
        gc.collect()
        try:
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

        logger.info("训练完成: %s (loss=%.4f, data=%d)",
                    version, meta["train_loss"], data["total"])
        return {"version": version, "version_dir": str(version_dir),
                "data_count": data["total"], "base_model": base_dir,
                "train_loss": meta["train_loss"]}

    @staticmethod
    def _load_base_model(transformers: Any, base_dir: str,
                         bnb_config: Any, attn_impl: str = "") -> Any:
        """按可用 AutoClass 加载 4bit 基座（CausalLM → ImageTextToText → AutoModel）。

        attn_impl（TASK-053）：非空时透传 attn_implementation；
        指定实现加载失败自动降级 sdpa → 不指定（模型默认），保证可训练性。
        """
        attn_chain: list[str] = []
        if attn_impl:
            attn_chain = [attn_impl] + [a for a in ("sdpa", "")
                                        if a != attn_impl]
        else:
            attn_chain = [""]
        errors: list[str] = []
        for attn in attn_chain:
            for cls_name in ("AutoModelForCausalLM",
                             "AutoModelForImageTextToText", "AutoModel"):
                cls = getattr(transformers, cls_name, None)
                if cls is None:
                    continue
                kwargs: dict[str, Any] = dict(
                    quantization_config=bnb_config,
                    device_map="auto", trust_remote_code=True)
                if attn:
                    kwargs["attn_implementation"] = attn
                try:
                    model = cls.from_pretrained(base_dir, **kwargs)
                    if attn:
                        logger.info("基座注意力实现: %s (%s)", attn, cls_name)
                    return model
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{cls_name}[{attn or 'default'}]: {exc}")
            if attn:
                logger.warning("注意力实现 %s 加载失败，尝试降级", attn)
        raise TrainingFailed("基座模型加载失败 → " + " | ".join(errors))

    @staticmethod
    def _tokenize(tokenizer: Any, samples: list[dict],
                  max_seq: int) -> list[dict]:
        """样本 → input_ids/labels（prompt 段 labels=-100 不参与 loss）。"""
        feats: list[dict] = []
        for s in samples:
            prompt = (f"指令：{s['instruction']}\n输入：{s['input']}\n回答："
                      if s["input"] else f"指令：{s['instruction']}\n回答：")
            full = prompt + s["output"]
            p_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
            f_ids = tokenizer(full, add_special_tokens=False)["input_ids"][:max_seq]
            cut = min(len(p_ids), len(f_ids))
            labels = [-100] * cut + f_ids[cut:]
            feats.append({"input_ids": f_ids, "labels": labels,
                          "attention_mask": [1] * len(f_ids)})
        return feats

    @staticmethod
    def _make_collator(tokenizer: Any) -> Callable[[list[dict]], dict]:
        """简易 collator：按 batch 内最长序列 padding。"""
        pad_id = tokenizer.pad_token_id or 0

        def collate(batch: list[dict]) -> dict:
            import torch as _torch
            max_len = max(len(b["input_ids"]) for b in batch)
            input_ids, labels, attn = [], [], []
            for b in batch:
                pad = max_len - len(b["input_ids"])
                input_ids.append(b["input_ids"] + [pad_id] * pad)
                labels.append(b["labels"] + [-100] * pad)
                attn.append(b["attention_mask"] + [0] * pad)
            return {
                "input_ids": _torch.tensor(input_ids, dtype=_torch.long),
                "labels": _torch.tensor(labels, dtype=_torch.long),
                "attention_mask": _torch.tensor(attn, dtype=_torch.long),
            }
        return collate

    @staticmethod
    def _loss_callback(progress_cb: Callable[[dict], None] | None) -> Any:
        """loss 实时回调（transformers.TrainerCallback.on_log）。"""
        from transformers import TrainerCallback  # type: ignore

        class _Cb(TrainerCallback):
            def on_log(self, args: Any, state: Any, control: Any,
                       logs: dict | None = None, **kw: Any) -> None:
                if not logs or "loss" not in logs or progress_cb is None:
                    return
                try:
                    progress_cb({"loss": float(logs["loss"]),
                                 "epoch": logs.get("epoch"),
                                 "step": state.global_step,
                                 "max_steps": max(1, state.max_steps)})
                except Exception:  # noqa: BLE001
                    pass
        return _Cb()

    @staticmethod
    def _cache_cleanup_callback(torch: Any) -> Any:
        """每 epoch 结束清理 torch 显存缓存（配置可控）。"""
        from transformers import TrainerCallback  # type: ignore

        class _Cb(TrainerCallback):
            def on_epoch_end(self, args: Any, state: Any, control: Any,
                             **kw: Any) -> None:
                gc.collect()
                try:
                    torch.cuda.empty_cache()
                except Exception:  # noqa: BLE001
                    pass
        return _Cb()

    @staticmethod
    def _throttle_callback(throttle_ms: int) -> Any:
        """每个 optimizer step 后 sleep，降低 GPU 占空比（资源约束限频）。"""
        from transformers import TrainerCallback  # type: ignore

        class _Cb(TrainerCallback):
            def on_step_end(self, args: Any, state: Any, control: Any,
                            **kw: Any) -> None:
                time.sleep(throttle_ms / 1000.0)
        return _Cb()

    def _cancel_callback(self, task_id: str) -> Any:
        """强制取消回调：每个 optimizer step 边界检测取消标志。

        置 control.should_training_stop=True 是 HF Trainer 官方中断通道，
        训练循环在当前 step 完成后干净退出（不撕裂 CUDA 上下文）。
        """
        from transformers import TrainerCallback  # type: ignore

        svc = self

        class _Cb(TrainerCallback):
            def on_step_end(self, args: Any, state: Any, control: Any,
                            **kw: Any) -> None:
                if svc._is_cancelled(task_id):
                    control.should_training_stop = True
                    logger.info("训练循环收到取消信号，将于本步后停止: %s", task_id)
        return _Cb()

    # ═══════════════════════════════════════════════════════════
    #  质量评估（TASK-038 evaluate）
    # ═══════════════════════════════════════════════════════════

    def evaluate(self, version: str) -> dict:
        """验证集困惑度 + 样本生成质量打分。

        通过（quality_score ≥ 0.5）→ meta.status=registered（由 _run_task
        置为 current）；未通过 → 保留旧版，本版标记 pending_review。
        依赖/基座缺失时返回 passed=False 且不崩溃。
        """
        version_dir = LORA_DIR / version
        meta = self._read_meta(version_dir)
        if meta is None:
            return {"version": version, "passed": False,
                    "error": f"版本不存在: {version}"}

        torch = _try_import("torch")
        transformers = _try_import("transformers")
        peft = _try_import("peft")
        # 审计 09-10 P1-14 残留：优先级倒置修正——_last_train_data 是
        # train() 记录的「本次训练实际使用的数据集」（外部 JSONL 场景的
        # 唯一正确来源），必须优先于 _dataset（知识库自动构建缓存，
        # 可能属于完全无关的语料）；此前外部训练版本被旧自动集顶掉
        data = self._last_train_data or self._dataset or {}
        valid = data.get("valid", [])
        base_dir = meta.get("base_model") or str(DEFAULT_BASE_MODEL)
        if (torch is None or transformers is None or peft is None
                or not valid or not Path(base_dir).is_dir()
                or not torch.cuda.is_available()):
            meta.update({"quality_score": None, "status": "pending_review",
                         "eval_error": "评估依赖/数据/基座不可用"})
            self._write_meta(version_dir, meta)
            return {"version": version, "passed": False,
                    "quality_score": None,
                    "error": "评估依赖/数据/基座不可用，标记待审核"}

        try:
            bnb_config = transformers.BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_use_double_quant=True)
            tokenizer = transformers.AutoTokenizer.from_pretrained(
                base_dir, trust_remote_code=True)
            base_model = self._load_base_model(transformers, base_dir,
                                               bnb_config)
            model = peft.PeftModel.from_pretrained(
                base_model, str(version_dir))
            model.eval()

            ppl = self._eval_perplexity(torch, tokenizer, model, valid)
            gen_score = self._eval_generation(torch, tokenizer, model, valid)
            # 综合分：生成质量 70% + 困惑度得分 30%（ppl 越低越好，ppl=1→1.0）
            ppl_score = 1.0 / max(1.0, ppl)
            quality = round(0.7 * gen_score + 0.3 * ppl_score, 4)
            passed = quality >= EVAL_PASS_SCORE

            meta.update({"quality_score": quality, "perplexity": round(ppl, 4),
                         "gen_score": round(gen_score, 4),
                         "status": "registered" if passed else "pending_review"})
            self._write_meta(version_dir, meta)

            del model, base_model
            gc.collect()
            try:
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            logger.info("版本 %s 评估: quality=%.3f ppl=%.2f → %s",
                        version, quality, ppl,
                        "registered" if passed else "pending_review")
            return {"version": version, "passed": passed,
                    "quality_score": quality, "perplexity": ppl}
        except Exception as exc:  # noqa: BLE001 - 评估失败不崩溃
            logger.error("版本 %s 评估异常: %s", version, exc)
            meta.update({"status": "pending_review", "eval_error": str(exc)})
            self._write_meta(version_dir, meta)
            return {"version": version, "passed": False, "error": str(exc)}

    @staticmethod
    def _eval_perplexity(torch: Any, tokenizer: Any, model: Any,
                         valid: list[dict]) -> float:
        """验证集困惑度：对 output 文本计算 exp(平均 NLL)（最多 32 条）。"""
        import math
        losses: list[float] = []
        with torch.no_grad():
            for s in valid[:EVAL_PPL_MAX_SAMPLES]:
                text = s.get("output", "")
                if not text.strip():
                    continue
                ids = tokenizer(text, return_tensors="pt",
                                truncation=True, max_length=512)
                ids = {k: v.to(model.device) for k, v in ids.items()}
                out = model(**ids, labels=ids["input_ids"])
                if out.loss is not None:
                    losses.append(float(out.loss.item()))
        if not losses:
            return float("inf")
        return math.exp(sum(losses) / len(losses))

    @staticmethod
    def _eval_generation(torch: Any, tokenizer: Any, model: Any,
                         valid: list[dict]) -> float:
        """样本生成质量启发式打分（0..1）：非空/长度/多样性/不回显 prompt。"""
        if not valid:
            return 0.0
        scores: list[float] = []
        with torch.no_grad():
            for s in valid[:EVAL_GEN_SAMPLES]:
                prompt = (f"指令：{s['instruction']}\n输入：{s['input']}\n回答："
                          if s.get("input")
                          else f"指令：{s['instruction']}\n回答：")
                ids = tokenizer(prompt, return_tensors="pt").to(model.device)
                out = model.generate(**ids, max_new_tokens=64,
                                     do_sample=False,
                                     pad_token_id=tokenizer.pad_token_id)
                text = tokenizer.decode(
                    out[0][ids["input_ids"].shape[1]:],
                    skip_special_tokens=True).strip()
                score = 0.0
                if text:
                    score += 0.3
                if len(text) >= 20:
                    score += 0.2
                grams = {text[i:i + 4] for i in range(max(len(text) - 3, 1))}
                if len(grams) / max(len(text), 1) > 0.4:
                    score += 0.3
                if prompt[-30:] not in text:
                    score += 0.2
                scores.append(score)
        return sum(scores) / len(scores) if scores else 0.0

    # ── §4.3 自适应：连续质量下降检测 ────────────────────────────────

    def check_quality_decline_pause(self) -> bool:
        """最近 QUALITY_DECLINE_WINDOW 次完成训练的 quality_score 连续
        QUALITY_DECLINE_STREAK 次下降（score[i] > score[i+1]）→ True。

        质量分来源：各版本 meta.json 的 quality_score（evaluate() 评估后
        回填；None 表示未成功评估，不计入窗口）。质量数据不足窗口长度时
        返回 False（不暂停）。异常时同样返回 False，不阻断训练链路。
        """
        try:
            scores = [float(v["quality_score"])
                      for v in self.list_versions()
                      if v.get("quality_score") is not None]
        except Exception:  # noqa: BLE001 - 检测失败不暂停
            return False
        recent = scores[-QUALITY_DECLINE_WINDOW:]
        if len(recent) < QUALITY_DECLINE_WINDOW:
            return False
        # 相邻对比较：recent 与 recent[1:] 长度差 1 属预期，显式 strict=False
        declines = sum(1 for a, b in zip(recent, recent[1:], strict=False) if a > b)
        if declines >= QUALITY_DECLINE_STREAK:
            logger.warning("§4.3 自适应：最近 %d 次训练质量分 %s 连续 %d 次下降",
                           len(recent), recent, declines)
            return True
        return False

    # ═══════════════════════════════════════════════════════════
    #  版本管理（TASK-038：list_versions/rollback/merge/get_current）
    # ═══════════════════════════════════════════════════════════

    def list_versions(self) -> list[dict]:
        """列出全部版本（按版本号升序，含 meta.json 内容）。"""
        LORA_DIR.mkdir(parents=True, exist_ok=True)
        versions: list[dict] = []
        for d in sorted(LORA_DIR.iterdir()):
            if not (d.is_dir() and d.name.startswith("v")
                    and d.name[1:].isdigit()):
                continue
            meta = self._read_meta(d) or {}
            versions.append({
                "version": d.name,
                "path": str(d),
                "has_adapter": (d / "adapter_model.bin").is_file()
                or (d / "adapter_model.safetensors").is_file(),
                "has_config": (d / "adapter_config.json").is_file(),
                **meta,
            })
        versions.sort(key=lambda v: int(v["version"][1:]))
        return versions

    def get_current(self) -> str:
        """当前生效版本（current.json 指针）；无则回退最新已注册版本。"""
        try:
            if CURRENT_FILE.is_file():
                data = json.loads(CURRENT_FILE.read_text(encoding="utf-8"))
                current = data.get("current", "")
                if current and (LORA_DIR / current).is_dir():
                    return current
        except (OSError, json.JSONDecodeError):
            pass
        registered = [v["version"] for v in self.list_versions()
                      if v.get("status") == "registered"]
        return registered[-1] if registered else ""

    def rollback(self, version: str) -> bool:
        """回滚到指定版本（置 current 指针）。版本不存在返回 False。

        审计 09-10 P1-5 残留收口：current 指针只允许指向**本服务注册过
        的版本白名单**——此前仅查目录存在，`../` 或手建目录名可直接写进
        指针，下游按 current 拼路径即越出 LORA_DIR（与 delete_version
        白名单同规矩，无 rmtree 危害但同样拒绝指针污染）。
        """
        known = {v["version"] for v in self.list_versions()}
        if version not in known or not (LORA_DIR / version).is_dir():
            logger.warning("回滚目标版本不在注册白名单: %s", version)
            return False
        self._set_current(version)
        _broadcast("lora_rollback", {"version": version})
        logger.info("LoRA 回滚到 %s", version)
        return True

    def merge(self, v1: str, v2: str) -> str | None:
        """合并两个版本 adapter（逐张量平均），保存为新版本并返回版本号。

        依赖 torch；adapter 文件缺失或 torch 不可用时返回 None（不崩溃）。
        """
        torch = _try_import("torch")
        d1, d2 = LORA_DIR / v1, LORA_DIR / v2
        if torch is None or not d1.is_dir() or not d2.is_dir():
            return None
        f1 = self._adapter_file(d1)
        f2 = self._adapter_file(d2)
        if f1 is None or f2 is None:
            return None
        try:
            sd1 = self._load_adapter(torch, f1)
            sd2 = self._load_adapter(torch, f2)
            merged: dict[str, Any] = {}
            for k, t1 in sd1.items():
                t2 = sd2.get(k)
                merged[k] = t1 if t2 is None else (t1.float() + t2.float()) / 2
            version_dir, version = self._next_version_dir()
            if f1.suffix == ".safetensors":
                from safetensors.torch import save_file  # type: ignore
                save_file(merged, str(version_dir / "adapter_model.safetensors"))
            else:
                torch.save(merged, str(version_dir / "adapter_model.bin"))
            # adapter_config 继承 v2（较新），meta 标注合并来源
            cfg_src = d2 / "adapter_config.json"
            if cfg_src.is_file():
                (version_dir / "adapter_config.json").write_text(
                    cfg_src.read_text(encoding="utf-8"), encoding="utf-8")
            meta = {
                "version": version, "created_at": time.time(),
                "quality_score": None, "status": "pending_review",
                "data_count": 0,
                "base_model": (self._read_meta(d2) or {}).get(
                    "base_model", str(DEFAULT_BASE_MODEL)),
                "merged_from": [v1, v2],
            }
            self._write_meta(version_dir, meta)
            self._prune_versions()
            logger.info("LoRA 合并完成: %s + %s → %s", v1, v2, version)
            return version
        except Exception as exc:  # noqa: BLE001 - 合并失败不崩溃
            logger.error("LoRA 合并失败: %s + %s: %s", v1, v2, exc)
            return None

    # ── 版本管理内部工具 ─────────────────────────────────────────

    @staticmethod
    def _read_meta(version_dir: Path) -> dict | None:
        try:
            p = version_dir / "meta.json"
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        return None

    @staticmethod
    def _write_meta(version_dir: Path, meta: dict) -> None:
        version_dir.mkdir(parents=True, exist_ok=True)
        (version_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _adapter_file(version_dir: Path) -> Path | None:
        for name in ("adapter_model.bin", "adapter_model.safetensors"):
            p = version_dir / name
            if p.is_file():
                return p
        return None

    @staticmethod
    def _load_adapter(torch: Any, path: Path) -> dict:
        if path.suffix == ".safetensors":
            from safetensors.torch import load_file  # type: ignore
            return load_file(str(path))
        # B0（2026-09-13）：weights_only=True——拒绝含任意 pickle 对象
        # 的权重文件（恶意模型包 RCE 链收口）；自训产物均为纯 state_dict
        return torch.load(str(path), map_location="cpu", weights_only=True)

    def _next_version_dir(self) -> tuple[Path, str]:
        """分配下一个版本目录（models/lora/v{N+1}）。"""
        LORA_DIR.mkdir(parents=True, exist_ok=True)
        max_n = 0
        for d in LORA_DIR.iterdir():
            if d.is_dir() and d.name.startswith("v") and d.name[1:].isdigit():
                max_n = max(max_n, int(d.name[1:]))
        version = f"v{max_n + 1}"
        version_dir = LORA_DIR / version
        version_dir.mkdir(parents=True, exist_ok=True)
        return version_dir, version

    def _set_current(self, version: str) -> None:
        """写入 current.json 指针。"""
        LORA_DIR.mkdir(parents=True, exist_ok=True)
        CURRENT_FILE.write_text(
            json.dumps({"current": version, "updated_at": time.time()},
                       ensure_ascii=False, indent=2), encoding="utf-8")

    def _prune_versions(self) -> None:
        """最多保留 MAX_VERSIONS 版（不删除 current 指针指向的版本）。"""
        versions = self.list_versions()
        if len(versions) <= MAX_VERSIONS:
            return
        current = self.get_current()
        victims = [v for v in versions if v["version"] != current]
        victims.sort(key=lambda v: v.get("created_at", 0))
        import shutil
        for v in victims[:len(versions) - MAX_VERSIONS]:
            try:
                shutil.rmtree(v["path"], ignore_errors=True)
                logger.info("清理旧 LoRA 版本: %s", v["version"])
            except OSError as exc:
                logger.warning("清理版本失败 %s: %s", v["version"], exc)

    # ═══════════════════════════════════════════════════════════
    #  状态查询
    # ═══════════════════════════════════════════════════════════

    def get_status(self) -> dict:
        """训练服务状态快照。"""
        with self._state_lock:
            active = self._training_task_id
        data = self._dataset
        return {
            "active_task": active,
            "queue_size": self._queue.qsize(),
            "feature_lock": get_feature_lock().active_feature,
            "current_version": self.get_current(),
            "versions": self.list_versions(),
            "dataset": {
                "total": data["total"] if data else 0,
                "sources": data["sources"] if data else {},
                "sufficient": data["sufficient"] if data else False,
            },
            "min_samples": MIN_TRAINING_SAMPLES,
        }


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

def get_lora_training_service() -> LoRATrainingService:
    """获取 LoRA 训练服务单例。"""
    return LoRATrainingService.instance()
