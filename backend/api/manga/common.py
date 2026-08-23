"""漫剧 API 共享层：内存态兜底存储 / 行与项目辅助 / 引擎单例 / 出图规格铁律。

TASK-P2-01 自 manga.py 按路由域拆出（原文件 4521 行 → 包）。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

from fastapi import APIRouter

from ...config import (
    DATA_DIR,
)
from ...data.database import get_db_safe, parse_json
from ...data.models import (
    AssetGenerateRequest,
)
from ...middleware.error_handler import ApiError
from ...services.inference.paint_engine import get_paint_engine
from ...services.inference.prompt_translator import translate_prompt_zh2en

router = APIRouter()
log = logging.getLogger("omnispace.api.manga.common")


# ── 内存态模拟存储（数据库不可用时的兜底数据源）──────────────────────────
_storyboards: dict[str, list[dict]] = {}   # project_id -> [分镜行 dict]
_cameras: dict[str, dict] = {}             # camera_id -> 机位 dict
_characters: dict[str, dict] = {}           # character_id -> {position, rotation, scale, locked}
_video_tasks: dict[str, dict] = {}         # task_id -> 任务 dict
_video_cancel_flags: dict[str, bool] = {}  # task_id -> 取消旗标（批 1.7 COMIC-131）
# 视频任务预计剩余时间（2026-08-22）：task_id -> (eta_seconds, 更新时间戳)。
# 引擎 step callback 实时外推，瞬时值内存缓存（免 DB 迁移，任务结束清除）
_video_eta: dict[str, tuple[float, float]] = {}
_voices: list[dict] = [
    {"id": "voice_preset_01", "name": "温柔女声", "character_id": "", "is_preset": True},
    {"id": "voice_preset_02", "name": "沉稳男声", "character_id": "", "is_preset": True},
    {"id": "voice_preset_03", "name": "少年音", "character_id": "", "is_preset": True},
    {"id": "voice_preset_04", "name": "萝莉音", "character_id": "", "is_preset": True},
]

# 导演台默认 stage：API 无 stage/project 上下文，机位/角色统一挂接到此 stage。
# 为满足 director_cameras/director_characters 的 stage_id 外键，需级联保证
# project -> storyboard -> stage 三条记录存在。
_DEFAULT_PROJECT_ID = "__director_default__"
_DEFAULT_STORYBOARD_ID = "__director_default__"
_DEFAULT_STAGE_ID = "__director_default__"

# 占位图（1x1 PNG base64）
_PLACEHOLDER_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLv"
    "AAAAAElFTkSuQmCC"
)
# 占位音频（base64，空 WAV 头），仅用于试听兜底
_PLACEHOLDER_AUDIO = "UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA="

# ── 显式查询列（禁止 SELECT *：列变更可控、避免多余 IO）──────────────────
_SB_COLS = "id, project_id, name, created_at, updated_at"
_SB_ROW_COLS = ("id, storyboard_id, shot_number, original_dialogue, description,"
                " characters, scene, props, voice_id, voice_emotion,"
                " director_stage_done, generation_status, is_ai_generated,"
                " sort_index, camera_type, camera_angle, camera_movement,"
                " duration, transition, speed, volume, music_path, asset_id,"
                " asset_ids, is_locked")

# 批 1.3 导演字段枚举（非法值 → SYSTEM_PARAM_INVALID）
CAMERA_TYPES = ("特写", "近景", "中景", "全景", "远景", "俯拍", "仰拍", "主观镜头")
CAMERA_ANGLES = ("平视", "俯视", "仰视", "侧视", "背面")
CAMERA_MOVEMENTS = ("推", "拉", "摇", "移", "跟", "甩", "升", "降", "静止")
TRANSITIONS = ("淡入淡出", "叠化", "闪白", "闪黑", "推拉", "无")


def _validate_row_director_fields(fields: dict) -> None:
    """批 1.3 导演字段枚举/范围校验；非法值抛 SYSTEM_PARAM_INVALID。"""
    enum_rules = (("camera_type", CAMERA_TYPES), ("camera_angle", CAMERA_ANGLES),
                  ("camera_movement", CAMERA_MOVEMENTS),
                  ("transition", TRANSITIONS))
    for key, allowed in enum_rules:
        val = fields.get(key)
        if val is not None and val != "" and val not in allowed:
            raise ApiError(40008, f"{key} 非法值: {val}",
                           detail={"field": key, "allowed": list(allowed)})
    if "duration" in fields and fields["duration"] is not None:
        dur = float(fields["duration"])
        if dur != 0 and not (1.0 <= dur <= 60.0):
            raise ApiError(40008, "duration 须在 1~60 秒",
                           detail={"field": "duration", "min": 1, "max": 60})
    if "speed" in fields and fields["speed"] is not None:
        spd = float(fields["speed"])
        if not (0.5 <= spd <= 2.0):
            raise ApiError(40008, "speed 须在 0.5~2.0",
                           detail={"field": "speed", "min": 0.5, "max": 2.0})
    if "volume" in fields and fields["volume"] is not None:
        vol = float(fields["volume"])
        if not (-12.0 <= vol <= 0.0):
            raise ApiError(40008, "volume 须在 -12~0 dB",
                           detail={"field": "volume", "min": -12, "max": 0})


_DIR_CHAR_COLS = ("id, stage_id, character_id, name, position, rotation,"
                  " scale, locked")
_DIR_CAM_COLS = "id, stage_id, name, position, rotation, fov"
_VIDEO_TASK_COLS = ("id, storyboard_row_id, description, screenshot_4in1,"
                    " character_assets, audio_path, resolution, fps,"
                    " duration_seconds, codec, model_override, model_used,"
                    " status, progress, file_path, generation_time_ms,"
                    " has_audio_sync, created_at, updated_at")
_VOICE_COLS = ("id, name, character_id, is_preset, file_path, emotion,"
               " created_at")


def _now() -> float:
    return time.time()


def _make_row(shot_number: int, **kw) -> dict:
    """构造一条分镜行（字段对齐 StoryboardRow 模型）。"""
    return {
        "id": kw.get("id") or uuid.uuid4().hex,
        "shot_number": shot_number,
        "original_dialogue": kw.get("original_dialogue", ""),
        "description": kw.get("description", ""),
        "characters": kw.get("characters", []),
        "scene": kw.get("scene", ""),
        "props": kw.get("props", []),
        "voice_id": kw.get("voice_id", ""),
        "voice_emotion": kw.get("voice_emotion", "默认"),
        "director_stage_done": kw.get("director_stage_done", False),
        "generation_status": kw.get("generation_status", "pending"),
        "is_ai_generated": kw.get("is_ai_generated", False),
        "sort_index": kw.get("sort_index", 0),
        "camera_type": kw.get("camera_type", ""),
        "camera_angle": kw.get("camera_angle", ""),
        "camera_movement": kw.get("camera_movement", ""),
        "duration": kw.get("duration", 0),
        "transition": kw.get("transition", ""),
        "speed": kw.get("speed", 1.0),
        "volume": kw.get("volume", 0.0),
        "music_path": kw.get("music_path", ""),
        "asset_id": kw.get("asset_id", ""),
        "asset_ids": kw.get("asset_ids", []),
        "is_locked": kw.get("is_locked", False),
    }


# ═══════════════════════════════════════════════════════════════════
#  分镜表：DB 映射辅助
# ═══════════════════════════════════════════════════════════════════

def _row_to_storyboard_row(r: dict) -> dict:
    """storyboard_rows 行 -> 对外分镜行 dict。

    多资产兼容（竞品对齐）：asset_ids 为空且旧列 asset_id 非空时，
    asset_ids 无缝升级为 [asset_id]；asset_id 恒返回（asset_ids 首元素
    或旧列值），旧读取方无感知。
    """
    legacy_asset_id = r.get("asset_id", "") or ""
    asset_ids = parse_json(r.get("asset_ids"), [])
    if not isinstance(asset_ids, list):
        asset_ids = []
    if not asset_ids and legacy_asset_id:
        asset_ids = [legacy_asset_id]
    return {
        "id": r["id"],
        "shot_number": r.get("shot_number", 0),
        "original_dialogue": r.get("original_dialogue", ""),
        "description": r.get("description", ""),
        "characters": parse_json(r.get("characters"), []),
        "scene": r.get("scene", ""),
        "props": parse_json(r.get("props"), []),
        "voice_id": r.get("voice_id", ""),
        "voice_emotion": r.get("voice_emotion", "默认"),
        "director_stage_done": bool(r.get("director_stage_done", 0)),
        "generation_status": r.get("generation_status", "pending"),
        "is_ai_generated": bool(r.get("is_ai_generated", 0)),
        "sort_index": int(r.get("sort_index", 0) or 0),
        "camera_type": r.get("camera_type", "") or "",
        "camera_angle": r.get("camera_angle", "") or "",
        "camera_movement": r.get("camera_movement", "") or "",
        "duration": float(r.get("duration", 0) or 0),
        "transition": r.get("transition", "") or "",
        "speed": float(r.get("speed", 1.0) or 1.0),
        "volume": float(r.get("volume", 0.0) or 0.0),
        "music_path": r.get("music_path", "") or "",
        "asset_id": asset_ids[0] if asset_ids else legacy_asset_id,
        "asset_ids": asset_ids,
        "is_locked": bool(r.get("is_locked", 0)),
    }


def _public_row_to_db(row: dict, storyboard_id: str, sort_index: int) -> dict:
    """对外分镜行 dict -> storyboard_rows 列值（bool->int，list 交给 insert 序列化）。

    asset_id 旧列与 asset_ids 保持一致：缺省时取 asset_ids 首元素。
    """
    asset_ids = row.get("asset_ids") or []
    if not isinstance(asset_ids, list):
        asset_ids = []
    return {
        "id": row["id"],
        "storyboard_id": storyboard_id,
        "shot_number": row.get("shot_number", 0),
        "original_dialogue": row.get("original_dialogue", ""),
        "description": row.get("description", ""),
        "characters": row.get("characters", []),
        "scene": row.get("scene", ""),
        "props": row.get("props", []),
        "voice_id": row.get("voice_id", ""),
        "voice_emotion": row.get("voice_emotion", "默认"),
        "director_stage_done": int(bool(row.get("director_stage_done", False))),
        "generation_status": row.get("generation_status", "pending"),
        "is_ai_generated": int(bool(row.get("is_ai_generated", False))),
        "sort_index": sort_index,
        "camera_type": row.get("camera_type", "") or "",
        "camera_angle": row.get("camera_angle", "") or "",
        "camera_movement": row.get("camera_movement", "") or "",
        "duration": float(row.get("duration", 0) or 0),
        "transition": row.get("transition", "") or "",
        "speed": float(row.get("speed", 1.0) or 1.0),
        "volume": float(row.get("volume", 0.0) or 0.0),
        "music_path": row.get("music_path", "") or "",
        "asset_id": row.get("asset_id", "") or (asset_ids[0] if asset_ids else ""),
        "asset_ids": asset_ids,
        "is_locked": int(bool(row.get("is_locked", False))),
    }


def _ensure_project(db, project_id: str) -> None:
    """确保 projects 表存在指定项目记录。"""
    if not db.query_one("SELECT id FROM projects WHERE id=?", (project_id,)):
        now = _now()
        db.insert("projects", {
            "id": project_id, "name": "未命名项目", "path": "",
            "created_at": now, "updated_at": now,
        })


def _find_storyboard(db, project_id: str):
    """返回项目的分镜表记录（可能为 None）。

    纯读 helper，不自动补建 projects 行——项目删除后迟到的轮询/读请求
    （分镜列表/视频任务列表/导出等）若经此补建，会让已删项目"复活"成
    关联数据全空的幽灵行。确实需要自动补建的写路径走 _ensure_storyboard
    或显式调用 _ensure_project。
    """
    return db.query_one(
        f"SELECT {_SB_COLS} FROM storyboards WHERE project_id=?",
        (project_id,))


def _ensure_storyboard(db, project_id: str) -> dict:
    """返回项目的分镜表记录，不存在则创建（写路径，级联补建项目行）。"""
    _ensure_project(db, project_id)
    sb = _find_storyboard(db, project_id)
    if sb is None:
        sid = uuid.uuid4().hex
        now = _now()
        db.insert("storyboards", {
            "id": sid, "project_id": project_id, "name": "",
            "created_at": now, "updated_at": now,
        })
        sb = db.query_one(f"SELECT {_SB_COLS} FROM storyboards WHERE id=?",
                          (sid,))
    return sb


def _load_rows(db, storyboard_id: str) -> list[dict]:
    """加载分镜表所有行（按 sort_index 升序）。"""
    rows = db.query(
        f"SELECT {_SB_ROW_COLS} FROM storyboard_rows WHERE storyboard_id=? "
        "ORDER BY sort_index ASC, shot_number ASC",
        (storyboard_id,),
    )
    return [_row_to_storyboard_row(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════
#  导演台：默认 stage 辅助
# ═══════════════════════════════════════════════════════════════════

def _ensure_default_stage(db) -> str:
    """确保默认 director_stage 存在（级联创建 project/storyboard/stage），返回 stage_id。

    导演台 API 无 project/scene 上下文，机位与角色统一挂接到该默认 stage。
    """
    if db.query_one("SELECT id FROM director_stages WHERE id=?",
                    (_DEFAULT_STAGE_ID,)):
        return _DEFAULT_STAGE_ID
    now = _now()
    _ensure_project(db, _DEFAULT_PROJECT_ID)
    if not db.query_one("SELECT id FROM storyboards WHERE id=?",
                        (_DEFAULT_STORYBOARD_ID,)):
        db.insert("storyboards", {
            "id": _DEFAULT_STORYBOARD_ID, "project_id": _DEFAULT_PROJECT_ID,
            "name": "导演台默认", "created_at": now, "updated_at": now,
        })
    db.insert("director_stages", {
        "id": _DEFAULT_STAGE_ID, "storyboard_id": _DEFAULT_STORYBOARD_ID,
        "scene_id": "", "name": "默认场景", "panorama_path": "", "created_at": now,
    })
    return _DEFAULT_STAGE_ID


def _row_to_camera(r: dict) -> dict:
    return {
        "id": r["id"], "name": r.get("name", ""),
        "position": parse_json(r.get("position"), {}),
        "rotation": parse_json(r.get("rotation"), {}),
        "fov": r.get("fov", 60),
    }


def _row_to_character(r: dict) -> dict:
    return {
        "character_id": r.get("character_id", ""),
        "position": parse_json(r.get("position"), {}),
        "rotation": parse_json(r.get("rotation"), {}),
        "scale": r.get("scale", 1.0),
        "locked": bool(r.get("locked", 0)),
    }


# 语音引擎懒加载单例（试听用；引擎未加载模型时 synthesize 走静音占位）
_voice_engine_instance = None
_voice_engine_lock = threading.Lock()

_video_engine_instance = None
_video_engine_lock = threading.Lock()


def _get_video_engine():
    """VideoEngine 进程级单例（视频模型自动装载链复用同一实例）。

    委托 video_engine.get_video_engine()：与 ModelManager 共享同一实例，
    保证显存记账/卸载作用于工作线程实际持有的管线引用。
    """
    global _video_engine_instance
    if _video_engine_instance is None:
        with _video_engine_lock:
            if _video_engine_instance is None:
                from ...services.inference.video_engine import get_video_engine
                _video_engine_instance = get_video_engine()
    return _video_engine_instance


def _get_voice_engine():
    """获取语音引擎单例（懒创建，不在模块导入时实例化）。"""
    global _voice_engine_instance
    if _voice_engine_instance is None:
        with _voice_engine_lock:
            if _voice_engine_instance is None:
                from ...services.inference.voice_engine import VoiceEngine
                _voice_engine_instance = VoiceEngine()
    return _voice_engine_instance


def _sovits_degrade_clause() -> str:
    """生成 degrade_reason 中的 GPT-SoVITS（F-08）说明子句。

    依据引擎 get_status().sovits 探测结果如实描述：权重随包但代码包缺失时
    说明门控原因；权重未随包时仅简述。探测异常时返回空串不影响主文案。
    """
    try:
        sovits = _get_voice_engine().get_status().get("sovits", {})
    except Exception:  # noqa: BLE001 - 探测失败不阻断降级文案
        return ""
    if sovits.get("weights_ready") and sovits.get("code_missing"):
        return ("GPT-SoVITS 权重已随包但缺官方推理代码包与 pypinyin（中文 G2P），"
                "已按诚实降级门控跳过；")
    if not sovits.get("weights_ready"):
        return "GPT-SoVITS 权重未随包；"
    return ""
_COMIC_ASSET_DIR = DATA_DIR / "comic_assets"
_KEYFRAME_DIR = DATA_DIR / "keyframes"


# ═══════════════════════════════════════════════════════════════════
#  批 1.4 资产图链路（COMIC-025~037、138）
# ═══════════════════════════════════════════════════════════════════

_ASSET_COLS = "id, project_id, kind, name, file_path, prompt, meta, created_at, scope"

# ── 出图统一规格与参考图风格对齐（2026-08-14 用户铁律）──────────────
# 资产图/分镜图统一出图 2560×1440（16:9）。SDXL 直出 2560×1440 构图
# 崩坏且显存紧张，采用 1280×720（恰为 1/2）生成 + LANCZOS 2x 上采样，
# 兼顾画质/速度/显存（16GB 基线）。
IMG_TARGET_W, IMG_TARGET_H = 2560, 1440
_IMG_GEN_MAX = 1344                      # SDXL 友好生成上限

# 参考图（E:\OmniSpace\参考图.png）风格：写实人像、柔和影棚光、纯白底、
# 干净排版的人设图。风格词统一缀尾（用户提示词仍置首，77 token 截断
# 保护不受影响）。
_STYLE_PHOTO = (", photorealistic, realistic photograph, soft studio "
                "lighting, clean composition, detailed skin texture, "
                "sharp focus, high detail")
_STYLE_WHITE_BG = ", pure white background"
_STYLE_NEGATIVE = (
    "anime, cartoon, comic, illustration, painting, drawing, sketch, "
    "3d render, cgi, doll, lowres, bad anatomy, bad hands, "
    "missing fingers, extra fingers, blurry, watermark, text, logo, "
    "cropped, worst quality, jpeg artifacts")


def _gen_size_for_target(width: int, height: int) -> tuple[int, int]:
    """目标出图尺寸 → SDXL 实际生成尺寸（约 1/2，2x 上采样无小数插值）。"""
    gw = max(256, min(width // 2, _IMG_GEN_MAX)) // 8 * 8
    gh = max(256, min(height // 2, _IMG_GEN_MAX)) // 8 * 8
    return gw, gh


def _upscale_to(image, width: int, height: int):
    """LANCZOS 重采样至目标出图尺寸（已达标则原样返回）。"""
    if image.size == (width, height):
        return image
    from PIL import Image
    return image.resize((width, height), Image.LANCZOS)

# 资产类型 → 提示词模板/子目录
_ASSET_KIND_CONF = {
    # 模板铁律：用户提示词必须置首——SDXL 单编码器 77 token 截断窗口，
    # 置首可保证超长时只丢尾部通用词缀而非用户细节；禁止硬编码风格词
    # （如 anime style），避免与用户指定风格（如 3D 游戏风）打架。
    "character": {"subdir": "characters",
                  "tpl": "{prompt}, full body, clean background, high quality"},
    "scene": {"subdir": "scenes",
              "tpl": "{prompt}, no people, wide shot, high quality"},
    "prop": {"subdir": "props",
             "tpl": "{prompt}, single object, centered, plain background, high quality"},
}


def _norm_asset_name(name: str) -> str:
    """资产名归一化（折叠空白 + 小写），用于同名资产桩匹配。"""
    return " ".join((name or "").split()).lower()


def _find_character_asset_stub(db, project_id: str,
                               name: str) -> dict | None:
    """查找同项目同名 character 资产桩（infer-entities 创建或无图片版本）。

    名称按大小写/空白归一匹配；命中返回资产行 dict，否则 None。
    已有图片的同角色资产不算桩（避免覆盖正式资产）。
    """
    target = _norm_asset_name(name)
    if not target:
        return None
    for r in db.query(
            f"SELECT {_ASSET_COLS} FROM comic_assets"
            " WHERE project_id=? AND kind='character'", (project_id,)):
        if _norm_asset_name(r.get("name") or "") != target:
            continue
        meta = parse_json(r.get("meta"), {})
        if meta.get("source") == "infer_entities" \
                or not (r.get("file_path") or "").strip():
            return r
    return None


def _generate_asset_sync(req: AssetGenerateRequest, kind: str,
                         prompt_en_override: str | None = None) -> dict:
    """同步执行一个资产生成（SDXL 文生图 → 落盘 → 登记 comic_assets 表）。

    由线程池调用（端点为 async，避免阻塞事件循环）。
    引擎未就绪抛 PAINT_ENGINE_NOT_READY（COMIC-125 同语义）。
    prompt_en_override: 批量端点整批预译的英文提示词（DB 仍存原文）；
    为 None 时此处现译（单资产生成路径）。
    """
    conf = _ASSET_KIND_CONF[kind]
    # 中文描述词先译英（SDXL CLIP 不理解中文，直送会塌缩为模板词）。
    # 必须在 paint ensure_loaded 之前翻译：译后绘画引擎腾挪显存卸载
    # 对话模型，避免双模型换载抖动。
    prompt_en = prompt_en_override or translate_prompt_zh2en(req.prompt)
    engine = get_paint_engine()
    if not engine.is_ready and not engine.ensure_loaded(None):
        status = engine.get_status()
        raise ApiError("PAINT_ENGINE_NOT_READY",
                       status.get("last_error") or "绘画模型未就绪")
    # 参考图风格对齐：角色/道具纯白底人设图风，场景写实影调不加白底
    style = _STYLE_PHOTO + (_STYLE_WHITE_BG if kind in ("character", "prop")
                            else "")
    prompt = conf["tpl"].format(prompt=prompt_en) + style
    # 半分辨率生成 + LANCZOS 上采样至目标尺寸（2560×1440 直出会构图崩坏）
    gen_w, gen_h = _gen_size_for_target(req.width, req.height)
    params = {"prompt": prompt, "negative": _STYLE_NEGATIVE,
              "steps": 24, "cfg": 7.0,
              "width": gen_w, "height": gen_h, "seed": -1}
    result = engine.generate(params)
    image = _upscale_to(result["images"][0], req.width, req.height)
    if kind == "prop" and req.transparent:
        # 道具透明背景（PIL 经典阈值抠图；SAM 未接 /art/segment 前的降级）
        image = _remove_background(image)
    asset_id = uuid.uuid4().hex
    out_dir = _COMIC_ASSET_DIR / req.project_id / conf["subdir"] / req.name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / ("portrait.png" if kind == "character" else "image.png")
    image.save(out_path, "PNG")
    rel_path = str(out_path.relative_to(DATA_DIR)).replace("\\", "/")
    meta = {"width": req.width, "height": req.height,
            "gen_width": gen_w, "gen_height": gen_h,
            "seed": result.get("seed", -1), "model": result.get("model", ""),
            "transparent": bool(req.transparent and kind == "prop"),
            "prompt_en": prompt_en}
    db = get_db_safe()
    if db is not None:
        db.insert("comic_assets", {
            "id": asset_id, "project_id": req.project_id, "kind": kind,
            "name": req.name, "file_path": rel_path, "prompt": req.prompt,
            "meta": meta, "created_at": _now()})
    return {"asset_id": asset_id, "project_id": req.project_id, "kind": kind,
            "name": req.name, "file_path": rel_path, "prompt": req.prompt,
            "meta": meta}


def _remove_background(image):
    """PIL 阈值抠图（四角采样背景色 → 相近色透明化）。无 SAM 时的经典降级。"""
    img = image.convert("RGBA")
    px = img.load()
    w, h = img.size
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    bg = tuple(sum(c[i] for c in corners) // 4 for i in range(3))
    threshold = 40
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if (abs(r - bg[0]) < threshold and abs(g - bg[1]) < threshold
                    and abs(b - bg[2]) < threshold):
                px[x, y] = (r, g, b, 0)
    return img


# ═══════════════════════════════════════════════════════════════════
#  批 1.6 关键帧 CRUD（COMIC-121~125）
# ═══════════════════════════════════════════════════════════════════

_KF_COLS = ("id, row_id, project_id, version, file_path, prompt,"
            " status, error, is_current, created_at")


# ═══════════════════════════════════════════════════════════════════
#  G1 — 解说漫剧：故事生词 / 故事生图 / 视频生词
# ═══════════════════════════════════════════════════════════════════

def _load_project_rows(project_id: str) -> tuple:
    """加载项目分镜行（按 sort_index 排序），返回 (db, storyboard, rows)。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用")
    sb = _find_storyboard(db, project_id)
    if sb is None:
        raise ApiError(40005, "项目不存在", detail={"project_id": project_id})
    rows = _load_rows(db, sb["id"])
    return db, sb, rows
