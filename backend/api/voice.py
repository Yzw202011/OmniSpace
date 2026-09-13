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

import logging
import threading
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from ..middleware.error_handler import ok

if TYPE_CHECKING:
    from ..services.inference.voice_engine import VoiceEngine

router = APIRouter()
log = logging.getLogger("omnispace.api.voice")

# 音频上传上限 100MB（长音频转写场景）
_MAX_AUDIO_BYTES = 100 * 1024 * 1024
# 允许的音频扩展名
_AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm", ".mp4", ".aac"}

_engine_instance = None
_engine_lock = threading.Lock()


def _get_engine() -> VoiceEngine:
    """VoiceEngine 进程级单例（与 manga.py 共享同一实例类）。"""
    global _engine_instance
    if _engine_instance is None:
        with _engine_lock:
            if _engine_instance is None:
                from ..services.inference.voice_engine import VoiceEngine
                _engine_instance = VoiceEngine()
    return _engine_instance


@router.get("/voice/status")
def voice_status() -> dict[str, Any]:
    """语音引擎状态：TTS 后端链、ASR 就绪、SoVITS 门控、发现的语音模型。"""
    return ok(_get_engine().get_status())


# 本项目仅供学习使用，商业授权请+Q 3559331368
