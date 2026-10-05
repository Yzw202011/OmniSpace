"""云端模型类别严格过滤单测（2026-09-21 用户令：工位只留对应类别）。

背景实弹：千问-文本连接「拉取模型列表」拉回百炼全量目录 250+ 条，
混入 MiniMax/Kimi 代销对话模型 + 音频/画图/TTS/向量化模型，用户两选
qwen-audio-asr 报 UNSUPPORTED_FORMAT。本测试锁过滤器的协议分支。
"""
from __future__ import annotations

from src.services.cloud_provider_service import filter_models_for_protocol


def test_text_protocol_keeps_chat_models():
    kept, removed = filter_models_for_protocol("openai_text", [
        "qwen-plus", "qwen-max", "qwen3-vl-plus", "qwen-vl-max",
        "qvq-plus", "qwen3-coder-plus", "qwen3-omni-flash",
        "kimi-k2.6", "MiniMax-M2.1", "deepseek-v3.2", "glm-5",
    ])
    assert kept == [
        "qwen-plus", "qwen-max", "qwen3-vl-plus", "qwen-vl-max",
        "qvq-plus", "qwen3-coder-plus", "qwen3-omni-flash",
        "kimi-k2.6", "MiniMax-M2.1", "deepseek-v3.2", "glm-5",
    ]
    assert removed == []


def test_text_protocol_removes_audio_tts_image_video_vector():
    kept, removed = filter_models_for_protocol("openai_text", [
        "qwen-audio-3.0-asr-flash",        # 2026-09-21 用户误选实弹
        "qwen3-tts-flash", "MiniMax/speech-02-hd",
        "qwen-image-2.0", "wan2.7-image", "z-image-turbo",
        "wanx2.1-t2i-turbo",
        "qwen3.7-text-embedding", "qwen3.7-text-rerank",
        "qwen-vl-ocr", "qwen-mt-plus",
        "qwen3-omni-flash-realtime",       # realtime 通道，非对话补全
        "qwen3-livetranslate-flash",
        "qwen-plus",                        # 对照：正常对话模型保留
    ])
    assert kept == ["qwen-plus"]
    assert "qwen-audio-3.0-asr-flash" in removed
    assert "qwen3-tts-flash" in removed
    assert "qwen-image-2.0" in removed
    assert "qwen3.7-text-embedding" in removed
    assert "qwen-vl-ocr" in removed
    assert "qwen-mt-plus" in removed
    assert "qwen3-omni-flash-realtime" in removed


def test_image_protocol_keeps_image_models():
    """图片协议不排 image/wanx（出图模型名本身含这些词），只排语音/向量。"""
    kept, removed = filter_models_for_protocol("task_image", [
        "wanx2.1-t2i-turbo", "qwen-image-edit", "qwen-audio-tts",
        "qwen3.7-text-embedding",
    ])
    assert kept == ["wanx2.1-t2i-turbo", "qwen-image-edit"]
    assert removed == ["qwen-audio-tts", "qwen3.7-text-embedding"]


def test_video_protocol_keeps_video_models():
    kept, removed = filter_models_for_protocol("task_video", [
        "wan2.6-t2v", "kimi-audio", "text-embedding-v4",
    ])
    assert kept == ["wan2.6-t2v"]
    assert removed == ["kimi-audio", "text-embedding-v4"]


def test_empty_and_blank_entries_dropped():
    kept, removed = filter_models_for_protocol("openai_text",
                                               ["", "  ", "qwen-plus"])
    assert kept == ["qwen-plus"]
    assert removed == []


def test_result_preserves_order():
    models = ["qwen-turbo", "qwen-tts-x", "qwen-max"]
    kept, _ = filter_models_for_protocol("openai_text", models)
    assert kept == ["qwen-turbo", "qwen-max"]


def test_video_protocol_whitelist_only_video_models() -> None:
    """2026-10-06 用户实报：视频协议拉模型列表全量混出（文本/图片模型
    塞进视频连接）——视频协议白名单只留视频模型，其余进 removed。"""
    kept, removed = filter_models_for_protocol("task_video", [
        "composer-2.5", "grok-4.3-latest", "grok-4.5",
        "grok-imagine", "grok-imagine-image-2.0",
        "grok-imagine-video", "grok-imagine-video-1.5",
        "wanx-i2v-turbo",  # i2v 视频模型
        "deepseek-chat",
    ])
    assert kept == ["grok-imagine-video", "grok-imagine-video-1.5",
                    "wanx-i2v-turbo"]
    assert set(removed) >= {"composer-2.5", "grok-4.3-latest",
                            "grok-imagine-image-2.0", "deepseek-chat"}


def test_sora_video_protocol_same_whitelist() -> None:
    """sora_video 协议同规则（MagicAI Grok 视频实测场景）。"""
    kept, _ = filter_models_for_protocol("sora_video", [
        "grok-4.5", "grok-imagine-video", "grok-imagine-video-1.5"])
    assert kept == ["grok-imagine-video", "grok-imagine-video-1.5"]
