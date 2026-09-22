"""长文两步走单测（2026-09-22 用户拍板：对话页长文先列提纲再写）。

复读高发于无计划的注水段——先出提纲、正文按提纲写（计划型记忆）。
判定器真行为断言（dialog.py 可导入）；两路接线用源码断言防静默删改。
"""
from __future__ import annotations

from pathlib import Path

from src.services.inference.thinking_policy import (
    is_longform_request as _is_longform_request,
)

DIALOG_PY = (Path(__file__).resolve().parents[2] / "src" / "api" / "dialog.py")
API = DIALOG_PY.read_text(encoding="utf-8")


def test_longform_hits():
    assert _is_longform_request("帮我写一篇5000字的恋爱小说")
    assert _is_longform_request("写一篇3000字的故事")
    assert _is_longform_request("来一篇长篇科幻")


def test_short_and_non_creation_pass_through():
    assert not _is_longform_request("你好")
    assert not _is_longform_request("写一首诗")
    assert not _is_longform_request("总结一下图片内容")
    assert not _is_longform_request("什么是量子纠缠")


def test_outline_asking_skips_two_phase():
    """用户本身在要提纲/大纲 → 不介入两步走。"""
    assert not _is_longform_request("帮我写一个小说大纲")
    assert not _is_longform_request("给这篇长文列个提纲")


def test_wiring_in_both_paths():
    """WS/REST 两路都要有：提纲生成调用 + sys_prompt 注入 + 开头输出。"""
    assert "longform_outline, engine, message" in API, "REST 路径未接提纲"
    assert "longform_outline, engine, content" in API, "WS 路径未接提纲"
    assert API.count("outline_system_block(outline)") == 2, "sys_prompt 注入未覆盖两路"
    assert API.count('"【创作提纲】\\n" + outline') == 2, (
        "提纲开头输出未覆盖两路 _produce")
    assert '"正在列创作提纲（约 10~30 秒）' in API, "WS 提纲状态帧被删"
