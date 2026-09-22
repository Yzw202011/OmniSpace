"""云端图片透传单测（2026-09-22 用户报「云端 API 图片无法回答」）。

实弹：deepseek-flash 发图 → 引擎按 supports_images=False 丢图 → 模型
说「没收到图片」。修复 = remote_backend 打开图片支持 + 按模型视觉
能力二次裁决（视觉型号换 OpenAI image_url，非视觉剥除占位诚实丢弃）。
"""
from __future__ import annotations

import json
from unittest.mock import patch

from PIL import Image

from src.services.cloud_provider_service import is_vision_model


def test_vision_heuristic():
    assert is_vision_model("qwen3-vl-plus")
    assert is_vision_model("qwen-vl-max")
    assert is_vision_model("qvq-plus")
    assert is_vision_model("qwen-omni-turbo")
    assert is_vision_model("glm-4v")
    assert not is_vision_model("deepseek-flash")
    assert not is_vision_model("kimi-k2")
    assert not is_vision_model("qwen-plus")


def _make_image() -> Image.Image:
    return Image.new("RGB", (2, 2), color=(255, 0, 0))


class _CaptureResp:
    status_code = 200
    text = ""

    def __init__(self) -> None:
        self.captured: dict = {}

    def iter_lines(self):
        line1 = json.dumps(
            {"choices": [{"delta": {"content": "好"}, "finish_reason": None}]},
            ensure_ascii=False)
        yield f"data: {line1}".encode()
        yield b"data: [DONE]"

    def close(self) -> None:
        pass


def _ready_backend(model_id: str):
    from src.services.inference.backends.remote_backend import (
        RemoteDialogBackend,
    )
    b = RemoteDialogBackend()
    b._ready = True
    b._base_url = "http://mock-cloud"
    b._api_key = ""
    b._provider_name = "测试"
    b.model_id = model_id
    return b


def _engine_style_messages() -> list[dict]:
    """引擎 build_context 的多模态形态：图片占位 + 文本。"""
    return [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": [
            {"type": "image"},
            {"type": "text", "text": "解译图片"},
        ]},
    ]


def test_vision_model_payload_embeds_image_url():
    resp = _CaptureResp()
    with patch("requests.post", return_value=resp) as mock_post:
        b = _ready_backend("qwen3-vl-plus")
        list(b.chat_stream(_engine_style_messages(), images=[_make_image()]))
    sent = mock_post.call_args.kwargs["json"]
    user_parts = sent["messages"][-1]["content"]
    kinds = [p["type"] for p in user_parts]
    assert kinds == ["image_url", "text"], kinds
    assert user_parts[0]["image_url"]["url"].startswith(
        "data:image/png;base64,")


def test_non_vision_model_strips_image_parts():
    """非视觉型号（deepseek 系）收图会 400 → 剥除占位诚实丢弃。"""
    resp = _CaptureResp()
    with patch("requests.post", return_value=resp) as mock_post:
        b = _ready_backend("deepseek-flash")
        list(b.chat_stream(_engine_style_messages(), images=[_make_image()]))
    sent = mock_post.call_args.kwargs["json"]
    user_parts = sent["messages"][-1]["content"]
    assert [p["type"] for p in user_parts] == ["text"], "图片占位未剥除"
    assert all("image_url" not in json.dumps(p) for p in user_parts)
