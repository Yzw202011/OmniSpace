"""视频风格 API 路由（文档 §7.1.4 /v1/style：/train, /preview, /versions）。

端点清单：
- POST /style/upload              上传风格素材（视频抽帧/图片收录 → 数据集）
- GET  /style/datasets            数据集列表
- GET  /style/datasets/{id}       数据集样本统计
- POST /style/train               触发风格 LoRA 训练（QLoRA 入队）
- GET  /style/tasks               训练任务列表
- GET  /style/tasks/{task_id}     训练任务详情（真实状态/进度）
- GET  /style/versions            风格 LoRA 版本列表
- POST /style/rollback            版本回滚（置 current 指针）
- POST /style/preview             风格预览（原始 vs 风格化对比帧）
- GET  /style/status              风格服务状态（基座/FFmpeg/队列/当前版本）

约定：router 不带 prefix；成功 ok(data)；错误抛 ApiError。
错误码段 8001x（文档 §9.1.2：80000~89999 风格/LoRA 段）：
  80010 基座未就绪 / 80011 数据集无效或样本不足 / 80012 版本不存在 /
  80013 预览不可用 / 80014 任务不存在 / 80015 素材格式不支持 / 80016 抽帧失败

诚实标注：当前出货环境 LTX-2 基座权重未分发（models/ltx-2 不存在），
/style/train 如实返回 80010、/style/preview 如实返回 80013，不伪造进度；
用户下载权重放入 models/ltx-2 后管线自动生效（见 style_lora_service 头注）。

训练互斥由服务内部持有 "training" 功能锁保证（规格 §6.1），API 层不持锁。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, UploadFile

from ..config import DATA_DIR
from ..middleware import upload_guard
from ..middleware.error_handler import ApiError, ok
from ..services.style_lora_service import (
    MAX_UPLOAD_BYTES,
    MIN_STYLE_SAMPLES,
    StylePreviewUnavailable,
    StyleTrainingFailed,
    get_style_lora_service,
)

router = APIRouter()
log = logging.getLogger("omnispace.api.style")


# ═══════════════════════════════════════════════════════════════════
#  素材上传与数据集
# ═══════════════════════════════════════════════════════════════════

@router.post("/style/upload")
async def style_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """上传风格素材并构建训练数据集。

    视频（mp4/webm/mov/mkv/avi）经 FFmpeg 以 fps=1 抽帧（≤32 帧）；
    图片（png/jpg/jpeg/webp/bmp）直接收录。单文件上限 200MB。
    返回 dataset_id，供 /style/train 引用。
    """
    if not file or not file.filename:
        raise ApiError(40008, "未提供上传文件")
    # 审计 R3-BE2：限量读取（上限+1 字节），先验大小再判空，
    # 避免超限文件被整体读入内存
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise ApiError(40008, "上传文件内容为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ApiError(
            40009, "上传文件超过 200MB 上限",
            detail={"size_bytes": len(content), "limit_bytes": MAX_UPLOAD_BYTES},
            suggestion="请压缩视频或截取片段后重新上传")

    # TASK-P0-03：后缀白名单 + 魔数嗅验（伪装扩展名的可执行体在此拦截）
    try:
        upload_guard.validate(file.filename, content, upload_guard.MEDIA_TABLE)
    except upload_guard.UploadRejected as exc:
        raise ApiError(80015, str(exc),
                       detail={"filename": file.filename}) from exc

    svc = get_style_lora_service()
    try:
        result = svc.save_upload(file.filename, content)
    except ValueError as exc:
        # 格式不支持 / 内容为空（服务层已带可操作建议）
        raise ApiError(80015, str(exc)) from exc
    except RuntimeError as exc:
        # FFmpeg 不可用 / 抽帧失败 / 素材无法解析
        raise ApiError(80016, str(exc)) from exc
    except OSError as exc:
        raise ApiError(40006, "素材落盘失败",
                       detail={"error": str(exc)}) from exc

    return ok(result, message="素材已上传并构建数据集")


@router.get("/style/datasets")
def style_datasets() -> dict[str, Any]:
    """已构建的风格数据集列表（按更新时间倒序）。"""
    svc = get_style_lora_service()
    items = svc.list_datasets()
    return ok({"items": items, "total": len(items)})


@router.get("/style/datasets/{dataset_id}")
def style_dataset_detail(dataset_id: str) -> dict[str, Any]:
    """数据集样本统计与训练充分性判定。"""
    svc = get_style_lora_service()
    stats = svc.dataset_stats(dataset_id)
    if stats["total"] <= 0:
        raise ApiError(80011, f"风格数据集不存在或无有效样本: {dataset_id}",
                       detail={"dataset_id": dataset_id,
                               "min_samples": MIN_STYLE_SAMPLES})
    return ok(stats)


# ═══════════════════════════════════════════════════════════════════
#  训练
# ═══════════════════════════════════════════════════════════════════

@router.post("/style/train")
def style_train(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """触发视频风格 LoRA 训练（文档 §8.3.7 QLoRA 4bit，P2 优先级入队）。

    请求体:
        dataset_id (str, 必填): /style/upload 返回的数据集 ID
        name (str): 版本名称；style_prompt (str): 风格提示词
        lora_rank/lora_alpha (int)、learning_rate (float)、epochs (int)：
            可选超参，越界由服务层钳制到硬件安全边界（审计 P0-6）

    前置条件不满足时按原因映射：80010 基座未就绪 / 80011 数据集无效 /
    40007 GPU 被占用或已有训练进行中。
    """
    body = body or {}
    dataset_id = str(body.get("dataset_id") or "").strip()
    if not dataset_id:
        raise ApiError(40008, "缺少必填参数: dataset_id",
                       suggestion="先通过 POST /style/upload 上传素材构建数据集")

    svc = get_style_lora_service()
    ok_flag, reason = svc.can_train(dataset_id)
    if not ok_flag:
        if reason == "base_not_ready":
            _ready, base_reason = svc.base_ready()
            raise ApiError(80010, "风格训练基座未就绪（LTX-2 权重未下载）",
                           detail={"reason": base_reason,
                                   "base_model_dir": "models/ltx-2"},
                           suggestion="下载 LTX-2 权重放入 models/ltx-2 后重试")
        if reason == "dataset_insufficient":
            stats = svc.dataset_stats(dataset_id)
            raise ApiError(
                80011,
                f"风格训练数据集无效或样本不足：当前 {stats['total']} 帧，"
                f"至少需要 {MIN_STYLE_SAMPLES} 帧",
                detail={"dataset_id": dataset_id, "total": stats["total"],
                        "required": MIN_STYLE_SAMPLES},
                suggestion="重新上传时长更久的视频或更多风格图片")
        # training_active / feature_busy
        raise ApiError(
            40007, "训练条件不满足：GPU 正被其他功能占用或已有训练任务进行中",
            detail={"reason": reason, "status": svc.get_status()})

    config = {
        "dataset_id": dataset_id,
        "name": body.get("name"),
        "style_prompt": body.get("style_prompt"),
    }
    for key in ("lora_rank", "lora_alpha", "learning_rate", "epochs"):
        if body.get(key) is not None:
            config[key] = body[key]

    priority = str(body.get("priority") or "low")
    task_id = svc.trigger_train(config, priority=priority)
    if task_id is None:
        # can_train 与 trigger 之间存在竞态（极端情况），如实上报
        raise ApiError(40007, "训练任务入队失败：并发状态已变化",
                       detail=svc.get_status())

    task = svc.get_task(task_id) or {"id": task_id, "status": "queued",
                                     "progress": 0.0}
    return ok(task, message="风格训练任务已创建")


@router.get("/style/tasks")
def style_tasks() -> dict[str, Any]:
    """风格训练任务列表（按创建时间倒序）。"""
    svc = get_style_lora_service()
    items = svc.list_tasks()
    return ok({"items": items, "total": len(items)})


@router.get("/style/tasks/{task_id}")
def style_task_detail(task_id: str) -> dict[str, Any]:
    """风格训练任务详情：服务实时写入的真实状态与进度。"""
    svc = get_style_lora_service()
    task = svc.get_task(task_id)
    if task is None:
        raise ApiError(80014, "风格训练任务不存在",
                       detail={"task_id": task_id})
    return ok(task)


# ═══════════════════════════════════════════════════════════════════
#  任务控制（批 2 STYLE-017/018：pause/resume/cancel/resume-training）
# ═══════════════════════════════════════════════════════════════════

def _task_control(task_id: str, action: str) -> dict[str, Any]:
    """pause/resume/cancel 公共分发。"""
    svc = get_style_lora_service()
    handler = {"pause": svc.pause_task, "resume": svc.resume_task,
               "cancel": svc.cancel_task}[action]
    ok_flag, state = handler(task_id)
    if not ok_flag and state == "not_found":
        raise ApiError(80014, "风格训练任务不存在",
                       detail={"task_id": task_id})
    if not ok_flag:  # state_invalid
        task = svc.get_task(task_id) or {}
        raise ApiError(
            "TRAINING_TASK_STATE_INVALID",
            f"当前任务状态不允许 {action} 操作",
            detail={"task_id": task_id, "status": task.get("status", "")})
    return ok({"task_id": task_id, "action": action, "status": state})


@router.post("/style/tasks/{task_id}/pause")
def style_task_pause(task_id: str) -> dict[str, Any]:
    """暂停训练任务（STYLE-017）：epoch 检查点挂起，状态 → paused。"""
    return _task_control(task_id, "pause")


@router.post("/style/tasks/{task_id}/resume")
def style_task_resume(task_id: str) -> dict[str, Any]:
    """恢复已暂停任务（STYLE-017）：清除挂起旗标，状态 → training。"""
    return _task_control(task_id, "resume")


@router.post("/style/tasks/{task_id}/cancel")
def style_task_cancel(task_id: str) -> dict[str, Any]:
    """取消训练任务（STYLE-017）：队列中直接置 cancelled；
    训练中置取消旗标，下个 epoch 检查点中断；已终结任务幂等返回。"""
    return _task_control(task_id, "cancel")


@router.post("/style/tasks/{task_id}/resume-training")
def style_task_resume_training(task_id: str) -> dict[str, Any]:
    """断点续训（STYLE-018）：复制原任务配置（数据集+超参）重新入队新任务。"""
    svc = get_style_lora_service()
    new_task_id = svc.resume_training(task_id)
    if new_task_id is None:
        raise ApiError(80014, "风格训练任务不存在或数据集已失效，无法续训",
                       detail={"task_id": task_id})
    return ok({"task_id": task_id, "new_task_id": new_task_id},
              message="续训任务已入队")


# ═══════════════════════════════════════════════════════════════════
#  版本管理与预览
# ═══════════════════════════════════════════════════════════════════

@router.get("/style/versions")
def style_versions() -> dict[str, Any]:
    """风格 LoRA 版本列表（含 meta、是否当前版本）。"""
    svc = get_style_lora_service()
    versions = svc.list_versions()
    return ok({"items": versions, "total": len(versions),
               "current": svc.get_current()})


@router.post("/style/rollback")
def style_rollback(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """风格 LoRA 版本回滚：{"version": "v3"} → 置为当前生效版本。"""
    version = str((body or {}).get("version", "")).strip()
    if not version:
        raise ApiError(40008, "缺少必填参数: version")
    svc = get_style_lora_service()
    if not svc.rollback(version):
        raise ApiError(80012, f"风格 LoRA 版本不存在: {version}",
                       detail={"version": version,
                               "versions": [v.get("version")
                                            for v in svc.list_versions()]})
    return ok({"version": version, "current": svc.get_current()},
              message=f"已回滚到 {version}")


@router.post("/style/preview")
def style_preview(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """风格预览：应用指定（或当前）版本 LoRA 生成风格化对比帧。

    请求体: {"version"?: str（缺省=当前版本）, "image_path"?: str,
             "max_frames"?: int ≤32, "strength"?: float 0~1（STYLE-023）}
    返回 base64 JPEG 帧序列。基座/版本/依赖未就绪如实返回 80013。
    """
    body = body or {}
    svc = get_style_lora_service()
    version = str(body.get("version") or "").strip() or svc.get_current()
    if not version:
        raise ApiError(80012, "尚无已注册的风格 LoRA 版本",
                       suggestion="先通过 /style/train 训练一个风格版本")
    if not any(v["version"] == version for v in svc.list_versions()):
        raise ApiError(80012, f"风格 LoRA 版本不存在: {version}",
                       detail={"version": version})
    try:
        max_frames = int(body.get("max_frames", 8))
    except (TypeError, ValueError):
        max_frames = 8
    max_frames = max(2, min(max_frames, 32))
    # STYLE-023：强度参数校验（0~1），非法值 → 参数校验失败
    try:
        strength = float(body.get("strength", 1.0))
    except (TypeError, ValueError):
        raise ApiError(40008, "strength 必须是 0~1 的数值") from None
    if not (0.0 <= strength <= 1.0):
        raise ApiError(40008, "strength 必须在 0~1 之间",
                       detail={"min": 0.0, "max": 1.0, "given": strength})

    # 审计 09-10 P2-10：image_path 会直喂 Image.open，限制在产品数据
    # 目录内，防任意本机路径/UNC 外带
    image_path = body.get("image_path")
    if image_path:
        try:
            preview_src = Path(image_path).resolve()
        except OSError:
            raise ApiError(40010, "image_path 不合法") from None
        if not preview_src.is_relative_to(DATA_DIR.resolve()):
            raise ApiError(40010, "image_path 必须在产品数据目录内",
                           detail={"data_dir": str(DATA_DIR)})
        if not preview_src.is_file():
            raise ApiError(40005, "预览图文件不存在",
                           detail={"image_path": image_path})

    try:
        result = svc.preview(version, image_path=image_path,
                             max_frames=max_frames, strength=strength)
    except StylePreviewUnavailable as exc:
        raise ApiError(80013, "风格预览不可用（推理引擎或版本未就绪）",
                       detail={"reason": str(exc), "version": version}) from exc
    return ok(result)


# ═══════════════════════════════════════════════════════════════════
#  状态
# ═══════════════════════════════════════════════════════════════════

@router.get("/style/status")
def style_status() -> dict[str, Any]:
    """风格服务状态：基座就绪 / FFmpeg / 队列 / 当前版本 / 数据集统计。"""
    svc = get_style_lora_service()
    return ok(svc.get_status())


# ═══════════════════════════════════════════════════════════════════
#  风格项目管理（批 2 STYLE-024/025/026/027/028/030/032）
# ═══════════════════════════════════════════════════════════════════

@router.get("/style/list")
def style_list(search: str = "", status: str = "") -> dict[str, Any]:
    """风格项目列表（STYLE-027）：版本即项目实体，支持名称搜索/状态筛选。"""
    svc = get_style_lora_service()
    items = svc.list_versions()
    if search:
        needle = search.lower()
        items = [v for v in items
                 if needle in str(v.get("name", "")).lower()
                 or needle in v["version"].lower()]
    if status:
        items = [v for v in items if v.get("status", "") == status]
    return ok({"items": items, "total": len(items),
               "current": svc.get_current()})


@router.put("/style/{version}")
def style_rename(version: str, body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """重命名风格项目（STYLE-028）：写 meta.json name。"""
    name = str((body or {}).get("name") or "").strip()
    if not name:
        raise ApiError(40008, "缺少必填参数: name")
    svc = get_style_lora_service()
    if not svc.rename_version(version, name[:100]):
        raise ApiError(80012, f"风格 LoRA 版本不存在: {version}",
                       detail={"version": version})
    return ok({"version": version, "name": name})


@router.delete("/style/{version}")
def style_delete(version: str) -> dict[str, Any]:
    """删除风格项目（STYLE-028）：训练中引用拒绝（STYLE_TRAINING_LOCKED）。"""
    svc = get_style_lora_service()
    deleted, reason = svc.delete_version(version)
    if not deleted and reason == "not_found":
        raise ApiError(80012, f"风格 LoRA 版本不存在: {version}",
                       detail={"version": version})
    if not deleted:  # training_locked
        raise ApiError(
            "STYLE_TRAINING_LOCKED",
            "该风格正被进行中的训练任务引用，禁止删除",
            detail={"version": version},
            suggestion="等待训练完成或先取消任务后再删除")
    return ok({"version": version, "deleted": True})


@router.get("/style/{version}/metrics")
def style_metrics(version: str) -> dict[str, Any]:
    """版本质量指标（STYLE-030）：quality_score + 关联训练记录。"""
    svc = get_style_lora_service()
    metrics = svc.version_metrics(version)
    if metrics is None:
        raise ApiError(80012, f"风格 LoRA 版本不存在: {version}",
                       detail={"version": version})
    return ok(metrics)


@router.post("/style/merge")
def style_merge(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """多 LoRA 权重线性融合（STYLE-024）。

    请求体: {"versions": ["v1","v2",...], "weights": [0.6,0.4,...],
             "name"?: str}
    产出新版本目录（adapter 加权和 + meta.merged_from/weights）。
    """
    body = body or {}
    versions = body.get("versions")
    weights = body.get("weights")
    if not isinstance(versions, list) or len(versions) < 2:
        raise ApiError(40008, "versions 至少需要 2 个版本号")
    versions = [str(v).strip() for v in versions]
    if not isinstance(weights, list) or len(weights) != len(versions):
        raise ApiError(40008, "weights 数量须与 versions 一致")
    try:
        weights = [float(w) for w in weights]
    except (TypeError, ValueError):
        raise ApiError(40008, "weights 必须是数值数组") from None
    svc = get_style_lora_service()
    for ver in versions:
        if not any(v["version"] == ver for v in svc.list_versions()):
            raise ApiError(80012, f"风格 LoRA 版本不存在: {ver}",
                           detail={"version": ver})
    try:
        result = svc.merge_versions(versions, weights,
                                    str(body.get("name") or ""))
    except StyleTrainingFailed as exc:
        raise ApiError("LORA_LOAD_FAILED", str(exc)) from exc
    return ok(result, message=f"已合并为新版本 {result['version']}")


@router.post("/style/export")
def style_export(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """导出风格包（STYLE-025）：tar.gz 含 adapter+meta+SHA256 校验文件。"""
    svc = get_style_lora_service()
    version = str((body or {}).get("version") or "").strip() or svc.get_current()
    if not version:
        raise ApiError(80012, "尚无已注册的风格 LoRA 版本")
    try:
        result = svc.export_version(version)
    except StyleTrainingFailed as exc:
        raise ApiError(80012, str(exc), detail={"version": version}) from exc
    return ok(result)


@router.post("/style/clone")
def style_clone(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """克隆风格项目（STYLE-032）：数据集+配置复制为新训练任务入队。"""
    svc = get_style_lora_service()
    version = str((body or {}).get("version") or "").strip()
    if not version:
        raise ApiError(40008, "缺少必填参数: version")
    new_task_id = svc.clone_version(version,
                                    str((body or {}).get("name") or ""))
    if new_task_id is None:
        raise ApiError(80012,
                       f"风格版本不存在或其数据集已失效，无法克隆: {version}",
                       detail={"version": version})
    return ok({"version": version, "new_task_id": new_task_id},
              message="克隆任务已入队")


@router.get("/style/templates")
def style_templates() -> dict[str, Any]:
    """风格模板列表（STYLE-032）。"""
    svc = get_style_lora_service()
    items = svc.list_templates()
    return ok({"items": items, "total": len(items)})


@router.post("/style/templates")
def style_template_save(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """保存风格模板（STYLE-032）：name/style_prompt/超参集合。"""
    if not (body or {}).get("name"):
        raise ApiError(40008, "缺少必填参数: name")
    svc = get_style_lora_service()
    tpl = svc.save_template(body)
    return ok(tpl, message=f"模板已保存: {tpl['name']}")
