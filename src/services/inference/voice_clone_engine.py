# 本项目仅供学习使用，商业授权请+Q 3559331368
"""F5-TTS 音色克隆引擎（大四件④，2026-09-17 用户拍板=补真实现）。

选型依据（调研 2026-09-17）：
  - F5-TTS 推理仅需 2-3GB VRAM（16GB 卡留足余量给其他模型）
  - pip install f5-tts 一步装、英文+中文原生
  - 零样本克隆：5-10 秒参考音频 + 参考文本即可

懒加载单例 + 可用性探测（不可用时 voices_clone 端点诚实降级回
SAPI5 通道，不产生伪克隆结果）。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("omnispace.inference.voice_clone")

_engine: Any = None
_engine_lock = threading.Lock()
_available: bool | None = None


def f5_tts_available() -> bool | None:
    """探测 F5-TTS 可用性（None=未探测；True/False=已探测结果）。"""
    global _available
    if _available is None:
        try:
            import f5_tts  # type: ignore[import-untyped]  # noqa: F401
            _available = True
        except ImportError:
            _available = False
            log.info("F5-TTS 未安装（pip install f5-tts 可启用音色克隆）")
    return _available


def _get_engine() -> Any:
    """懒加载 F5-TTS 推理器（首次调用下载预训练权重 ~1.4GB）。"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from f5_tts.infer.utils_infer import (  # type: ignore[import-untyped]
                    infer_process,
                    load_model,
                    preprocess_ref_audio_text,
                )
                _engine = {
                    "infer_process": infer_process,
                    "load_model": load_model,
                    "preprocess_ref_audio_text": preprocess_ref_audio_text,
                }
                log.info("F5-TTS 推理器已加载")
    return _engine


def clone_voice_to_speech(
    text: str,
    ref_audio_path: str,
    ref_text: str = "",
    output_path: str = "",
    speed: float = 1.0,
) -> dict[str, Any]:
    """用参考音频的音色把文本合成语音（零样本克隆）。

    Args:
        text: 要合成的文本（中文/英文）
        ref_audio_path: 参考音频文件路径（5-10 秒效果最佳）
        ref_text: 参考音频的文本转写（提供可提高质量；空则自动 ASR）
        output_path: 输出 wav 路径（空则自动生成临时文件）
        speed: 语速（1.0=正常）

    Returns:
        {"output_path": str, "duration_s": float, "sample_rate": int}

    Raises:
        RuntimeError: F5-TTS 不可用或推理失败
    """
    if not f5_tts_available():
        raise RuntimeError(
            "F5-TTS 未安装（pip install f5-tts 后重启后端可启用音色克隆）")

    eng = _get_engine()
    import soundfile as sf  # type: ignore[import-untyped]
    import torch

    # 预处理参考音频（截取有效段+去静音+对齐文本）
    ref_audio, ref_text_processed = eng["preprocess_ref_audio_text"](
        ref_audio_path, ref_text)

    # 推理（F5-TTS 默认模型 ~1.4GB，首次自动下载到 ~/.cache/f5-tts）
    device = "cuda" if torch.cuda.is_available() else "cpu"
    audio, sr, _ = eng["infer_process"](
        ref_audio, ref_text_processed, text,
        speed=speed, device=device,
    )

    # 落盘
    if not output_path:
        import tempfile
        import uuid
        output_path = str(
            Path(tempfile.gettempdir()) / f"voice_clone_{uuid.uuid4().hex[:8]}.wav")
    sf.write(output_path, audio, sr)

    duration = len(audio) / sr
    log.info("音色克隆完成: %.1fs 音频 → %s", duration, output_path)
    return {
        "output_path": output_path,
        "duration_s": round(duration, 2),
        "sample_rate": sr,
    }
