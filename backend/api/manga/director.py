"""漫剧导演台路由：全景图 / 4合1截图 / 机位与角色 / 文本转3D。

TASK-P2-01 自 manga.py 按路由域拆出（原文件 4521 行 → 包）。
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Body

from ...config import (
    DATA_DIR,
    PANORAMA_RESOLUTIONS,
)
from ...data.database import get_db_safe
from ...data.models import (
    CameraAdd,
    CameraUpdate,
    CharacterLock,
    CharacterPositionUpdate,
    PanoramaRequest,
    ScreenshotRequest,
    TextTo3DRequest,
)
from ...middleware.error_handler import ApiError, ok
from ...services.inference.paint_engine import get_paint_engine
from ...services.offload import run_blocking
from .common import (
    _DIR_CAM_COLS,
    _DIR_CHAR_COLS,
    _PLACEHOLDER_PNG,
    _cameras,
    _characters,
    _ensure_default_stage,
    _now,
    _row_to_camera,
    _row_to_character,
)

router = APIRouter()
log = logging.getLogger("omnispace.api.manga.director")



# ═══════════════════════════════════════════════════════════════════
#  导演台端点
# ═══════════════════════════════════════════════════════════════════

@router.post("/manga/director/panorama")
@router.post("/director/panorama")  # 顶层别名（文档 §7.1.4 /v1/director）
async def director_panorama(req: PanoramaRequest):
    """生成全景图（规格 §4.4）。

    审计说明：3D 渲染在前端 Three.js 画布执行，后端无渲染引擎。
    本端点返回占位图并携带 degraded 诚实标记（与绘画/语音降级一致），
    前端应以本地 Three.js 渲染结果为准。
    """
    if req.resolution not in PANORAMA_RESOLUTIONS:
        raise ApiError(40010, "不支持的全景分辨率",
                       detail={"allowed": PANORAMA_RESOLUTIONS})
    return ok({
        "scene_id": req.scene_id,
        "resolution": req.resolution,
        "panorama": _PLACEHOLDER_PNG,
        "degraded": True,
        "degrade_reason": "后端无 3D 渲染引擎：全景图为占位图，"
                          "真实渲染由前端 Three.js 画布执行",
        "generated_at": _now(),
    })


@router.post("/manga/director/screenshot-4in1")
@router.post("/director/screenshot-4in1")  # 顶层别名
async def director_screenshot_4in1(req: ScreenshotRequest):
    """4合1截图（规格 §4.4）。必须恰好4个机位，否则 70003。

    同 panorama：后端无 3D 渲染引擎，返回占位图 + degraded 诚实标记。
    """
    if len(req.camera_ids) != 4:
        raise ApiError(70003, "4合1截图需要恰好4个机位",
                       detail={"provided": len(req.camera_ids), "required": 4})
    return ok({
        "scene_id": req.scene_id,
        "camera_ids": req.camera_ids,
        "screenshot": _PLACEHOLDER_PNG,
        "layout": "2x2",
        "degraded": True,
        "degrade_reason": "后端无 3D 渲染引擎：截图为占位图，"
                          "真实渲染由前端 Three.js 画布执行",
        "generated_at": _now(),
    })


@router.post("/manga/director/export")
@router.post("/director/export")  # 顶层别名（文档 §7.1.4 /director/export，R2-B08）
def director_export(body: dict = Body(default_factory=dict)):
    """导演台导出（文档 §7.1.4，审计 R2-B08）。

    导出当前（或指定）stage 的完整导演台状态为 JSON 包：
    场景元信息 + 角色布局 + 机位列表。全部为数据库真实状态，
    供前端下载存档或导入复用；图片类资产由前端 Three.js 渲染导出。
    """
    stage_id = str((body or {}).get("stage_id") or "").strip()
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_INTERNAL_ERROR", "数据库不可用，无法导出导演台状态")
    if not stage_id:
        stage_id = _ensure_default_stage(db)
    stage = db.query_one(
        "SELECT id, storyboard_id, scene_id, name, panorama_path, created_at"
        " FROM director_stages WHERE id=?", (stage_id,))
    if stage is None:
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", "导演台场景不存在",
                       detail={"stage_id": stage_id})
    chars = db.query(
        f"SELECT {_DIR_CHAR_COLS} FROM director_characters WHERE stage_id=?",
        (stage_id,))
    cams = db.query(
        f"SELECT {_DIR_CAM_COLS} FROM director_cameras WHERE stage_id=?",
        (stage_id,))
    return ok({
        "stage": {
            "id": stage["id"], "storyboard_id": stage.get("storyboard_id", ""),
            "scene_id": stage.get("scene_id", ""), "name": stage.get("name", ""),
            "panorama_path": stage.get("panorama_path", ""),
            "created_at": stage.get("created_at", 0),
        },
        "characters": [_row_to_character(r) for r in chars],
        "cameras": [_row_to_camera(r) for r in cams],
        "exported_at": _now(),
        "format": "omnispace-director-stage/v1",
    })


@router.post("/manga/director/character/position")
@router.post("/director/character/position")  # 顶层别名
def director_character_position(req: CharacterPositionUpdate):
    """更新角色位置（规格 §4.4）。"""
    db = get_db_safe()
    if db is not None:
        try:
            stage_id = _ensure_default_stage(db)
            row = db.query_one(
                f"SELECT {_DIR_CHAR_COLS} FROM director_characters"
                " WHERE character_id=?",
                (req.character_id,),
            )
            if row is None:
                cid = uuid.uuid4().hex
                db.insert("director_characters", {
                    "id": cid, "stage_id": stage_id,
                    "character_id": req.character_id, "name": "",
                    "position": req.position,
                    "rotation": req.rotation or {},
                    "scale": req.scale if req.scale is not None else 1.0,
                    "locked": 0,
                })
                row = db.query_one(
                    f"SELECT {_DIR_CHAR_COLS} FROM director_characters"
                    " WHERE id=?", (cid,))
            else:
                data = {"position": req.position}
                if req.rotation is not None:
                    data["rotation"] = req.rotation
                if req.scale is not None:
                    data["scale"] = req.scale
                db.update("director_characters", data,
                          "character_id=?", (req.character_id,))
                row = db.query_one(
                    f"SELECT {_DIR_CHAR_COLS} FROM director_characters"
                " WHERE character_id=?",
                    (req.character_id,),
                )
            return ok({"character_id": req.character_id,
                       "character": _row_to_character(row)})
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库写入失败，降级内存存储: %s", exc)

    char = _characters.setdefault(req.character_id, {
        "position": {"x": 0, "y": 0, "z": 0}, "locked": False,
    })
    char["position"] = req.position
    if req.rotation is not None:
        char["rotation"] = req.rotation
    if req.scale is not None:
        char["scale"] = req.scale
    return ok({"character_id": req.character_id, "character": char})


@router.post("/manga/director/camera/add")
@router.post("/director/camera/add")  # 顶层别名
def director_camera_add(req: CameraAdd):
    """添加机位（规格 §4.4）。"""
    cid = uuid.uuid4().hex
    camera = {
        "id": cid, "name": req.name, "position": req.position,
        "rotation": req.rotation, "fov": req.fov,
    }
    db = get_db_safe()
    if db is not None:
        try:
            stage_id = _ensure_default_stage(db)
            db.insert("director_cameras", {
                "id": cid, "stage_id": stage_id, "name": req.name,
                "position": req.position, "rotation": req.rotation, "fov": req.fov,
            })
            return ok({"camera": camera}, message="机位已添加")
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库写入失败，降级内存存储: %s", exc)
    _cameras[cid] = camera
    return ok({"camera": camera}, message="机位已添加")


@router.put("/manga/director/camera/{camera_id}")
@router.put("/director/camera/{camera_id}")  # 顶层别名
def director_camera_update(camera_id: str, req: CameraUpdate):
    """更新机位（规格 §4.4）。仅更新非空字段。"""
    db = get_db_safe()
    if db is not None:
        try:
            row = db.query_one(
                f"SELECT {_DIR_CAM_COLS} FROM director_cameras WHERE id=?",
                (camera_id,))
            if row is None:
                raise ApiError(40005, "机位不存在", detail={"camera_id": camera_id})
            update_fields = req.model_dump(exclude_none=True)
            if update_fields:
                db.update("director_cameras", update_fields, "id=?", (camera_id,))
                row = db.query_one(
                    f"SELECT {_DIR_CAM_COLS} FROM director_cameras WHERE id=?",
                    (camera_id,))
            return ok({"camera": _row_to_camera(row)})
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库更新失败，降级内存存储: %s", exc)

    camera = _cameras.get(camera_id)
    if camera is None:
        raise ApiError(40005, "机位不存在", detail={"camera_id": camera_id})
    camera.update(req.model_dump(exclude_none=True))
    return ok({"camera": camera})


def _set_character_lock(character_id: str, locked: bool) -> dict:
    """锁定/解锁角色占位的内部实现。"""
    db = get_db_safe()
    if db is not None:
        try:
            stage_id = _ensure_default_stage(db)
            row = db.query_one(
                f"SELECT {_DIR_CHAR_COLS} FROM director_characters"
                " WHERE character_id=?",
                (character_id,),
            )
            if row is None:
                cid = uuid.uuid4().hex
                db.insert("director_characters", {
                    "id": cid, "stage_id": stage_id,
                    "character_id": character_id, "name": "",
                    "position": {}, "rotation": {}, "scale": 1.0,
                    "locked": int(locked),
                })
            else:
                db.update("director_characters", {"locked": int(locked)},
                          "character_id=?", (character_id,))
            return ok({"character_id": character_id, "locked": locked})
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库写入失败，降级内存存储: %s", exc)

    char = _characters.setdefault(character_id, {
        "position": {"x": 0, "y": 0, "z": 0}, "locked": False,
    })
    char["locked"] = locked
    return ok({"character_id": character_id, "locked": locked})


@router.post("/manga/director/character/lock")
@router.post("/director/character/lock")  # 顶层别名
def director_character_lock(req: CharacterLock):
    """锁定占位（规格 §4.4）。"""
    return _set_character_lock(req.character_id, True)


@router.post("/manga/director/character/unlock")
@router.post("/director/character/unlock")  # 顶层别名
def director_character_unlock(req: CharacterLock):
    """解锁占位（规格 §4.4）。"""
    return _set_character_lock(req.character_id, False)


# ═══════════════════════════════════════════════════════════════════
#  视频生成
# ═══════════════════════════════════════════════════════════════════

# 视频降级管线标识与诚实降级文案（审计 BK-014）：
# LTX-2/Wan2.1/CogVideoX 未随包时视频生成走 Ken Burns 图片推拉 + FFmpeg
# 降级管线（产出真实可播放文件但非 AI 生成视频），响应必须携带
# degraded:true 与 degrade_reason 中文字段告知前端。
_FALLBACK_VIDEO_MODEL = "fallback-kenburns"
_VIDEO_DEGRADE_REASON = (
    "AI 视频模型（LTX-2/Wan2.1/CogVideoX）未随包安装，本视频由 Ken Burns "
    "图片推拉降级管线生成（真实可播放文件，非 AI 生成视频）")


def _text_to_3d_sync(req: TextTo3DRequest) -> dict:
    """同步执行文生 3D（线程池调用）：SDXL 概念图 → TripoSR image-to-3D。

    TripoSR 官方管线为 image-to-3D：本文生入口先用 SDXL 生成概念图
    （单主体居中 + 简洁背景模板，贴合 TripoSR 训练分布），再经
    TripoSR 生成带顶点色的 glb 网格。任一环缺失即诚实报错，绝不
    伪造网格产物。
    """
    from ..services.inference.triposr_engine import get_triposr_engine

    tsr = get_triposr_engine()
    if not tsr.is_ready and not tsr.load_model():
        status = tsr.get_status()
        code = "MODEL_FILE_NOT_FOUND" if not status.get("weights_ready") \
            else "MODEL_LOAD_FAILED"
        raise ApiError(
            code,
            status.get("unavailable_reason") or status.get("last_error")
            or "TripoSR 引擎不可用",
            detail={"missing_deps": status.get("missing_deps")})

    engine = get_paint_engine()
    if not engine.is_ready and not engine.ensure_loaded(None):
        status = engine.get_status()
        raise ApiError("PAINT_ENGINE_NOT_READY",
                       status.get("last_error") or "绘画模型未就绪")

    concept_prompt = (
        f"{req.prompt}, single object, centered, full view, "
        "simple solid light background, studio lighting, high quality")
    result = engine.generate({
        "prompt": concept_prompt, "negative": "", "steps": 24, "cfg": 7.0,
        "width": 512, "height": 512, "seed": -1})
    image = result["images"][0]

    out_dir = DATA_DIR / "generated" / "3d" / (req.project_id or "default")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    concept_path = out_dir / f"concept_{stamp}.png"
    image.save(concept_path, "PNG")

    gen = tsr.generate_3d(image, out_dir, mc_resolution=256)
    return {
        "prompt": req.prompt,
        "project_id": req.project_id or "default",
        "concept_image": str(concept_path),
        "glb_path": gen["glb_path"],
        "vertices": gen["vertices"],
        "faces": gen["faces"],
        "elapsed_s": gen["elapsed_s"],
        "backend": gen["backend"],
        "degraded": False,
        "note": "文生 3D 经 SDXL 概念图中转（TripoSR 为 image-to-3D 管线）",
    }


@router.post("/director/text-to-3d")
async def director_text_to_3d(req: TextTo3DRequest):
    """文生 3D（COMIC-105）：SDXL 概念图 → TripoSR 真实网格生成。

    成功返回 glb_path / vertices / faces（degraded=false）；
    TripoSR 权重或依赖缺失 → MODEL_FILE_NOT_FOUND / MODEL_LOAD_FAILED；
    绘画引擎未就绪 → PAINT_ENGINE_NOT_READY。绝不伪造 glb 网格产物。
    """
    try:
        data = await run_blocking(_text_to_3d_sync, req)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ApiError("MODEL_LOAD_FAILED", f"文生 3D 失败: {exc}") from exc
    return ok(data)
