"""音色克隆引擎测试（大四件④，2026-09-17）。

mock F5-TTS 推理链路——真装实弹需 pip install f5-tts + GPU。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.inference import voice_clone_engine as vce  # noqa: E402


def test_f5_unavailable_graceful() -> None:
    """F5-TTS 未安装时 f5_tts_available()=False，clone 抛 RuntimeError。"""
    with patch.dict(sys.modules, {"f5_tts": None}):
        vce._available = None  # 复位缓存
        assert vce.f5_tts_available() is False
        with pytest.raises(RuntimeError, match="pip install f5-tts"):
            vce.clone_voice_to_speech("测试", "fake.wav")
    vce._available = None  # 恢复


def test_clone_voice_mock(tmp_path: Path) -> None:
    """mock 推理链路——验证参数传递与输出落盘逻辑。"""
    import numpy as np

    vce._available = True
    fake_audio = np.zeros(24000, dtype=np.float32)  # 1 秒静音 @24kHz

    class _FakeSF:
        @staticmethod
        def write(path, audio, sr):
            Path(path).write_bytes(b"RIFF")  # 占位

    class _FakeEngine:
        @staticmethod
        def preprocess_ref_audio_text(ref, text):
            return ref, text or "参考文本"

        @staticmethod
        def infer_process(ref, ref_text, text, **kw):
            assert "测试文本" in text
            return fake_audio, 24000, None

    with patch.object(vce, "_get_engine",
                      return_value={"preprocess_ref_audio_text":
                                    _FakeEngine.preprocess_ref_audio_text,
                                    "infer_process":
                                    _FakeEngine.infer_process}), \
         patch.dict(sys.modules, {"soundfile": _FakeSF}):
        out = vce.clone_voice_to_speech(
            "测试文本", str(tmp_path / "ref.wav"),
            output_path=str(tmp_path / "out.wav"))
        assert out["duration_s"] == 1.0
        assert out["sample_rate"] == 24000
        assert Path(out["output_path"]).exists()

    vce._available = None  # 恢复
