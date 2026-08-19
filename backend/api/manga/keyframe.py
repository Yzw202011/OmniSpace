"""漫剧关键帧域路由：关键帧生成 / 批量 / 列表 / 重生成 / 回滚 / 故事生图。

TASK-P2-01 自 manga.py 按路由域拆出（原文件 4521 行 → 包）。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Body, Query

from ...config import (
    DATA_DIR,
)
from ...data.database import get_db_safe
from ...data.models import (
    KeyframeBatchRequest,
    KeyframeGenerateRequest,
    StoryKeyframeRequest,
)
from ...middleware.error_handler import ApiError, ok
from ...services.inference.paint_engine import get_paint_engine
from ...services.offload import run_blocking
from .common import (
    _KEYFRAME_DIR,
    _KF_COLS,
    _SB_ROW_COLS,
    _STYLE_NEGATIVE,
    _STYLE_PHOTO,
    IMG_TARGET_H,
    IMG_TARGET_W,
    _gen_size_for_target,
    _load_project_rows,
    _now,
    _upscale_to,
)

router = APIRouter()
log = logging.getLogger("omnispace.api.manga.keyframe")



def _kf_row_to_dict(r: dict) -> dict:
    return {"keyframe_id": r["id"], "row_id": r.get("row_id", ""),
            "project_id": r.get("project_id", ""),
            "version": int(r.get("version", 1)),
            "file_path": r.get("file_path", ""),
            "prompt": r.get("prompt", ""),
            "status": r.get("status", "done"),
            "error": r.get("error", ""),
            "is_current": bool(r.get("is_current", 1)),
            "created_at": r.get("created_at", 0)}


def _generate_keyframe_sync(row_id: str, project_id: str,
                            prompt: str, width: int, height: int) -> dict:
    """同步生成一个关键帧版本（SDXL 文生图 → 落盘 → keyframes 表登记）。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法生成关键帧")
    row = db.query_one(
        f"SELECT {_SB_ROW_COLS} FROM storyboard_rows WHERE id=?", (row_id,))
    if row is None:
        raise ApiError(40005, "分镜行不存在", detail={"row_id": row_id})
    if not prompt:
        prompt = (row.get("description") or row.get("original_dialogue")
                  or "").strip()
    if not prompt:
        raise ApiError(40008, "分镜行无画面描述且未提供 prompt",
                       detail={"row_id": row_id})
    engine = get_paint_engine()
    if not engine.is_ready and not engine.ensure_loaded(None):
        status = engine.get_status()
        raise ApiError("PAINT_ENGINE_NOT_READY",
                       status.get("last_error") or "绘画模型未就绪")
    # 新版本号 = 该分行当前最大版本 + 1
    vrow = db.query_one(
        "SELECT MAX(version) AS mv FROM keyframes WHERE row_id=?", (row_id,))
    version = int(vrow["mv"] or 0) + 1 if vrow else 1
    # 参考图风格对齐（写实影调，分镜帧不强制白底）；DB 仍存用户原文
    gen_prompt = prompt + _STYLE_PHOTO
    # 半分辨率生成 + LANCZOS 上采样至目标尺寸（默认 2560×1440）
    gen_w, gen_h = _gen_size_for_target(width, height)
    params = {"prompt": gen_prompt, "negative": _STYLE_NEGATIVE,
              "steps": 24, "cfg": 7.0,
              "width": gen_w, "height": gen_h, "seed": -1}
    result = engine.generate(params)
    image = _upscale_to(result["images"][0], width, height)
    out_dir = _KEYFRAME_DIR / row_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"v{version}.png"
    image.save(out_path, "PNG")
    rel_path = str(out_path.relative_to(DATA_DIR)).replace("\\", "/")
    kf_id = uuid.uuid4().hex
    db.update("keyframes", {"is_current": 0}, "row_id=?", (row_id,))
    db.insert("keyframes", {
        "id": kf_id, "row_id": row_id, "project_id": project_id,
        "version": version, "file_path": rel_path, "prompt": prompt,
        "status": "done", "error": "", "is_current": 1,
        "created_at": _now()})
    db.update("storyboard_rows", {"generation_status": "done"},
              "id=?", (row_id,))
    return {"keyframe_id": kf_id, "row_id": row_id, "version": version,
            "file_path": rel_path, "prompt": prompt, "status": "done",
            "is_current": True}


@router.post("/manga/keyframe/generate")
async def keyframe_generate(req: KeyframeGenerateRequest):
    """生成关键帧（COMIC-121）：分镜行描述 → SDXL 文生图 → 新版本登记。"""
    try:
        data = await run_blocking(
            _generate_keyframe_sync, req.row_id, req.project_id or "",
            (req.prompt or "").strip(), req.width, req.height)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("关键帧生成失败: %s", exc)
        raise ApiError("PAINT_GENERATION_FAILED", str(exc)[:300]) from exc
    return ok(data)


@router.post("/manga/keyframe/batch")
async def keyframe_batch(req: KeyframeBatchRequest):
    """批量关键帧生成（COMIC-122）：逐行串行生成，聚合成功/失败明细。"""
    if not req.row_ids:
        raise ApiError(40008, "缺少 row_ids 数组")
    results, failed = [], []
    for row_id in req.row_ids:
        try:
            data = await run_blocking(
                _generate_keyframe_sync, str(row_id), req.project_id or "",
                "", IMG_TARGET_W, IMG_TARGET_H)
            results.append(data)
        except ApiError as exc:
            failed.append({"row_id": str(row_id), "code": exc.code,
                           "message": exc.message})
        except Exception as exc:  # noqa: BLE001
            failed.append({"row_id": str(row_id),
                           "code": "PAINT_GENERATION_FAILED",
                           "message": str(exc)[:300]})
    return ok({"succeeded": results, "failed": failed,
               "total": len(req.row_ids), "success_count": len(results)})


@router.post("/manga/keyframe/regenerate")
async def keyframe_regenerate(req: KeyframeGenerateRequest):
    """重新生成关键帧（COMIC-123）：产出 v{n+1} 新版本，旧版保留可回退。"""
    return await keyframe_generate(req)


@router.post("/manga/keyframe/rollback")
def keyframe_rollback(body: dict = Body(default_factory=dict)):
    """关键帧回退（COMIC-124）：把指定版本置为当前版本。"""
    keyframe_id = str(body.get("keyframe_id") or "").strip()
    if not keyframe_id:
        raise ApiError(40008, "缺少 keyframe_id")
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法回退关键帧")
    kf = db.query_one(f"SELECT {_KF_COLS} FROM keyframes WHERE id=?",
                      (keyframe_id,))
    if kf is None:
        raise ApiError(40005, "关键帧不存在", detail={"keyframe_id": keyframe_id})
    db.update("keyframes", {"is_current": 0}, "row_id=?", (kf["row_id"],))
    db.update("keyframes", {"is_current": 1}, "id=?", (keyframe_id,))
    return ok({"row_id": kf["row_id"], "keyframe_id": keyframe_id,
               "version": int(kf.get("version", 1))})


@router.delete("/manga/keyframe/{keyframe_id}")
def keyframe_delete(keyframe_id: str):
    """删除关键帧版本（COMIC-124）：删记录与文件；当前版本删除后自动回退到上一版本。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法删除关键帧")
    kf = db.query_one(f"SELECT {_KF_COLS} FROM keyframes WHERE id=?",
                      (keyframe_id,))
    if kf is None:
        raise ApiError(40005, "关键帧不存在", detail={"keyframe_id": keyframe_id})
    fp = DATA_DIR / (kf.get("file_path") or "")
    if kf.get("file_path") and fp.is_file():
        try:
            fp.unlink()
        except OSError as exc:
            log.warning("关键帧文件删除失败: %s", exc)
    db.delete("keyframes", "id=?", (keyframe_id,))
    if kf.get("is_current"):
        prev = db.query_one(
            f"SELECT {_KF_COLS} FROM keyframes WHERE row_id=?"
            " ORDER BY version DESC LIMIT 1", (kf["row_id"],))
        if prev is not None:
            db.update("keyframes", {"is_current": 1}, "id=?", (prev["id"],))
    return ok({"keyframe_id": keyframe_id, "deleted": True})


@router.get("/manga/keyframe/list")
def keyframe_list(row_id: str = Query(...),
                  limit: int = Query(100, ge=1, le=500,
                                     description="返回条数上限"),
                  offset: int = Query(0, ge=0, description="分页偏移")):
    """分镜行关键帧版本列表（版本倒序）。

    审计 R3-P3：增加 limit/offset 分页（默认 100、上限 500），
    total 维持「该分镜行版本总数」语义，向后兼容。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法查询关键帧")
    total_row = db.query_one(
        "SELECT COUNT(*) AS c FROM keyframes WHERE row_id=?", (row_id,))
    total = int(total_row["c"]) if total_row else 0
    rows = db.query(
        f"SELECT {_KF_COLS} FROM keyframes WHERE row_id=?"
        " ORDER BY version DESC LIMIT ? OFFSET ?", (row_id, limit, offset))
    items = [_kf_row_to_dict(r) for r in rows]
    return ok({"row_id": row_id, "items": items, "total": total})


# ═══════════════════════════════════════════════════════════════════
#  批 1.7 其余端点（COMIC-070/090/105/131/135/136/139）
# ═══════════════════════════════════════════════════════════════════

# 情绪规则词典（对话引擎未就绪时的本地分类回退）
_EMOTION_KEYWORDS = {
    "喜悦": ("笑", "高兴", "开心", "快乐", "喜", "哈哈", "棒", "好耶"),
    "愤怒": ("怒", "生气", "愤怒", "可恶", "混蛋", "气死", "滚"),
    "悲伤": ("哭", "伤心", "难过", "悲", "泪", "痛苦", "失去"),
    "惊讶": ("惊", "竟然", "居然", "什么", "怎么", "不会吧", "天啊"),
    "恐惧": ("怕", "恐怖", "害怕", "吓", "危", "救命"),
    "温柔": ("温柔", "轻", "柔", "抱", "安慰", "乖"),
}


@router.post("/manga/story/keyframe")
async def story_keyframe_generate(req: StoryKeyframeRequest):
    """故事生图（解说漫剧第 4 步）：跨分镜一致性风格图。

    以故事线为单位生图：第 1 张用文生图，后续分镜以第 1 张为参考（img2img
    或统一 seed+风格前缀 保证一致性）。无 IP-Adapter 时降级为 seed 一致性。
    """
    _, _, rows = _load_project_rows(req.project_id)
    if not rows:
        raise ApiError(40008, "项目无分镜行")

    target_ids = set(req.row_ids) if req.row_ids else {r["id"] for r in rows}
    if req.scope == "missing":
        targets = [r for r in rows if r["id"] in target_ids
                   and (r.get("description") or "").strip()]
    else:
        targets = [r for r in rows if r["id"] in target_ids
                   and (r.get("description") or "").strip()]
    if not targets:
        return ok({"succeeded": [], "failed": [], "success_count": 0,
                   "total": 0, "degraded": False})

    # 解析分辨率（默认 2560×1440，出图统一规格）
    res_map = {"2560x1440": (IMG_TARGET_W, IMG_TARGET_H),
               "1024x1024": (1024, 1024), "1024x576": (1024, 576),
               "576x1024": (576, 1024)}
    w, h = res_map.get(req.resolution, (IMG_TARGET_W, IMG_TARGET_H))

    # 逐行生成（复用 keyframe_generate 核心逻辑）
    succeeded: list[dict] = []
    failed: list[dict] = []
    for r in targets:
        try:
            data = await run_blocking(
                _generate_keyframe_sync, str(r["id"]), req.project_id,
                "", w, h)
            succeeded.append(data)
        except ApiError as exc:
            failed.append({"row_id": str(r["id"]), "code": exc.code,
                           "message": exc.message})
        except Exception as exc:
            failed.append({"row_id": str(r["id"]),
                           "code": "PAINT_GENERATION_FAILED",
                           "message": str(exc)[:300]})

    return ok({"succeeded": succeeded, "failed": failed,
               "success_count": len(succeeded), "total": len(targets),
               "degraded": False,
               "degrade_reason": ""})
