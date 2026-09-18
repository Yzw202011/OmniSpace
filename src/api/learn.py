"""知识学习 API 路由（规格 §4.4 知识学习 / LoRA 训练）。

端点清单：
- POST /learn/train              创建训练任务（真实 QLoRA 管线入队）
- GET  /learn/tasks              训练任务列表
- GET  /learn/tasks/{task_id}    训练任务详情（真实状态/进度）
- POST /learn/tasks/{task_id}/cancel  取消训练任务（审计 R3-BE1）
- POST /learn/tasks/reorder      调整训练任务优先级（审计 R3-BE1）
- POST /learn/dataset/upload     上传训练数据（落盘 data/training/）
- GET  /learn/models             可训练的基础模型列表
- GET  /learn/training/status    训练服务状态（数据集充分性/版本/队列）
- POST /learn/lora/rollback      LoRA 版本回滚

约定：router 不带 prefix；成功 ok(data)；错误抛 ApiError。

数据层：train_tasks 表由 LoRATrainingService 写入并实时更新状态与进度，
本模块只读查询；数据库不可用时降级到服务内存镜像。
训练互斥由服务内部持有 "training" 功能锁保证（规格 §6.1），API 层不持锁。
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Query, UploadFile

from ..data.database import get_db_safe
from ..data.models import TrainStatus, TrainTaskCreate
from ..middleware import upload_guard
from ..middleware.error_handler import ApiError, ok
from ..services.lora_training_service import (
    MIN_TRAINING_SAMPLES,
    TRAIN_DATA_DIR,
    get_lora_training_service,
)

router = APIRouter()
log = logging.getLogger("omnispace.api.learn")


def _now() -> float:
    return time.time()


def _row_to_train_task(r: dict) -> dict:
    """train_tasks 行 -> TrainTask dict。"""
    return {
        "id": r["id"],
        "base_model": r["base_model"],
        "lora_rank": r.get("lora_rank", 16),
        "lora_alpha": r.get("lora_alpha", 32),
        "learning_rate": r.get("learning_rate", 1e-4),
        "epochs": r.get("epochs", 3),
        "dataset_path": r.get("dataset_path", ""),
        "status": r.get("status", TrainStatus.QUEUED.value),
        "progress": float(r.get("progress", 0.0) or 0.0),
    }


# 可训练基础模型清单（对齐 models/ 实际基座 + 服务默认基座）
_TRAINABLE_BASE_MODELS = [
    {"id": "qwen3-vl-4b", "name": "Qwen3-VL-4B", "category": "dialog",
     "params": "4B", "size_gb": 8.1, "trainable": True},
    {"id": "qwen2-vl-2b", "name": "Qwen2-VL-2B", "category": "dialog",
     "params": "2B", "size_gb": 4.5, "trainable": True},
    {"id": "sdxl-base-1.0", "name": "SDXL Base 1.0", "category": "paint",
     "params": "6.6B", "size_gb": 6.9, "trainable": True},
]


@router.post("/learn/train")
def learn_train(req: TrainTaskCreate) -> dict[str, Any]:
    """创建训练任务（规格 §4.4）：真实 QLoRA 微调入队。

    dataset_path 留空时自动从知识库 + 行为偏好对构建训练集
    （prepare_training_data，≥100 条才允许触发，规格 §3.3）。
    """
    if not req.base_model:
        raise ApiError(40008, "base_model 不能为空")

    svc = get_lora_training_service()
    config = {
        "base_model": req.base_model,
        "lora_rank": req.lora_rank,
        "lora_alpha": req.lora_alpha,
        "learning_rate": req.learning_rate,
        "epochs": req.epochs,
    }
    # 显式指定外部数据集时覆盖自动构建路径
    if req.dataset_path and req.dataset_path.strip():
        # 审计 P1-5：dataset_path 白名单校验（仅训练数据目录内），
        # 拒绝任意文件路径穿越读取（服务层 _load_external_dataset 双保险）
        if not svc._is_allowed_dataset_path(Path(req.dataset_path.strip())):
            raise ApiError(
                40013, "数据集路径不在允许的训练数据目录内",
                detail={"dataset_path": req.dataset_path,
                        "allowed_root": str(TRAIN_DATA_DIR)})
        config["dataset_path"] = req.dataset_path.strip()

    task_id = svc.trigger_finetune(config=config, priority="low")
    if task_id is None:
        # 触发条件不满足：区分原因给出可操作的错误信息
        # 外部数据集场景按外部数据判定，避免误报"数据不足"
        data = None
        if config.get("dataset_path"):
            data = svc._load_external_dataset(config["dataset_path"])
        if data is None:
            data = svc.prepare_training_data()
        if data["total"] < MIN_TRAINING_SAMPLES:
            raise ApiError(
                40009,
                f"训练数据不足：当前 {data['total']} 条，"
                f"至少需要 {MIN_TRAINING_SAMPLES} 条",
                detail={"total": data["total"],
                        "required": MIN_TRAINING_SAMPLES},
                suggestion="先使用知识学习/行为学习积累数据，"
                           "或通过 /learn/dataset/upload 上传训练集")
        raise ApiError(
            40007, "训练条件不满足：GPU 正被其他功能占用或已有训练任务进行中",
            detail=svc.get_status())

    # 读取服务写入的真实任务记录返回
    db = get_db_safe()
    if db is not None:
        try:
            row = db.query_one(
                "SELECT id, base_model, lora_rank, lora_alpha, learning_rate,"
                " epochs, dataset_path, status, progress, created_at,"
                " updated_at FROM train_tasks WHERE id=?",
                (task_id,))
            if row is not None:
                return ok(_row_to_train_task(row), message="训练任务已创建")
        except Exception as exc:  # noqa: BLE001
            log.warning("训练任务查询失败: %s", exc, exc_info=True)
    return ok({"id": task_id, "base_model": req.base_model,
               "status": TrainStatus.QUEUED.value, "progress": 0.0},
              message="训练任务已创建")


@router.get("/learn/tasks")
def learn_tasks() -> dict[str, Any]:
    """训练任务列表（规格 §4.4）。按创建时间倒序。"""
    db = get_db_safe()
    if db is not None:
        try:
            rows = db.query(
                "SELECT id, base_model, lora_rank, lora_alpha, learning_rate,"
                " epochs, dataset_path, status, progress, created_at,"
                " updated_at FROM train_tasks ORDER BY created_at DESC")
            items = [_row_to_train_task(r) for r in rows]
            return ok({"items": items, "total": len(items)})
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库查询失败: %s", exc, exc_info=True)
            raise ApiError(40006, "训练任务列表查询失败",
                           detail={"error": str(exc)}) from exc
    raise ApiError(40006, "数据库不可用，无法查询训练任务")


@router.get("/learn/tasks/{task_id}")
def learn_task_detail(task_id: str) -> dict[str, Any]:
    """训练任务详情（规格 §4.4）：返回服务实时写入的真实状态与进度。"""
    db = get_db_safe()
    if db is not None:
        try:
            row = db.query_one(
                "SELECT id, base_model, lora_rank, lora_alpha, learning_rate,"
                " epochs, dataset_path, status, progress, created_at,"
                " updated_at FROM train_tasks WHERE id=?",
                (task_id,))
            if row is not None:
                return ok(_row_to_train_task(row))
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库查询失败: %s", exc, exc_info=True)
            raise ApiError(40006, "训练任务查询失败",
                           detail={"error": str(exc)}) from exc
    raise ApiError(40005, "训练任务不存在", detail={"task_id": task_id})


# ═══════════════════════════════════════════════════════════════════
#  审计 R3-BE1：训练任务取消 / 优先级调整（前端 LearnView 真实入口）
# ═══════════════════════════════════════════════════════════════════

# 可取消状态：排队/训练/评估中；done/error/cancelled 为终态不可取消
_CANCELLABLE_STATUSES = {
    TrainStatus.QUEUED.value,
    TrainStatus.TRAINING.value,
    TrainStatus.EVALUATING.value,
}


@router.post("/learn/tasks/{task_id}/cancel")
def learn_task_cancel(task_id: str) -> dict[str, Any]:
    """强制取消训练任务（审计 R3-BE1 + 用户最高权限铁律）。

    仅 queued/training/evaluating 可取消；终态任务返回
    TRAINING_TASK_STATE_INVALID。

    取消语义（强制真实中断）：
    - queued：落库 cancelled，工作线程出队时跳过；
    - training：置位运行时中断标志，训练循环在下一个 step 边界
      经 control.should_training_stop 干净退出，半成品 adapter 不落盘；
    - evaluating：评估完成后按取消收敛，已评估版本不注册。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_UNAVAILABLE", "数据库不可用，无法取消训练任务")
    try:
        row = db.query_one(
            "SELECT id, status FROM train_tasks WHERE id=?", (task_id,))
    except Exception as exc:  # noqa: BLE001
        log.warning("训练任务查询失败: %s", exc, exc_info=True)
        raise ApiError(40006, "训练任务查询失败", detail={"error": str(exc)}) from exc
    if row is None:
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "训练任务不存在",
                       detail={"task_id": task_id})
    status = row.get("status", TrainStatus.QUEUED.value)
    if status not in _CANCELLABLE_STATUSES:
        raise ApiError(
            "TRAINING_TASK_STATE_INVALID",
            f"当前任务状态不允许取消: {status}",
            detail={"task_id": task_id, "status": status})
    try:
        db.update("train_tasks",
                  {"status": TrainStatus.CANCELLED.value,
                   "updated_at": _now()},
                  "id=?", (task_id,))
    except Exception as exc:  # noqa: BLE001
        log.warning("训练任务取消落库失败: %s", exc, exc_info=True)
        raise ApiError(40006, "训练任务取消失败", detail={"error": str(exc)}) from exc
    # 强制取消：置位运行时中断标志（训练/评估中的任务于下一检查点中断）
    try:
        from ..services.lora_training_service import get_lora_training_service
        get_lora_training_service().request_cancel(task_id)
    except Exception as exc:  # noqa: BLE001 - 中断置位失败不影响落库语义
        log.warning("训练中断标志置位失败（任务仍标记 cancelled）: %s", exc, exc_info=True)
    return ok({"id": task_id, "status": TrainStatus.CANCELLED.value},
              message="训练任务已取消")


# 终态任务记录可删（进行中须先取消：避免删除后服务内存态与库不一致）
_TERMINAL_STATUSES = {
    TrainStatus.DONE.value,
    TrainStatus.ERROR.value,
    TrainStatus.CANCELLED.value,
}


@router.delete("/learn/tasks/{task_id}")
def learn_task_delete(task_id: str) -> dict[str, Any]:
    """删除训练任务记录（2026-09-05 用户需求：训练任务记录要能删除）。

    仅终态（done/error/cancelled）可删；进行中（queued/training/
    evaluating）须先取消。只删任务记录，不影响已产出的 LoRA 版本
    文件（版本管理见 /learn/lora/versions）。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_UNAVAILABLE", "数据库不可用，无法删除训练任务")
    try:
        row = db.query_one(
            "SELECT id, status FROM train_tasks WHERE id=?", (task_id,))
    except Exception as exc:  # noqa: BLE001
        log.warning("训练任务查询失败: %s", exc, exc_info=True)
        raise ApiError(40006, "训练任务查询失败", detail={"error": str(exc)}) from exc
    if row is None:
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "训练任务不存在",
                       detail={"task_id": task_id})
    status = row.get("status", TrainStatus.QUEUED.value)
    if status not in _TERMINAL_STATUSES:
        raise ApiError(
            "TRAINING_TASK_STATE_INVALID",
            f"任务仍在进行中（{status}），请先取消再删除记录",
            detail={"task_id": task_id, "status": status})
    try:
        db.delete("train_tasks", "id=?", (task_id,))
    except Exception as exc:  # noqa: BLE001
        log.warning("训练任务删除失败: %s", exc, exc_info=True)
        raise ApiError(40006, "训练任务删除失败", detail={"error": str(exc)}) from exc
    return ok({"deleted": task_id}, message="训练任务记录已删除")


@router.post("/learn/tasks/reorder")
def learn_tasks_reorder(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """调整训练任务优先级（审计 R3-BE1）。

    body: {"task_ids": ["id1", "id2", ...]}，按数组顺序将
    train_tasks.priority 更新为递增序号（0,1,2,...）。
    """
    task_ids = (body or {}).get("task_ids")
    if (not isinstance(task_ids, list) or not task_ids
            or not all(isinstance(t, str) and t for t in task_ids)):
        raise ApiError(40008, "task_ids 必须是非空字符串数组")
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_UNAVAILABLE", "数据库不可用，无法调整任务顺序")
    updated, missing = 0, []
    try:
        for idx, tid in enumerate(task_ids):
            n = db.update("train_tasks",
                          {"priority": idx, "updated_at": _now()},
                          "id=?", (tid,))
            if n:
                updated += 1
            else:
                missing.append(tid)
    except Exception as exc:  # noqa: BLE001
        log.warning("训练任务排序失败: %s", exc, exc_info=True)
        raise ApiError(40006, "训练任务排序失败", detail={"error": str(exc)}) from exc
    return ok({"updated": updated, "missing": missing},
              message=f"已更新 {updated} 个任务的优先级")


# 审计 P1-4：上传大小上限 50MB（规格 §6.1 安全架构：本地文件上传限制）
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@router.post("/learn/dataset/upload")
async def learn_dataset_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """上传训练数据（JSONL：{"instruction","input","output"} 每行一条）。

    文件落盘 data/training/uploads/，作为 /learn/train 的 dataset_path 使用。
    审计 P1-4：单文件上限 50MB，超限拒绝（40009 操作超出上限）。
    """
    if not file or not file.filename:
        raise ApiError(40008, "未提供上传文件")
    # 审计 R3-BE2：限量读取（上限+1 字节），先验大小再判空，
    # 避免超限文件被整体读入内存
    content = await file.read(_MAX_UPLOAD_BYTES + 1)
    if not content:
        raise ApiError(40008, "上传文件内容为空")
    if len(content) > _MAX_UPLOAD_BYTES:
        raise ApiError(
            40009, "上传文件超过 50MB 上限",
            detail={"size_bytes": len(content), "limit_bytes": _MAX_UPLOAD_BYTES},
            suggestion="请拆分数据集或压缩后重新上传")

    # TASK-P0-03：后缀白名单 + 魔数嗅验（.exe 改名 .jsonl 在此拦截）
    try:
        upload_guard.validate(file.filename, content, upload_guard.DATASET_TABLE)
    except upload_guard.UploadRejected as exc:
        raise ApiError(40004, str(exc),
                       detail={"filename": file.filename},
                       suggestion="请上传 UTF-8 编码的 JSONL/JSON/TXT 文件") from exc
    suffix = Path(file.filename).suffix.lower()

    dataset_id = uuid.uuid4().hex
    dest_dir = TRAIN_DATA_DIR / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"dataset_{dataset_id}{suffix}"
    try:
        dest.write_bytes(content)
    except OSError as exc:
        raise ApiError(40006, "数据集落盘失败", detail={"error": str(exc)}) from exc

    return ok({"id": dataset_id, "filename": file.filename,
               "dataset_path": str(dest),
               "size_bytes": len(content),
               "uploaded_at": _now()},
              message="数据集已上传，可在 /learn/train 的 dataset_path 中引用")


@router.get("/learn/models")
def learn_models() -> dict[str, Any]:
    """可训练的基础模型列表（规格 §4.4）。"""
    return ok({"items": _TRAINABLE_BASE_MODELS,
               "total": len(_TRAINABLE_BASE_MODELS)})


@router.get("/learn/training/status")
def learn_training_status() -> dict[str, Any]:
    """训练服务状态：数据集充分性 / 当前版本 / 版本列表 / 队列 / 活跃任务。"""
    svc = get_lora_training_service()
    status = svc.get_status()
    status["min_training_samples"] = MIN_TRAINING_SAMPLES
    status["should_trigger_finetune"] = svc.should_trigger_finetune()
    return ok(status)


@router.post("/learn/lora/rollback")
def learn_lora_rollback(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """LoRA 版本回滚：{"version": "v3"} → 置为当前生效版本。"""
    version = str((body or {}).get("version", "")).strip()
    if not version:
        raise ApiError(40008, "缺少必填参数: version")
    svc = get_lora_training_service()
    if not svc.rollback(version):
        raise ApiError(40005, f"LoRA 版本不存在: {version}",
                       detail={"version": version,
                               "versions": [v.get("version")
                                            for v in svc.list_versions()]})
    return ok({"version": version, "current": svc.get_current()},
              message=f"已回滚到 {version}")


# ═══════════════════════════════════════════════════════════════════
#  契约别名（规格 §7.1.2 /v1/learn/lora/*）
# ═══════════════════════════════════════════════════════════════════

@router.get("/learn/lora/versions")
def learn_lora_versions() -> dict[str, Any]:
    """LoRA 版本列表（规格 §7.1.2）：含质量评分与当前生效版本。"""
    svc = get_lora_training_service()
    versions = svc.list_versions()
    return ok({"items": versions, "total": len(versions),
               "current": svc.get_current()})


@router.get("/learn/lora/versions/compare")
def learn_lora_versions_compare(a: str = Query(..., min_length=1),
                                b: str = Query(..., min_length=1)) -> dict[str, Any]:
    """LoRA 版本对比（LEARN-035）：?a=v1&b=v2 → 质量分/样本数/超参/
    训练耗时逐项对比，并给出字段级差值。"""
    svc = get_lora_training_service()
    versions = {v["version"]: v for v in svc.list_versions()}
    missing = [v for v in (a, b) if v not in versions]
    if missing:
        raise ApiError(40005, f"LoRA 版本不存在: {', '.join(missing)}",
                       detail={"missing": missing,
                               "versions": sorted(versions)})
    va, vb = versions[a], versions[b]
    # 对比维度：质量/数据/超参/时间（meta.json 字段全集的公共子集）
    keys = ("quality_score", "data_count", "base_model", "lora_rank",
            "lora_alpha", "learning_rate", "epochs", "train_seconds",
            "created_at", "status", "name")
    fields: dict[str, dict] = {}
    for k in keys:
        x, y = va.get(k), vb.get(k)
        if x is None and y is None:
            continue
        entry: dict = {"a": x, "b": y}
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            entry["diff"] = round(y - x, 6)
        fields[k] = entry
    qa = va.get("quality_score")
    qb = vb.get("quality_score")
    better = ""
    if isinstance(qa, (int, float)) and isinstance(qb, (int, float)):
        better = b if qb > qa else (a if qa > qb else "tie")
    return ok({"a": a, "b": b, "fields": fields,
               "quality_better": better,
               "current": svc.get_current()})
# 本项目仅供学习使用，商业授权请+Q 3559331368
