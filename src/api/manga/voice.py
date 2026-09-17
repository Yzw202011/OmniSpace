"""漫剧音色域路由：音色列表 / 绑定 / 情感 / 试听 / 上传克隆。

TASK-P2-01 自 manga.py 按路由域拆出（原文件 4521 行 → 包）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, File, Query, UploadFile

from ...config import (
    DATA_DIR,
    VOICE_PRESET_EMOTIONS,
)
from ...data.database import get_db_safe
from ...data.models import (
    VoiceBindRequest,
    VoiceEmotionUpdate,
    VoicePreviewRequest,
)
from ...middleware.error_handler import ApiError, ok
from ...services.offload import run_blocking
from .common import (
    _PLACEHOLDER_AUDIO,
    _VOICE_COLS,
    _get_voice_engine,
    _now,
    _sovits_degrade_clause,
    _voices,
)

if TYPE_CHECKING:
    from ...data.database import Database

router = APIRouter()
log = logging.getLogger("omnispace.api.manga.voice")



# ═══════════════════════════════════════════════════════════════════
#  音色（持久化到 voice_profiles 表，降级到内存预置）
# ═══════════════════════════════════════════════════════════════════

def _seed_voices(db: Database) -> None:
    """首次启动时将预置音色写入 voice_profiles 表（如果表为空）。"""
    try:
        existing = db.query_one("SELECT COUNT(*) AS cnt FROM voice_profiles")
        if existing and existing["cnt"] > 0:
            return
        now = _now()
        for v in _voices:
            db.insert("voice_profiles", {
                "id": v["id"], "name": v["name"],
                "character_id": v.get("character_id", ""),
                "is_preset": int(v.get("is_preset", True)),
                "file_path": "", "emotion": "默认",
                "created_at": now,
            })
        log.info("预置音色已写入 voice_profiles 表 (%d 条)", len(_voices))
    except Exception as exc:  # noqa: BLE001
        log.warning("预置音色写入失败: %s", exc)


def _row_to_voice(r: dict) -> dict:
    """voice_profiles 行 -> 对外音色 dict。"""
    return {
        "id": r["id"],
        "name": r.get("name", ""),
        "character_id": r.get("character_id", ""),
        "is_preset": bool(r.get("is_preset", 0)),
        "emotion": r.get("emotion", "默认"),
    }


@router.get("/manga/voices")
def voices_list() -> dict[str, Any]:
    """音色列表（规格 §4.4）。含预置情感标签。优先从数据库读取。"""
    db = get_db_safe()
    if db is not None:
        try:
            _seed_voices(db)
            rows = db.query(
                f"SELECT {_VOICE_COLS} FROM voice_profiles"
                " ORDER BY is_preset DESC, created_at ASC")
            items = [dict(_row_to_voice(r), emotions=list(VOICE_PRESET_EMOTIONS)) for r in rows]
            return ok({"items": items, "total": len(items)})
        except Exception as exc:  # noqa: BLE001
            log.warning("音色列表查询失败，降级内存存储: %s", exc)
    items = [dict(v, emotions=list(VOICE_PRESET_EMOTIONS)) for v in _voices]
    return ok({"items": items, "total": len(items)})


@router.post("/manga/voices/bind")
def voices_bind(req: VoiceBindRequest) -> dict[str, Any]:
    """绑定角色与音色（规格 §4.4）。持久化到 voice_profiles 表。"""
    db = get_db_safe()
    if db is not None:
        try:
            _seed_voices(db)
            row = db.query_one(
                f"SELECT {_VOICE_COLS} FROM voice_profiles WHERE id=?",
                (req.voice_id,))
            if row is None:
                raise ApiError(71001, "音色文件缺失", detail={"voice_id": req.voice_id})
            # 解除该角色之前绑定的其他音色
            db.update("voice_profiles", {"character_id": ""},
                      "character_id=?", (req.character_id,))
            # 绑定新音色
            db.update("voice_profiles", {"character_id": req.character_id},
                      "id=?", (req.voice_id,))
            return ok({"character_id": req.character_id, "voice_id": req.voice_id})
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("音色绑定写入失败，降级内存存储: %s", exc)

    # 内存降级
    voice = next((v for v in _voices if v["id"] == req.voice_id), None)
    if voice is None:
        raise ApiError(71001, "音色文件缺失", detail={"voice_id": req.voice_id})
    for v in _voices:
        if v["character_id"] == req.character_id:
            v["character_id"] = ""
    voice["character_id"] = req.character_id
    return ok({"character_id": req.character_id, "voice_id": req.voice_id})


@router.put("/manga/voices/{voice_id}/emotion")
def voices_emotion(voice_id: str, req: VoiceEmotionUpdate) -> dict[str, Any]:
    """更新音色情感（规格 §4.4）。持久化到 voice_profiles 表。"""
    db = get_db_safe()
    if db is not None:
        try:
            _seed_voices(db)
            row = db.query_one(
                f"SELECT {_VOICE_COLS} FROM voice_profiles WHERE id=?",
                (voice_id,))
            if row is None:
                raise ApiError(71001, "音色文件缺失", detail={"voice_id": voice_id})
            db.update("voice_profiles", {"emotion": req.emotion_label},
                      "id=?", (voice_id,))
            return ok({"voice_id": voice_id, "emotion": req.emotion_label})
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("音色情感更新失败，降级内存存储: %s", exc)

    # 内存降级
    voice = next((v for v in _voices if v["id"] == voice_id), None)
    if voice is None:
        raise ApiError(71001, "音色文件缺失", detail={"voice_id": voice_id})
    voice["emotion"] = req.emotion_label
    return ok({"voice_id": voice_id, "emotion": req.emotion_label})


@router.post("/manga/voices/preview")
async def voices_preview(req: VoicePreviewRequest) -> dict[str, Any]:
    """试听音色（规格 §4.4）。

    审计 BK-013 诚实降级：优先调用语音引擎真实合成；CosyVoice/ChatTTS
    未随包时引擎分级回退——SAPI5 系统语音真实发声（非 AI 音色）→
    静音占位 WAV（最后兜底），响应携带 degraded:true 与 degrade_reason
    中文字段，如实告知前端实际合成路径。GPT-SoVITS（F-08）权重随包但
    缺官方推理代码包时被门控跳过，degrade_reason 中一并如实说明。
    """
    db = get_db_safe()
    if db is not None:
        try:
            _seed_voices(db)
            row = await run_blocking(lambda: db.query_one(
                f"SELECT {_VOICE_COLS} FROM voice_profiles WHERE id=?",
                (req.voice_id,)))
            if row is None:
                raise ApiError(71001, "音色文件缺失", detail={"voice_id": req.voice_id})
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("音色查询失败，降级内存存储: %s", exc)
    else:
        voice = next((v for v in _voices if v["id"] == req.voice_id), None)
        if voice is None:
            raise ApiError(71001, "音色文件缺失", detail={"voice_id": req.voice_id})

    audio_b64 = _PLACEHOLDER_AUDIO
    degraded = True
    fallback_backend = ""
    try:
        engine = _get_voice_engine()
        # AI 推理 / SAPI5 COM 均为同步阻塞调用，放入线程池避免阻塞事件循环
        audio_path = await run_blocking(
            engine.synthesize, req.voice_id, req.text, req.emotion)
        raw = Path(audio_path).read_bytes()
        import base64
        audio_b64 = base64.b64encode(raw).decode("ascii")
        # is_ready=False（fallback/未加载）时 synthesize 走回退管线
        degraded = not engine.is_ready
        fallback_backend = engine.fallback_backend
    except Exception as exc:  # noqa: BLE001
        log.warning("试听合成失败，使用占位音频: %s", exc)

    data = {
        "voice_id": req.voice_id,
        "text": req.text,
        "emotion": req.emotion,
        "audio": audio_b64,
        "format": "wav",
    }
    if degraded:
        data["degraded"] = True
        sovits_clause = _sovits_degrade_clause()
        if fallback_backend == "sapi5":
            data["degrade_reason"] = (
                "CosyVoice/ChatTTS 语音模型未随包；" + sovits_clause +
                "已回退到 Windows 系统语音"
                "（SAPI5 真实发声，非 AI 音色）；安装语音模型后可获得 AI 音色")
        else:
            data["degrade_reason"] = (
                "CosyVoice/ChatTTS 语音模型未随包；" + sovits_clause +
                "且系统语音不可用，"
                "试听音频为静音占位 WAV（非真实音色）；安装语音模型后可真实合成")
    return ok(data)


# ═══════════════════════════════════════════════════════════════════
#  批 1.1 项目 CRUD（COMIC-001~004）
# ═══════════════════════════════════════════════════════════════════

# 漫剧模板预置分镜（COMIC-002：开场→发展→冲突→高潮→结尾）
_TEMPLATE_COMIC_DRAMA = (
    {"scene": "开场", "description": "故事开场：交代时间地点与主角登场"},
    {"scene": "发展", "description": "情节发展：主角行动，矛盾初现"},
    {"scene": "冲突", "description": "冲突爆发：矛盾激化，主角遭遇挑战"},
    {"scene": "高潮", "description": "故事高潮：决战/转折，情绪顶点"},
    {"scene": "结尾", "description": "结局收束：尘埃落定，主题升华"},
)
_VOICE_UPLOAD_DIR = DATA_DIR / "voices"
_DSL_MAX_BYTES = 10 * 1024 * 1024          # 10MB
_VOICE_MAX_BYTES = 20 * 1024 * 1024        # 20MB
_VOICE_UPLOAD_EXTS = (".wav", ".mp3", ".flac", ".m4a")


# ═══════════════════════════════════════════════════════════════════
#  批 1.5 音色上传/克隆（COMIC-047/048/053/054）
# ═══════════════════════════════════════════════════════════════════

@router.post("/manga/voices/upload")
async def voices_upload(name: str = Query("自定义音色"),
                        file: UploadFile = File(...)) -> dict[str, Any]:
    """上传自定义音色音频（wav/mp3/flac/m4a ≤20MB）。

    落盘 data/voices/ 并登记 voice_profiles 表（is_preset=0），
    可参与 bind/emotion/preview 全链路。
    """
    filename = (file.filename or "").lower()
    if not filename.endswith(_VOICE_UPLOAD_EXTS):
        raise ApiError(40010, "仅支持 wav/mp3/flac/m4a 音频文件",
                       detail={"filename": file.filename})
    # 有界读（2026-09-15 审计收尾）：先读后验改 read(limit+1)，与
    # comic_asset/comic.py 同款——全量 read 会让超大文件先整包进内存
    raw = await file.read(_VOICE_MAX_BYTES + 1)
    if not raw:
        raise ApiError("VOICE_FILE_MISSING", "音频文件为空")
    if len(raw) > _VOICE_MAX_BYTES:
        raise ApiError("OPERATION_LIMIT_EXCEEDED", "音频文件超过 20MB 上限",
                       detail={"max_bytes": _VOICE_MAX_BYTES,
                               "given": len(raw)})
    voice_id = "voice_custom_" + uuid.uuid4().hex[:12]
    _VOICE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(filename).suffix
    out_path = _VOICE_UPLOAD_DIR / f"{voice_id}{ext}"
    out_path.write_bytes(raw)
    rel_path = str(out_path.relative_to(DATA_DIR)).replace("\\", "/")
    db = get_db_safe()
    if db is not None:
        try:
            _seed_voices(db)
            await run_blocking(lambda: db.insert("voice_profiles", {
                "id": voice_id, "name": name[:100], "character_id": "",
                "is_preset": 0, "file_path": rel_path, "emotion": "默认",
                "created_at": _now()}))
        except Exception as exc:  # noqa: BLE001
            # 诚实失败（2026-09-17）：落库失败=音色不会出现在列表/无法绑定，
            # 返回 200 会让前端 toast 成功（审计四轮跨端遗留）——改为语义错误
            log.warning("音色上传落库失败: %s", exc)
            raise ApiError("VOICE_DB_WRITE",
                           "音色文件已保存但登记失败，音色暂不可用",
                           detail={"voice_id": voice_id},
                           suggestion="请重试上传；若持续失败请查看后端日志"
                           ) from None
    return ok({"voice_id": voice_id, "name": name, "file_path": rel_path,
               "size_bytes": len(raw)})


@router.post("/manga/voices/clone")
async def voices_clone(name: str = Query("克隆音色"),
                       file: UploadFile = File(...)) -> dict[str, Any]:
    """音色克隆（COMIC-053/054）。

    GPT-SoVITS 权重随包但缺官方推理代码包与 pypinyin（中文 G2P），
    如实返回 VOICE_CLONE_UNAVAILABLE（DEGRADED 转有依据错误），
    不产生伪克隆结果。
    """
    raise ApiError(
        "VOICE_CLONE_UNAVAILABLE",
        detail={"hint": "上传音频已接收，但 GPT-SoVITS 推理代码包未随包，"
                        "无法执行真实音色克隆；请使用预置音色或 voices/upload"},
        suggestion="安装 GPT-SoVITS 官方推理包与 pypinyin 后重试")
