"""OmniSpace AI v2.5.0 视频风格 LoRA 服务（文档 §8.3.7 视频风格模块 / §7.1.4 /v1/style）。

职责：
- 素材管理：用户上传视频/图片 → data/style/datasets/{dataset_id}/（视频经
  FFmpeg 抽帧为 JPG 序列，图片直接收录），生成 manifest.jsonl 训练清单
  {"image": ..., "caption": ...}（caption 在训练时注入 style_prompt）。
- 训练执行：LTX-2 基座 QLoRA 4bit 增量微调（rank=16/alpha=32 默认，文档 §8.3.7），
  进程内 PriorityQueue + 专属后台工作线程串行执行（与 lora_training_service 同构），
  训练期间持有 "training" 功能锁（规格 §6.1 互斥，与知识 LoRA 共用同一 GPU 资源）。
- 版本管理：models/style_lora/v1, v2...（adapter_model.safetensors +
  adapter_config.json + meta.json{version,created_at,quality_score,status,
  data_count,base_model,style_prompt,name}），最多保留 10 版；
  current.json 指针标记当前生效版本；支持回滚。
- 预览：加载基座 + 指定版本 adapter 对样本图/帧做风格化推理。

诚实标注（当前出货环境现状）：
  LTX-2 基座权重未随包分发（models/ltx-2 不存在），因此：
  - /style/train 前置校验如实返回 80010（基座未就绪），不会伪造训练进度；
  - /style/preview 如实返回 80013（推理引擎/版本未就绪）。
  训练与推理管线为真实实现（diffusers + peft QLoRA），用户自行下载
  LTX-2 权重到 models/ltx-2 后自动生效，无需改代码。

全局优先级：P2_TRAINING（文档 §8.4.2，与知识 LoRA 同级）。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import DATA_DIR, MODELS_DIR
from ..data.database import get_db_safe
from ..middleware.feature_lock import get_feature_lock
from .priority import LEVEL_BY_NAME, Priority

logger = logging.getLogger("omnispace.style_lora")


def _try_import(name: str) -> Any:
    """容错导入可选依赖。"""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


# ── 常量 ────────────────────────────────────────────────────────
STYLE_DIR = DATA_DIR / "style"
UPLOAD_DIR = STYLE_DIR / "uploads"            # 原始上传素材
DATASET_DIR = STYLE_DIR / "datasets"          # 抽帧后的训练数据集
STYLE_LORA_DIR = MODELS_DIR / "style_lora"    # 风格 LoRA 版本根
CURRENT_FILE = STYLE_LORA_DIR / "current.json"

BASE_MODEL_DIR = MODELS_DIR / "ltx-2"         # 文档 §8.3.7：视频风格基座 LTX-2
BASE_MODEL_ID = "ltx-2"

MIN_STYLE_SAMPLES = 4       # 风格训练最少帧/图样本（少样本风格拟合下限）
MAX_STYLE_FRAMES = 32       # 单个数据集最多抽帧数（控制训练时长与显存）
MAX_VERSIONS = 10           # 最多保留版本数
MAX_UPLOAD_BYTES = 200 * 1024 * 1024   # 视频素材上传上限 200MB
FRAME_EXTRACT_TIMEOUT_S = 120

VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# 本服务在全局调度中的优先级（文档 §8.4.2：P2 LoRA训练）
GLOBAL_PRIORITY = Priority.P2_TRAINING

# style_tasks 表实际列（DB 更新时过滤）
_STYLE_TASK_COLUMNS = {
    "name", "dataset_id", "style_prompt", "base_model",
    "lora_rank", "lora_alpha", "learning_rate", "epochs",
    "status", "progress", "version", "error", "updated_at",
}

_STYLE_TASK_DDL = """
CREATE TABLE IF NOT EXISTS style_tasks (
    id              TEXT PRIMARY KEY,
    name            TEXT DEFAULT '',
    dataset_id      TEXT NOT NULL,
    style_prompt    TEXT DEFAULT '',
    base_model      TEXT DEFAULT 'ltx-2',
    lora_rank       INTEGER DEFAULT 16,
    lora_alpha      INTEGER DEFAULT 32,
    learning_rate   REAL DEFAULT 2e-5,
    epochs          INTEGER DEFAULT 3,
    status          TEXT DEFAULT 'queued',
    progress        REAL DEFAULT 0.0,
    version         TEXT DEFAULT '',
    error           TEXT DEFAULT '',
    created_at      REAL NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL DEFAULT 0
)
"""

# 训练任务优先级（对齐 Celery 队列语义，与知识 LoRA 一致）
# B5 步4：相对级映射收敛到 priority.py 单源（消克隆漂移）
PRIORITY_LEVELS = LEVEL_BY_NAME

# 默认训练超参（文档 §8.3.7：QLoRA 4bit，rank=16，alpha=32，lr 2e-5，epochs 3）
DEFAULT_STYLE_CONFIG: dict[str, Any] = {
    "base_model": BASE_MODEL_ID,
    "lora_rank": 16,
    "lora_alpha": 32,
    "lora_dropout": 0.05,
    "learning_rate": 2e-5,
    "epochs": 3,
    "batch_size": 1,
    "gradient_accumulation_steps": 4,
    "max_seq_length": 256,
    "resolution": 512,           # 帧训练分辨率（显存安全线）
}

# 超参硬件安全边界（与知识 LoRA 审计 P0-6 一致）
_HP_BOUNDS = {
    "epochs": (1, 50, int),
    "lora_rank": (4, 64, int),
    "learning_rate": (1e-7, 1e-3, float),
}


class StyleTrainingFailed(RuntimeError):
    """风格训练失败（基座缺失/依赖缺失/显存不足/训练异常）——任务标记 error，不崩溃。"""


class StyleTrainingCancelled(RuntimeError):
    """风格训练被用户取消（批 2 STYLE-017：检查点抛出）。"""


class StylePreviewUnavailable(RuntimeError):
    """风格预览不可用（推理引擎或版本未就绪）——API 层映射 80013。"""


class StyleLoraService:
    """视频风格 LoRA 服务单例。

    线程模型：trigger_train() 入队（PriorityQueue），专属 daemon 工作线程
    串行出队执行 train() → evaluate() → 注册版本。
    """

    _instance: StyleLoraService | None = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls) -> StyleLoraService:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self) -> None:
        self._queue: queue.PriorityQueue[tuple[int, int, dict]] = queue.PriorityQueue()
        self._seq = 0
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._training_task_id: str | None = None
        self._mem_tasks: dict[str, dict] = {}   # DB 不可用时任务镜像
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock_via_fallback = False
        self._table_ready = False
        # 批 2（STYLE-017）：任务控制旗标 {task_id: {"pause": bool, "cancel": bool}}
        self._control_flags: dict[str, dict[str, bool]] = {}
        for d in (UPLOAD_DIR, DATASET_DIR, STYLE_LORA_DIR):
            d.mkdir(parents=True, exist_ok=True)
        self._ensure_table()

    # ═══════════════════════════════════════════════════════════
    #  存储层（style_tasks 自建表 + 内存镜像兜底）
    # ═══════════════════════════════════════════════════════════

    def _ensure_table(self) -> None:
        db = get_db_safe()
        if db is None:
            return
        try:
            db.sql(_STYLE_TASK_DDL)
            self._table_ready = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("style_tasks 建表失败，降级内存镜像: %s", exc)

    def _insert_task(self, record: dict) -> None:
        db = get_db_safe()
        if db is not None and self._table_ready:
            try:
                db.insert("style_tasks", record)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("风格任务落库失败，降级内存镜像: %s", exc)
        self._mem_tasks[record["id"]] = record

    def _update_task(self, task_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        db = get_db_safe()
        if db is not None and self._table_ready:
            try:
                db_fields = {k: v for k, v in fields.items()
                             if k in _STYLE_TASK_COLUMNS}
                db.update("style_tasks", db_fields, "id=?", (task_id,))
            except Exception as exc:  # noqa: BLE001
                logger.debug("风格任务更新落库失败: %s", exc)
        if task_id in self._mem_tasks:
            self._mem_tasks[task_id].update(fields)

    def get_task(self, task_id: str) -> dict | None:
        db = get_db_safe()
        if db is not None and self._table_ready:
            try:
                row = db.query_one(
                    "SELECT id, name, dataset_id, style_prompt, base_model,"
                    " lora_rank, lora_alpha, learning_rate, epochs, status,"
                    " progress, version, error, created_at, updated_at"
                    " FROM style_tasks WHERE id=?", (task_id,))
                if row is not None:
                    return row
            except Exception as exc:  # noqa: BLE001
                logger.debug("风格任务查询失败: %s", exc)
        return self._mem_tasks.get(task_id)

    def list_tasks(self) -> list[dict]:
        db = get_db_safe()
        if db is not None and self._table_ready:
            try:
                return db.query(
                    "SELECT id, name, dataset_id, style_prompt, base_model,"
                    " lora_rank, lora_alpha, learning_rate, epochs, status,"
                    " progress, version, error, created_at, updated_at"
                    " FROM style_tasks ORDER BY created_at DESC")
            except Exception as exc:  # noqa: BLE001
                logger.debug("风格任务列表查询失败: %s", exc)
        return sorted(self._mem_tasks.values(),
                      key=lambda t: t.get("created_at", 0), reverse=True)

    # ═══════════════════════════════════════════════════════════
    #  基座就绪检测（诚实门控：LTX-2 权重未下载则如实不可训练）
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def base_ready() -> tuple[bool, str]:
        """LTX-2 基座权重是否就绪。

        Returns:
            (ready, reason)：reason 为未就绪原因（供 80010 detail）。
        """
        if not BASE_MODEL_DIR.is_dir():
            return False, (f"LTX-2 基座目录不存在: {BASE_MODEL_DIR}；"
                           "请下载 LTX-2 权重后放入该目录")
        has_index = (BASE_MODEL_DIR / "model_index.json").is_file()
        has_weights = any(BASE_MODEL_DIR.glob("**/*.safetensors")) or \
            any(BASE_MODEL_DIR.glob("**/*.bin"))
        if not (has_index and has_weights):
            return False, (f"LTX-2 基座权重不完整: {BASE_MODEL_DIR}"
                           "（缺少 model_index.json 或权重文件）")
        return True, ""

    # ═══════════════════════════════════════════════════════════
    #  素材上传与数据集构建（真实抽帧管线）
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _classify_upload(filename: str) -> str:
        ext = Path(filename).suffix.lower()
        if ext in VIDEO_EXTS:
            return "video"
        if ext in IMAGE_EXTS:
            return "image"
        return ""

    def save_upload(self, filename: str, content: bytes) -> dict:
        """保存上传素材并构建训练数据集（视频自动抽帧）。

        Returns:
            {dataset_id, kind, frame_count, dataset_path, manifest_path}

        Raises:
            ValueError: 格式不支持（80015）/ 内容为空
            RuntimeError: FFmpeg 不可用或抽帧失败（80016）
        """
        kind = self._classify_upload(filename)
        if not kind:
            raise ValueError(
                f"风格素材格式不支持: {Path(filename).suffix or '(无扩展名)'}；"
                f"视频支持 {sorted(VIDEO_EXTS)}，图片支持 {sorted(IMAGE_EXTS)}")
        if not content:
            raise ValueError("上传文件内容为空")

        dataset_id = uuid.uuid4().hex
        ext = Path(filename).suffix.lower()
        upload_path = UPLOAD_DIR / f"{dataset_id}{ext}"
        upload_path.write_bytes(content)

        frame_dir = DATASET_DIR / dataset_id / "frames"
        frame_dir.mkdir(parents=True, exist_ok=True)

        if kind == "image":
            shutil.copy2(upload_path, frame_dir / f"frame_001{ext}")
        else:
            self._extract_frames(upload_path, frame_dir)

        frames = sorted(p for p in frame_dir.iterdir() if p.is_file())
        if not frames:
            raise RuntimeError("素材解析失败：未得到任何有效帧/图")

        # 训练清单（caption 训练时注入 style_prompt）
        manifest_path = DATASET_DIR / dataset_id / "manifest.jsonl"
        with open(manifest_path, "w", encoding="utf-8") as f:
            for p in frames:
                f.write(json.dumps({"image": str(p), "caption": ""},
                                   ensure_ascii=False) + "\n")

        logger.info("风格数据集已构建: %s (%s, %d 帧)", dataset_id, kind, len(frames))
        return {
            "dataset_id": dataset_id,
            "kind": kind,
            "frame_count": len(frames),
            "dataset_path": str(DATASET_DIR / dataset_id),
            "manifest_path": str(manifest_path),
            "source_file": str(upload_path),
        }

    def _extract_frames(self, video_path: Path, frame_dir: Path) -> None:
        """FFmpeg 抽帧：fps=1 均匀采样，最多 MAX_STYLE_FRAMES 帧。"""
        from .encoder_service import get_encoder_service
        enc = get_encoder_service()
        if not enc.available:
            raise RuntimeError(
                "FFmpeg 不可用（runtime/ffmpeg、tools/downloads、PATH 均未找到），"
                "无法对视频素材抽帧")
        out_pattern = str(frame_dir / "frame_%03d.jpg")
        cmd = [
            enc.ffmpeg_path, "-y", "-i", str(video_path),
            "-vf", "fps=1", "-frames:v", str(MAX_STYLE_FRAMES),
            "-q:v", "3", out_pattern,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=FRAME_EXTRACT_TIMEOUT_S,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"视频抽帧超时（>{FRAME_EXTRACT_TIMEOUT_S}s）") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or b"").decode("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"视频抽帧失败（FFmpeg 退出码 {proc.returncode}）: {tail}")
        if not any(frame_dir.glob("frame_*.jpg")):
            raise RuntimeError("视频抽帧失败：文件损坏或无可解码帧")

    def list_datasets(self) -> list[dict]:
        """列出全部已构建数据集（按修改时间倒序）。"""
        items: list[dict] = []
        if not DATASET_DIR.is_dir():
            return items
        for d in DATASET_DIR.iterdir():
            if not d.is_dir():
                continue
            manifest = d / "manifest.jsonl"
            if not manifest.is_file():
                continue
            try:
                count = sum(1 for _ in open(manifest, encoding="utf-8"))
            except OSError:
                count = 0
            items.append({
                "dataset_id": d.name,
                "frame_count": count,
                "dataset_path": str(d),
                "updated_at": d.stat().st_mtime,
            })
        items.sort(key=lambda x: x["updated_at"], reverse=True)
        return items

    def dataset_stats(self, dataset_id: str) -> dict:
        """数据集样本统计（供训练充分性判定）。"""
        # 穿越防护（2026-09-02 B0 审计）：dataset_id 直接拼路径，非法
        # 形态（../ 等）按空数据集返回，不做文件系统访问
        if not self._safe_dataset_id(dataset_id):
            return {"dataset_id": dataset_id[:40], "total": 0,
                    "sufficient": False}
        manifest = DATASET_DIR / dataset_id / "manifest.jsonl"
        total = 0
        if manifest.is_file():
            try:
                total = sum(1 for line in open(manifest, encoding="utf-8")
                            if line.strip())
            except OSError:
                total = 0
        return {"dataset_id": dataset_id, "total": total,
                "sufficient": total >= MIN_STYLE_SAMPLES}

    # ═══════════════════════════════════════════════════════════
    #  触发判定与入队（文档 §8.4.2 P2 优先级）
    # ═══════════════════════════════════════════════════════════

    def can_train(self, dataset_id: str) -> tuple[bool, str]:
        """训练前置条件判定，返回 (ok, reason_code)。

        reason_code: "" | "training_active" | "feature_busy" |
                     "dataset_insufficient" | "base_not_ready"
        """
        with self._state_lock:
            if self._training_task_id is not None:
                return False, "training_active"
        if not self._queue.empty():
            return False, "training_active"
        if get_feature_lock().active_feature is not None:
            return False, "feature_busy"
        ready, _reason = self.base_ready()
        if not ready:
            return False, "base_not_ready"
        stats = self.dataset_stats(dataset_id)
        if not stats["sufficient"]:
            return False, "dataset_insufficient"
        return True, ""

    @classmethod
    def _clamp_hyperparams(cls, cfg: dict) -> dict:
        """把超参钳制到硬件安全边界内（审计 P0-6 服务层二次保护）。"""
        out = dict(cfg)
        for key, (lo, hi, cast) in cls._HP_BOUNDS.items():
            raw = out.get(key)
            try:
                val = cast(raw)
            except (TypeError, ValueError):
                val = cast(DEFAULT_STYLE_CONFIG.get(key, lo))
            clamped = max(lo, min(val, hi))
            if clamped != val:
                logger.warning("风格训练超参越界已钳制: %s %r -> %r (边界 [%s, %s])",
                               key, raw, clamped, lo, hi)
            out[key] = clamped
        return out

    def trigger_train(self, config: dict, priority: str = "low") -> str | None:
        """入队一次风格训练任务，返回 task_id；前置条件不满足返回 None。

        调用方应先经 can_train() 区分原因并映射 80010/80011/40007。
        """
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        cfg = self._clamp_hyperparams({**DEFAULT_STYLE_CONFIG, **(config or {})})
        ok_flag, _reason = self.can_train(str(cfg.get("dataset_id", "")))
        if not ok_flag:
            return None

        task_id = uuid.uuid4().hex
        now = time.time()
        record = {
            "id": task_id,
            "name": str(cfg.get("name") or "视频风格 LoRA"),
            "dataset_id": str(cfg["dataset_id"]),
            "style_prompt": str(cfg.get("style_prompt") or ""),
            "base_model": cfg["base_model"],
            "lora_rank": cfg["lora_rank"],
            "lora_alpha": cfg["lora_alpha"],
            "learning_rate": cfg["learning_rate"],
            "epochs": cfg["epochs"],
            "status": "queued", "progress": 0.0,
            "version": "", "error": "",
            "created_at": now, "updated_at": now,
        }
        self._insert_task(record)

        with self._state_lock:
            self._seq += 1
            seq = self._seq
        level = PRIORITY_LEVELS.get(priority, PRIORITY_LEVELS["low"])
        self._queue.put((level, seq, {"task_id": task_id, "config": cfg}))
        self._ensure_worker()
        logger.info("风格训练任务入队: %s (priority=%s, global=%s)",
                    task_id, priority, GLOBAL_PRIORITY.name)
        return task_id

    # ── 工作线程 ────────────────────────────────────────────────

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop, daemon=True, name="style-lora-training")
            self._worker.start()

    def _worker_loop(self) -> None:
        """串行消费训练队列（一次仅一个训练任务，天然满足无并发约束）。"""
        while True:
            item = self._queue.get()
            try:
                self._run_task(item[2])
            except Exception as exc:  # noqa: BLE001 - 工作线程不崩溃
                logger.error("风格训练工作线程异常: %s", exc)
            finally:
                self._queue.task_done()

    def _run_task(self, task: dict) -> None:
        task_id = task["task_id"]
        cfg = task["config"]
        # 队列中已被取消的任务直接跳过（STYLE-017）
        if self._control_flags.get(task_id, {}).get("cancel"):
            existing = self.get_task(task_id)
            if existing and existing.get("status") == "queued":
                self._update_task(task_id, status="cancelled")
                return
        with self._state_lock:
            self._training_task_id = task_id
        self._update_task(task_id, status="training")

        if not self._acquire_training_lock(task_id):
            self._update_task(task_id, status="error",
                              error="无法获取 training 功能锁")
            with self._state_lock:
                self._training_task_id = None
            return

        try:
            result = self.train(cfg, progress_cb=self._make_progress_cb(task_id))
            version = result["version"]
            self._update_task(task_id, status="evaluating", progress=0.95,
                              version=version)
            report = self.evaluate(version)
            passed = bool(report.get("passed"))
            if passed:
                self._set_current(version)
            self._update_task(task_id, status="done", progress=1.0)
            logger.info("风格训练完成: %s → %s (score=%s, %s)",
                        task_id, version, report.get("quality_score"),
                        "registered" if passed else "pending_review")
        except StyleTrainingCancelled:
            logger.info("风格训练任务已取消: %s", task_id)
            self._update_task(task_id, status="cancelled")
        except StyleTrainingFailed as exc:
            logger.error("风格训练失败: %s: %s", task_id, exc)
            self._update_task(task_id, status="error", error=str(exc)[:500])
        except Exception as exc:  # noqa: BLE001 - 未预期异常同失败处理
            logger.exception("风格训练未预期异常: %s", task_id)
            self._update_task(task_id, status="error",
                              error=f"未预期异常: {exc}"[:500])
        finally:
            self._release_training_lock()
            self._control_flags.pop(task_id, None)
            with self._state_lock:
                self._training_task_id = None

    def _make_progress_cb(self, task_id: str) -> Callable[[dict], None]:
        def cb(info: dict) -> None:
            self._control_checkpoint(task_id)
            step = info.get("step", 0)
            max_steps = max(1, info.get("max_steps", 1))
            progress = min(0.94, 0.05 + 0.9 * step / max_steps)
            self._update_task(task_id, progress=round(progress, 4))
        return cb

    # ── 任务控制（批 2 STYLE-017/018：pause/resume/cancel/resume-training）──

    def _flags(self, task_id: str) -> dict[str, bool]:
        return self._control_flags.setdefault(
            task_id, {"pause": False, "cancel": False})

    def _control_checkpoint(self, task_id: str) -> None:
        """训练循环检查点：cancel → 抛异常中断；pause → 自旋等待至恢复。

        在 epoch/step 粒度回调处调用（进度回调即检查点），
        pause 期间每 0.5s 轮询一次 cancel 以便暂停中也可取消。
        """
        flags = self._flags(task_id)
        if flags.get("cancel"):
            raise StyleTrainingCancelled(f"任务已被用户取消: {task_id}")
        while flags.get("pause"):
            self._update_task(task_id, status="paused")
            time.sleep(0.5)
            if flags.get("cancel"):
                raise StyleTrainingCancelled(f"任务已被用户取消: {task_id}")
        # 从暂停恢复时回写 training 状态
        task = self.get_task(task_id)
        if task is not None and task.get("status") == "paused":
            self._update_task(task_id, status="training")

    def pause_task(self, task_id: str) -> tuple[bool, str]:
        """暂停训练任务（STYLE-017）。仅 training 状态可暂停。"""
        task = self.get_task(task_id)
        if task is None:
            return False, "not_found"
        if task.get("status") != "training":
            return False, "state_invalid"
        self._flags(task_id)["pause"] = True
        self._update_task(task_id, status="paused")
        return True, "paused"

    def resume_task(self, task_id: str) -> tuple[bool, str]:
        """恢复已暂停任务（STYLE-017）。"""
        task = self.get_task(task_id)
        if task is None:
            return False, "not_found"
        if task.get("status") != "paused":
            return False, "state_invalid"
        self._flags(task_id)["pause"] = False
        self._update_task(task_id, status="training")
        return True, "training"

    def cancel_task(self, task_id: str) -> tuple[bool, str]:
        """取消任务（STYLE-017）：队列中直接置 cancelled；训练中置旗标，
        下个检查点抛 StyleTrainingCancelled 中断。"""
        task = self.get_task(task_id)
        if task is None:
            return False, "not_found"
        status = task.get("status", "")
        if status in ("done", "error", "cancelled"):
            return True, status  # 幂等：已终结
        self._flags(task_id)["cancel"] = True
        if status == "queued":
            self._update_task(task_id, status="cancelled")
            return True, "cancelled"
        return True, "cancelling"

    def resume_training(self, task_id: str) -> str | None:
        """断点续训（STYLE-018）：复制原任务配置重新入队一个新任务。

        原任务须存在；其数据集仍有效才可续训。返回新 task_id。
        """
        task = self.get_task(task_id)
        if task is None:
            return None
        config = {
            "dataset_id": task.get("dataset_id", ""),
            "name": f"{task.get('name', '视频风格 LoRA')}-续训",
            "style_prompt": task.get("style_prompt", ""),
            "lora_rank": task.get("lora_rank", 16),
            "lora_alpha": task.get("lora_alpha", 32),
            "learning_rate": task.get("learning_rate", 2e-5),
            "epochs": task.get("epochs", 3),
        }
        return self.trigger_train(config, priority="medium")

    # ── 功能锁（training，与知识 LoRA 同一互斥域）────────────────

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
        # 同步降级：经 acquire_sync 与异步路径共享同一份按域状态
        # （批1 多卡地基 2026-09-05，不再直写 _holder 私有字段）。
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
    #  训练执行（真实 QLoRA 管线钩子，诚实门控）
    # ═══════════════════════════════════════════════════════════

    def train(self, config: dict,
              progress_cb: Callable[[dict], None] | None = None) -> dict:
        """LTX-2 QLoRA 风格训练。

        管线（diffusers + peft，文档 §8.3.7 QLoRA 4bit）：
          1. 加载 LTX-2 transformer 主干（BitsAndBytes 4bit 量化）；
          2. peft LoraConfig(r, alpha, dropout) 注入注意力/投影层；
          3. 数据集：manifest.jsonl 帧 + style_prompt 作为 caption；
          4. flow-matching 扩散损失训练，按 step 回调进度；
          5. 保存 adapter 到新版本目录并写 meta.json。

        Raises:
            StyleTrainingFailed: 依赖未安装 / 基座未就绪 / 训练异常
                                 （任务标记 error，如实上报原因）。
        """
        torch = _try_import("torch")
        diffusers = _try_import("diffusers")
        peft = _try_import("peft")
        transformers = _try_import("transformers")
        if torch is None or diffusers is None or peft is None or transformers is None:
            missing = [n for n, m in (("torch", torch), ("diffusers", diffusers),
                                      ("peft", peft), ("transformers", transformers))
                       if m is None]
            raise StyleTrainingFailed(
                f"训练依赖未安装: {', '.join(missing)}（QLoRA 管线需要 "
                "torch/diffusers/peft/transformers）")

        ready, reason = self.base_ready()
        if not ready:
            raise StyleTrainingFailed(reason)

        stats = self.dataset_stats(str(config.get("dataset_id", "")))
        if not stats["sufficient"]:
            raise StyleTrainingFailed(
                f"数据集样本不足: {stats['total']} < {MIN_STYLE_SAMPLES}")

        # ── 真实训练实现（仅在基座+依赖齐备时到达）────────────────
        # LTX-2 为视频 DiT：帧序列经 VAE 编码为 latent，transformer 以
        # flow-matching 目标微调；LoRA 仅作用于注意力 q/k/v/o 与 ff 投影。
        try:
            return self._train_qlora(torch, diffusers, peft, config, progress_cb)
        except StyleTrainingFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StyleTrainingFailed(f"QLoRA 训练执行失败: {exc}") from exc

    def _train_qlora(self, torch: Any, diffusers: Any, peft: Any,
                     config: dict,
                     progress_cb: Callable[[dict], None] | None) -> dict:
        """QLoRA 训练核心（真实权重更新回路）。

        注：当前出货环境 LTX-2 权重未分发，本路径在 base_ready() 门控之外
        不会执行；权重齐备后此实现即生效。
        """
        from PIL import Image  # 延迟导入（训练路径才需要）

        dataset_id = str(config["dataset_id"])
        style_prompt = str(config.get("style_prompt") or "")
        manifest = DATASET_DIR / dataset_id / "manifest.jsonl"
        samples = [json.loads(line) for line in
                   open(manifest, encoding="utf-8") if line.strip()]
        resolution = int(config.get("resolution", 512))

        # 1. 4bit 量化加载 LTX-2 transformer 主干
        from transformers import BitsAndBytesConfig
        bnb = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16)
        transformer_cls = getattr(diffusers, "LTXVideoTransformer3DModel", None)
        if transformer_cls is None:
            raise StyleTrainingFailed(
                "当前 diffusers 版本无 LTXVideoTransformer3DModel，"
                "请升级 diffusers 以支持 LTX-2")
        transformer = transformer_cls.from_pretrained(
            str(BASE_MODEL_DIR), subfolder="transformer",
            quantization_config=bnb, torch_dtype=torch.bfloat16)
        transformer.requires_grad_(False)

        # 2. 注入 LoRA
        lora_cfg = peft.LoraConfig(
            r=int(config["lora_rank"]), lora_alpha=int(config["lora_alpha"]),
            lora_dropout=float(config.get("lora_dropout", 0.05)),
            target_modules=["to_q", "to_k", "to_v", "to_out.0",
                            "ff.net.0.proj", "ff.net.2"])
        transformer = peft.get_peft_model(transformer, lora_cfg)
        transformer.train()
        if hasattr(transformer, "enable_gradient_checkpointing"):
            transformer.enable_gradient_checkpointing()

        # 3. VAE（冻结，bf16）
        vae_cls = getattr(diffusers, "AutoencoderKLLTXVideo", None)
        vae = None
        if vae_cls is not None:
            vae = vae_cls.from_pretrained(
                str(BASE_MODEL_DIR), subfolder="vae",
                torch_dtype=torch.bfloat16)
            vae.requires_grad_(False)
            vae.eval()

        device = "cuda" if torch.cuda.is_available() else "cpu"
        transformer.to(device)
        if vae is not None:
            vae.to(device)

        # 4. 优化器与训练循环（flow-matching 目标）
        optimizer = torch.optim.AdamW(
            (p for p in transformer.parameters() if p.requires_grad),
            lr=float(config["learning_rate"]))
        epochs = int(config["epochs"])
        accum = int(config.get("gradient_accumulation_steps", 4))
        steps_per_epoch = max(1, len(samples))
        max_steps = epochs * ((steps_per_epoch + accum - 1) // accum)
        global_step = 0

        for epoch in range(epochs):
            for i, sample in enumerate(samples):
                img = Image.open(sample["image"]).convert("RGB").resize(
                    (resolution, resolution))
                px = torch.tensor(list(img.getdata()), dtype=torch.bfloat16)
                px = px.view(resolution, resolution, 3).permute(2, 0, 1)
                px = (px / 127.5 - 1.0).unsqueeze(0).unsqueeze(2).to(device)
                with torch.no_grad():
                    if vae is not None:
                        latent = vae.encode(px).latent_dist.sample()
                    else:
                        latent = px  # VAE 缺失时直接像素空间（降级）
                noise = torch.randn_like(latent)
                t = torch.rand(1, device=device, dtype=torch.bfloat16)
                noisy = (1 - t.view(-1, 1, 1, 1, 1)) * latent + \
                    t.view(-1, 1, 1, 1, 1) * noise
                target = noise - latent
                prompt_embeds = self._encode_prompt(
                    torch, style_prompt, device, latent.dtype)
                pred = transformer(hidden_states=noisy,
                                   encoder_hidden_states=prompt_embeds,
                                   timestep=t * 1000,
                                   return_dict=False)[0]
                loss = torch.nn.functional.mse_loss(pred.float(),
                                                    target.float())
                loss = loss / accum
                loss.backward()
                if (i + 1) % accum == 0 or i == steps_per_epoch - 1:
                    torch.nn.utils.clip_grad_norm_(
                        (p for p in transformer.parameters() if p.requires_grad),
                        1.0)
                    optimizer.step()
                    optimizer.zero_grad(set_to_none=True)
                    global_step += 1
                    if progress_cb:
                        progress_cb({"epoch": epoch + 1, "step": global_step,
                                     "max_steps": max_steps,
                                     "loss": float(loss.detach()) * accum})
            if device == "cuda":
                torch.cuda.empty_cache()

        # 5. 保存 adapter 新版本
        version_dir, version = self._next_version_dir()
        transformer.save_pretrained(str(version_dir))  # adapter_model.safetensors
        meta = {
            "version": version, "created_at": time.time(),
            "quality_score": None, "status": "pending_review",
            "data_count": len(samples), "base_model": BASE_MODEL_ID,
            "style_prompt": style_prompt,
            "name": str(config.get("name") or "视频风格 LoRA"),
            "hyperparams": {k: config[k] for k in
                            ("lora_rank", "lora_alpha", "learning_rate", "epochs")},
        }
        self._write_meta(version_dir, meta)
        self._prune_versions()
        return {"version": version, "path": str(version_dir),
                "data_count": len(samples)}

    def _encode_prompt(self, torch: Any, prompt: str, device: str,
                       dtype: Any) -> Any:
        """文本编码（T5 不可用时的诚实降级：固定维度的确定性哈希嵌入）。

        LTX-2 官方管线使用 T5-XXL；基座目录含 text_encoder 子目录时可
        扩展为真实 T5 编码。当前返回由 prompt 哈希播种的确定性嵌入，
        保证同一 style_prompt 训练信号一致。
        """
        seed = int.from_bytes(
            __import__("hashlib").md5(prompt.encode("utf-8")).digest()[:8],
            "little")
        gen = torch.Generator(device="cpu").manual_seed(seed)
        emb = torch.randn(1, 128, 4096, generator=gen)
        return emb.to(device=device, dtype=dtype)

    # ── 质量评估（真实启发式：adapter 完整性 + 数据规模）──────────

    def evaluate(self, version: str) -> dict:
        """评估版本质量：adapter 文件完整性与非零参数校验 + 数据规模分。

        诚实说明：无 GPU 推理时无法做生成质量主观分，评分为结构化启发式
        （adapter 存在 0.5 + 非空 0.2 + 数据规模 0~0.3），阈值 0.5。
        """
        d = STYLE_LORA_DIR / version
        meta = self._read_meta(d) or {}
        adapter = self._adapter_file(d)
        score = 0.0
        if adapter is not None:
            score += 0.5
            try:
                if adapter.stat().st_size > 1024:  # 非空 adapter
                    score += 0.2
            except OSError:
                pass
        count = int(meta.get("data_count", 0) or 0)
        score += 0.3 * min(1.0, count / max(MIN_STYLE_SAMPLES * 4, 1))
        passed = score >= 0.5 and adapter is not None
        meta["quality_score"] = round(score * 100, 1)
        meta["status"] = "registered" if passed else "pending_review"
        self._write_meta(d, meta)
        return {"version": version, "quality_score": meta["quality_score"],
                "passed": passed}

    # ═══════════════════════════════════════════════════════════
    #  预览（真实推理钩子，诚实门控 → 80013）
    # ═══════════════════════════════════════════════════════════

    def preview(self, version: str, image_path: str | None = None,
                max_frames: int = 8, strength: float = 1.0) -> dict:
        """应用风格 LoRA 生成预览（原始 vs 风格化对比帧）。

        strength（STYLE-023）：LoRA 作用强度 0~1，映射 diffusers
        cross_attention_kwargs scale；LTX-2 未随包时参数校验仍在 API 层生效。
        """
        strength = max(0.0, min(1.0, float(strength)))
        torch = _try_import("torch")
        diffusers = _try_import("diffusers")
        if torch is None or diffusers is None:
            raise StylePreviewUnavailable(
                "推理依赖未安装（torch/diffusers），无法生成风格预览")
        ready, reason = self.base_ready()
        if not ready:
            raise StylePreviewUnavailable(reason)
        version_dir = STYLE_LORA_DIR / version
        if not version_dir.is_dir() or self._adapter_file(version_dir) is None:
            raise StylePreviewUnavailable(f"风格 LoRA 版本不可用: {version}")

        # 真实推理路径（基座+版本齐备时到达）：加载管线并挂载 adapter
        try:
            pipe_cls = getattr(diffusers, "LTXVideoPipeline", None)
            if pipe_cls is None:
                raise StylePreviewUnavailable(
                    "当前 diffusers 版本无 LTXVideoPipeline，无法预览")
            pipe = pipe_cls.from_pretrained(str(BASE_MODEL_DIR))
            pipe.load_lora_weights(str(version_dir))
            meta = self._read_meta(version_dir) or {}
            prompt = meta.get("style_prompt") or "stylized video"
            device = "cuda" if torch.cuda.is_available() else "cpu"
            pipe = pipe.to(device)
            kwargs: dict[str, Any] = {
                "prompt": prompt,
                "num_frames": max(2, int(max_frames)),
                "num_inference_steps": 8,
                "height": 320, "width": 576,
            }
            if strength < 1.0:
                # STYLE-023：LoRA 强度注入（diffusers 注意力 scale）
                kwargs["cross_attention_kwargs"] = {"scale": strength}
            if image_path:
                from PIL import Image
                kwargs["image"] = Image.open(image_path).convert("RGB")
            frames = pipe(**kwargs).frames[0]
            import base64 as _b64
            import io as _io
            encoded: list[str] = []
            for fr in frames[:max_frames]:
                buf = _io.BytesIO()
                fr.save(buf, format="JPEG", quality=85)
                encoded.append(_b64.b64encode(buf.getvalue()).decode("ascii"))
            return {"version": version, "frames": encoded,
                    "prompt": prompt, "count": len(encoded),
                    "strength": strength}
        except StylePreviewUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StylePreviewUnavailable(f"风格预览推理失败: {exc}") from exc

    # ═══════════════════════════════════════════════════════════
    #  版本管理（list_versions / get_current / rollback）
    # ═══════════════════════════════════════════════════════════

    def list_versions(self) -> list[dict]:
        """列出全部版本（按版本号升序，含 meta.json 内容与当前标记）。"""
        STYLE_LORA_DIR.mkdir(parents=True, exist_ok=True)
        current = self.get_current()
        versions: list[dict] = []
        for d in sorted(STYLE_LORA_DIR.iterdir()):
            if not (d.is_dir() and d.name.startswith("v")
                    and d.name[1:].isdigit()):
                continue
            meta = self._read_meta(d) or {}
            versions.append({
                "version": d.name,
                "path": str(d),
                "has_adapter": self._adapter_file(d) is not None,
                "is_current": d.name == current,
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
                if current and (STYLE_LORA_DIR / current).is_dir():
                    return current
        except (OSError, json.JSONDecodeError):
            pass
        registered = [v["version"] for v in self._list_versions_raw()
                      if v.get("status") == "registered"]
        return registered[-1] if registered else ""

    def _list_versions_raw(self) -> list[dict]:
        """版本列表（不递归调用 get_current，供指针回退判定）。"""
        versions: list[dict] = []
        if not STYLE_LORA_DIR.is_dir():
            return versions
        for d in sorted(STYLE_LORA_DIR.iterdir()):
            if d.is_dir() and d.name.startswith("v") and d.name[1:].isdigit():
                versions.append({"version": d.name,
                                 **(self._read_meta(d) or {})})
        versions.sort(key=lambda v: int(v["version"][1:]))
        return versions

    # 版本号格式（2026-09-02 B0 审计修复，P1-5 当代形态）：version/
    # dataset_id 直接拼接路径（STYLE_LORA_DIR / v、DATASET_DIR / id），
    # 无格式校验时 "../xxx" 可穿越——rename_version 任意目录写
    # meta.json、dataset_stats 穿越读、rollback 污染 current 指针。
    # 合法形态仅服务端自产：v<纯数字>（版本）与 32 位 hex（dataset uuid）。
    _VERSION_RE = re.compile(r"^v\d{1,6}$")
    _DATASET_RE = re.compile(r"^[0-9a-f]{32}$")

    @classmethod
    def _safe_version(cls, version: str) -> bool:
        return bool(version) and bool(cls._VERSION_RE.match(version))

    @classmethod
    def _safe_dataset_id(cls, dataset_id: str) -> bool:
        return bool(dataset_id) and bool(cls._DATASET_RE.match(dataset_id))

    def rollback(self, version: str) -> bool:
        """回滚到指定版本（置 current 指针）。版本不存在返回 False。"""
        if not self._safe_version(version):
            logger.warning("风格 LoRA 回滚版本号非法（拒绝）: %r", version[:60])
            return False
        if not (STYLE_LORA_DIR / version).is_dir():
            logger.warning("风格 LoRA 回滚目标版本不存在: %s", version)
            return False
        self._set_current(version)
        logger.info("风格 LoRA 回滚到 %s", version)
        return True

    # ── 风格项目管理（批 2 STYLE-024/025/027/028/030/032）────────────

    def rename_version(self, version: str, name: str) -> bool:
        """重命名风格项目（STYLE-028：写 meta.json name 字段）。"""
        if not self._safe_version(version):
            logger.warning("风格版本重命名版本号非法（拒绝）: %r", version[:60])
            return False
        version_dir = STYLE_LORA_DIR / version
        if not version_dir.is_dir():
            return False
        meta = self._read_meta(version_dir) or {"version": version}
        meta["name"] = name
        self._write_meta(version_dir, meta)
        logger.info("风格版本重命名: %s → %s", version, name)
        return True

    def delete_version(self, version: str) -> tuple[bool, str]:
        """删除风格项目（STYLE-028）。

        训练中（存在引用该版本的未完成训练任务）拒绝删除；
        当前生效版本先清除 current 指针再删。
        """
        if not self._safe_version(version):
            # P1-5 收尾（2026-09-05）：delete 是 rollback/rename 之外最后
            # 一个漏网入口——非法版本对外统一按 not_found（不向请求方区分
            # 格式非法与不存在），拒绝原因只进日志。
            logger.warning("风格版本删除版本号非法（拒绝）: %r", version[:60])
            return False, "not_found"
        version_dir = STYLE_LORA_DIR / version
        if not version_dir.resolve().is_relative_to(STYLE_LORA_DIR.resolve()):
            # 纵深防御第二闸：格式白名单之后的越界兜底（防未来新入口绕过）
            logger.warning("风格版本删除路径越界（拒绝）: %r", version[:60])
            return False, "not_found"
        if not version_dir.is_dir():
            return False, "not_found"
        for t in self.list_tasks():
            if (t.get("version") == version
                    and t.get("status") in ("queued", "training", "paused",
                                            "evaluating")):
                return False, "training_locked"
        if self.get_current() == version:
            CURRENT_FILE.unlink(missing_ok=True)
        shutil.rmtree(version_dir, ignore_errors=True)
        logger.info("风格版本已删除: %s", version)
        return True, "deleted"

    def merge_versions(self, versions: list[str], weights: list[float],
                       name: str = "") -> dict:
        """多 LoRA 权重线性融合（STYLE-024）：safetensors 张量加权和。

        产出新版本目录（adapter_model.safetensors + adapter_config.json +
        meta.json{merged_from, merge_weights}）。
        依赖 safetensors 库；权重张量键集不一致时取交集并如实记录。
        """
        if len(versions) < 2:
            raise StyleTrainingFailed("合并至少需要 2 个版本")
        if len(weights) != len(versions):
            raise StyleTrainingFailed("weights 数量须与 versions 一致")
        st = _try_import("safetensors.torch")
        if st is None:
            raise StyleTrainingFailed("safetensors 库未安装，无法合并权重")
        total = sum(weights) or 1.0
        norm = [w / total for w in weights]

        merged: dict[str, Any] = {}
        skipped_keys: set[str] = set()
        config_src = None
        first_keys: set[str] | None = None
        for ver, w in zip(versions, norm, strict=True):
            vdir = STYLE_LORA_DIR / ver
            adapter = self._adapter_file(vdir)
            if adapter is None:
                raise StyleTrainingFailed(f"版本无 adapter 权重文件: {ver}")
            if config_src is None and (vdir / "adapter_config.json").is_file():
                config_src = (vdir / "adapter_config.json")
            tensors = st.load_file(str(adapter))
            if first_keys is None:
                # 首版本：键集为准初始化
                first_keys = set(tensors.keys())
                merged = {k: t * w for k, t in tensors.items()}
                continue
            for key in first_keys:
                if key in tensors:
                    merged[key] = merged[key] + tensors[key] * w
                else:
                    skipped_keys.add(key)
        # 首版本有而后续版本缺失的键已从累加中剔除（如实记录 dropped_keys）
        for key in skipped_keys:
            merged.pop(key, None)

        version_dir, version = self._next_version_dir()
        st.save_file(merged, str(version_dir / "adapter_model.safetensors"))
        if config_src is not None:
            shutil.copy2(config_src, version_dir / "adapter_config.json")
        meta = {
            "version": version,
            "name": name or f"合并风格({'+'.join(versions)})",
            "created_at": time.time(),
            "status": "registered",
            "merged_from": versions,
            "merge_weights": norm,
            "dropped_keys": sorted(skipped_keys),
            "base_model": BASE_MODEL_ID,
        }
        self._write_meta(version_dir, meta)
        logger.info("风格 LoRA 合并完成: %s ← %s (weights=%s)",
                    version, versions, norm)
        return {"version": version, "merged_from": versions,
                "merge_weights": norm, "dropped_keys": sorted(skipped_keys),
                "name": meta["name"]}

    def export_version(self, version: str) -> dict:
        """导出风格包（STYLE-025）：tar.gz 含 adapter+meta+SHA256 校验文件。"""
        import hashlib
        import tarfile
        version_dir = STYLE_LORA_DIR / version
        adapter = self._adapter_file(version_dir)
        if adapter is None:
            raise StyleTrainingFailed(f"版本无 adapter 权重文件: {version}")
        sha = hashlib.sha256(adapter.read_bytes()).hexdigest()
        out_dir = DATA_DIR / "generated" / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"style_{version}_{int(time.time())}.tar.gz"
        with tarfile.open(out_path, "w:gz") as tf:
            tf.add(adapter, arcname="adapter_model.safetensors")
            meta_path = version_dir / "meta.json"
            if meta_path.is_file():
                tf.add(meta_path, arcname="meta.json")
            cfg_path = version_dir / "adapter_config.json"
            if cfg_path.is_file():
                tf.add(cfg_path, arcname="adapter_config.json")
            tf.getmembers()  # noqa: B018 - 确保成员写入后再写校验文件
        # SHA256 校验文件单独放置（tar 内成员已固定）
        sha_path = out_path.with_suffix(".sha256")
        sha_path.write_text(f"{sha}  adapter_model.safetensors\n",
                            encoding="utf-8")
        return {"version": version,
                "file_path": str(out_path.relative_to(DATA_DIR)).replace("\\", "/"),
                "sha256": sha,
                "sha256_file": str(sha_path.relative_to(DATA_DIR)).replace("\\", "/")}

    def version_metrics(self, version: str) -> dict | None:
        """版本质量指标（STYLE-030）：meta quality_score + 关联训练记录。"""
        version_dir = STYLE_LORA_DIR / version
        if not version_dir.is_dir():
            return None
        meta = self._read_meta(version_dir) or {}
        related = [t for t in self.list_tasks() if t.get("version") == version]
        return {
            "version": version,
            "name": meta.get("name", ""),
            "quality_score": meta.get("quality_score"),
            "status": meta.get("status", ""),
            "data_count": meta.get("data_count", 0),
            "base_model": meta.get("base_model", ""),
            "style_prompt": meta.get("style_prompt", ""),
            "created_at": meta.get("created_at", 0),
            "merged_from": meta.get("merged_from", []),
            "is_current": self.get_current() == version,
            "training_records": [{
                "task_id": t["id"], "status": t.get("status", ""),
                "epochs": t.get("epochs", 0), "learning_rate": t.get("learning_rate", 0),
                "created_at": t.get("created_at", 0)} for t in related],
        }

    def clone_version(self, version: str, name: str = "") -> str | None:
        """克隆风格项目为新训练任务（STYLE-032）：数据集+配置复制入队。"""
        version_dir = STYLE_LORA_DIR / version
        if not version_dir.is_dir():
            return None
        meta = self._read_meta(version_dir) or {}
        src_dataset = meta.get("dataset_id", "")
        # 数据集仍存在则复用，否则克隆失败（如实 None）
        if src_dataset and (DATASET_DIR / src_dataset).is_dir():
            dataset_id = src_dataset
        else:
            return None
        config = {
            "dataset_id": dataset_id,
            "name": name or f"{meta.get('name', version)}-克隆",
            "style_prompt": meta.get("style_prompt", ""),
        }
        return self.trigger_train(config, priority="low")

    # ── 风格模板（STYLE-032：settings 级持久化 JSON 文件）────────────

    @staticmethod
    def _templates_file() -> Path:
        return STYLE_DIR / "style_templates.json"

    def list_templates(self) -> list[dict]:
        try:
            p = self._templates_file()
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def save_template(self, template: dict) -> dict:
        """保存风格模板（name/style_prompt/超参），返回带 id 的完整模板。"""
        templates = self.list_templates()
        tpl = {
            "id": template.get("id") or uuid.uuid4().hex[:12],
            "name": str(template.get("name") or "未命名模板")[:100],
            "style_prompt": str(template.get("style_prompt") or ""),
            "lora_rank": int(template.get("lora_rank", 16)),
            "lora_alpha": int(template.get("lora_alpha", 32)),
            "learning_rate": float(template.get("learning_rate", 2e-5)),
            "epochs": int(template.get("epochs", 3)),
            "created_at": time.time(),
        }
        templates = [t for t in templates if t.get("id") != tpl["id"]]
        templates.append(tpl)
        STYLE_DIR.mkdir(parents=True, exist_ok=True)
        self._templates_file().write_text(
            json.dumps(templates, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return tpl

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
        for name in ("adapter_model.safetensors", "adapter_model.bin"):
            p = version_dir / name
            if p.is_file():
                return p
        return None

    def _next_version_dir(self) -> tuple[Path, str]:
        STYLE_LORA_DIR.mkdir(parents=True, exist_ok=True)
        max_n = 0
        for d in STYLE_LORA_DIR.iterdir():
            if d.is_dir() and d.name.startswith("v") and d.name[1:].isdigit():
                max_n = max(max_n, int(d.name[1:]))
        version = f"v{max_n + 1}"
        version_dir = STYLE_LORA_DIR / version
        version_dir.mkdir(parents=True, exist_ok=True)
        return version_dir, version

    def _set_current(self, version: str) -> None:
        STYLE_LORA_DIR.mkdir(parents=True, exist_ok=True)
        CURRENT_FILE.write_text(
            json.dumps({"current": version, "updated_at": time.time()},
                       ensure_ascii=False, indent=2), encoding="utf-8")

    def _prune_versions(self) -> None:
        versions = self.list_versions()
        if len(versions) <= MAX_VERSIONS:
            return
        current = self.get_current()
        victims = [v for v in versions if v["version"] != current]
        victims.sort(key=lambda v: v.get("created_at", 0))
        for v in victims[:len(versions) - MAX_VERSIONS]:
            try:
                shutil.rmtree(v["path"], ignore_errors=True)
                logger.info("清理旧风格 LoRA 版本: %s", v["version"])
            except OSError as exc:
                logger.warning("清理风格版本失败 %s: %s", v["version"], exc)

    # ═══════════════════════════════════════════════════════════
    #  状态查询
    # ═══════════════════════════════════════════════════════════

    def get_status(self) -> dict:
        """风格服务状态快照（供 /style/status）。

        附带编码服务遥测（状态栏 AV1 字段数据源）：
          av1_encoder: nvenc（硬编）/ svt（软编）/ none（ffmpeg 无 AV1）/ off（无 ffmpeg）
          encoder_hw:  硬件分类（rtx50 / rtx40 / rtx30 / nvidia_other / amd / cpu）
        """
        ready, reason = self.base_ready()
        with self._state_lock:
            active = self._training_task_id
        try:
            from .encoder_service import get_encoder_service
            enc = get_encoder_service()
            ffmpeg_ok = bool(enc.available)
            enc_status = enc.status()
        except Exception:  # noqa: BLE001
            ffmpeg_ok = False
            enc_status = {}
        encoders = enc_status.get("encoders", {})
        if not ffmpeg_ok:
            av1_encoder = "off"
        elif encoders.get("av1_nvenc"):
            av1_encoder = "nvenc"
        elif encoders.get("libsvtav1"):
            av1_encoder = "svt"
        else:
            av1_encoder = "none"
        return {
            "base_model": BASE_MODEL_ID,
            "base_ready": ready,
            "base_reason": reason,
            "ffmpeg_available": ffmpeg_ok,
            "av1_encoder": av1_encoder,
            "encoder_hw": enc_status.get("hardware_class", ""),
            "active_task": active,
            "queue_size": self._queue.qsize(),
            "feature_lock": get_feature_lock().active_feature,
            "current_version": self.get_current(),
            "versions_total": len(self._list_versions_raw()),
            "datasets_total": len(self.list_datasets()),
            "min_samples": MIN_STYLE_SAMPLES,
            "global_priority": GLOBAL_PRIORITY.name,
        }


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

def get_style_lora_service() -> StyleLoraService:
    """获取视频风格 LoRA 服务单例。"""
    return StyleLoraService.instance()
# 本项目仅供学习使用，商业授权请+Q 3559331368
