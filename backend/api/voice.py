"""语音 API 路由（《显存阶梯参考》语音模型全维度对接）。

端点清单：
- GET  /voice/status       语音引擎状态（TTS 后端链 / ASR 就绪 / SoVITS 门控 / 发现的语音模型）
- GET  /voice/models       models/ 目录动态发现的语音模型清单（导入即可见）
- POST /voice/transcribe   语音转写（Whisper 真实推理，multipart 上传音频）
- POST /voice/synthesize   语音合成（TTS 自动装载链：cosyvoice → chattts → bark
                           → SAPI5/静音回退，诚实标注 degraded）

对接说明（导入 models/ 即可用）：
- ASR：models/whisper*（transformers 原生，如 openai/whisper-large-v3）
- TTS：models/bark*（transformers 原生）；cosyvoice/chattts 需各自代码包，
  未安装时如实门控并回退系统语音，绝不伪造 AI 合成。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile

from ..config import DATA_DIR
from ..data.models import VoicePreviewRequest
from ..middleware.error_handler import ApiError, ok

router = APIRouter()
log = logging.getLogger("omnispace.api.voice")

# 音频上传上限 100MB（长音频转写场景）
_MAX_AUDIO_BYTES = 100 * 1024 * 1024
# 允许的音频扩展名
_AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm", ".mp4", ".aac"}

_engine_instance = None
_engine_lock = threading.Lock()


def _get_engine():
    """VoiceEngine 进程级单例（与 manga.py 共享同一实例类）。"""
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                from ..services.inference.voice_engine import VoiceEngine
                _engine_instance = VoiceEngine()
    return _engine_instance


@router.get("/voice/status")
def voice_status():
    """语音引擎状态：TTS 后端链、ASR 就绪、SoVITS 门控、发现的语音模型。"""
    return ok(_get_engine().get_status())


@router.get("/voice/models")
def voice_models():
    """models/ 目录动态发现的语音模型（ready=True 表示当前环境可直接推理）。"""
    from ..services.inference.voice_engine import discover_voice_models
    found = discover_voice_models()
    items = [{"id": name, **info} for name, info in sorted(found.items())]
    return ok({"items": items, "total": len(items)})


@router.post("/voice/transcribe")
async def voice_transcribe(
    file: UploadFile = File(...),
    language: str = Form(""),
    model_id: str = Form(""),
):
    """语音转写（Whisper 真实推理）。

    - WAV 直接解码；mp3/m4a/flac/ogg 等经 FFmpeg 转 16k 单声道 WAV；
    - 未导入 whisper 模型时返回 71003 诚实门控错误（不伪造转写文本）。
    """
    if not file or not file.filename:
        raise ApiError(40008, "未提供上传文件")
    ext = Path(file.filename).suffix.lower()
    if ext not in _AUDIO_EXTS:
        raise ApiError(71002, f"不支持的音频格式: {ext or '(无扩展名)'}",
                       suggestion="支持: " + "/".join(sorted(_AUDIO_EXTS)))
    content = await file.read(_MAX_AUDIO_BYTES + 1)
    if not content:
        raise ApiError(40008, "上传文件内容为空")
    if len(content) > _MAX_AUDIO_BYTES:
        raise ApiError(40009, "音频文件超过 100MB 上限",
                       detail={"size_bytes": len(content)})

    audio_dir = DATA_DIR / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = audio_dir / f"upload_{uuid.uuid4().hex}{ext}"
    try:
        tmp_path.write_bytes(content)
    except OSError as exc:
        raise ApiError(40006, "音频落盘失败", detail={"error": str(exc)}) from exc

    engine = _get_engine()
    try:
        # Whisper 推理为同步阻塞调用，放入线程池避免阻塞事件循环
        result = await asyncio.to_thread(
            engine.transcribe, str(tmp_path),
            language or None, model_id or None)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    return ok(result)


@router.post("/voice/synthesize")
async def voice_synthesize(req: VoicePreviewRequest):
    """语音合成（复用 VoicePreviewRequest 契约：voice_id/text/emotion）。

    TTS 自动装载链：cosyvoice → chattts → bark（transformers 原生，
    导入 models/ 即可用）→ SAPI5 系统语音 → 静音占位。非 AI 后端时
    响应携带 degraded:true 与实际后端名（诚实降级）。
    """
    engine = _get_engine()
    try:
        # AI 推理 / SAPI5 COM 均为同步阻塞调用，放入线程池
        audio_path = await asyncio.to_thread(
            engine.synthesize, req.voice_id, req.text, req.emotion)
        raw = Path(audio_path).read_bytes()
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ApiError(71005, "语音合成失败", detail={"error": str(exc)}) from exc

    data = {
        "voice_id": req.voice_id,
        "text": req.text,
        "emotion": req.emotion,
        "audio": base64.b64encode(raw).decode("ascii"),
        "format": "wav",
    }
    if not engine.is_ready:
        data["degraded"] = True
        data["fallback_backend"] = engine.fallback_backend
    return ok(data)
