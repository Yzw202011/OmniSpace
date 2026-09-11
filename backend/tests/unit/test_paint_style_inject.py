"""风格包注入统一回归（2026-09-06 架构升级计划 B-阶段一，《AI计划》§3.1）。

绘画链此前只用风格包选模型（resolve_route）不注入提示词，与漫剧
关键帧链不对齐。本文件锁定 _inject_style_pack：块拼接格式、negative
用户自带优先/风格包兜底、无块透传、真实嗅探中文风格词不抛异常。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

from backend.api.draw import _inject_style_pack
from backend.services.inference import gen_router


class _FakePack:
    """explain_style 返回的最小 StylePack 替身（只暴露消费字段）。"""

    def __init__(self, style_block: str = "", quality_block: str = "",
                 negative_hint: str = "", sid: str = "test",
                 label: str = "测试包") -> None:
        self.style_block = style_block
        self.quality_block = quality_block
        self.negative_hint = negative_hint
        self.sid = sid
        self.label = label


def _patch_explain(monkeypatch, pack) -> None:
    monkeypatch.setattr(gen_router, "explain_style",
                        lambda text: (pack, "测试凭据"))


def test_inject_appends_blocks(monkeypatch) -> None:
    _patch_explain(monkeypatch, _FakePack(
        style_block="watercolor painting style",
        quality_block="soft edges, high quality"))
    params: dict = {"prompt": "a girl by the lake"}
    new_prompt, note = _inject_style_pack("a girl by the lake", params)
    assert new_prompt == ("a girl by the lake, watercolor painting style, "
                          "soft edges, high quality")
    assert "test" in note and "测试凭据" in note


def test_negative_fallback_from_pack(monkeypatch) -> None:
    _patch_explain(monkeypatch, _FakePack(
        style_block="watercolor style", negative_hint="blurry, lowres"))
    params: dict = {"prompt": "a cat"}
    _inject_style_pack("a cat", params)
    assert params["negative"] == "blurry, lowres"


def test_negative_user_takes_priority(monkeypatch) -> None:
    _patch_explain(monkeypatch, _FakePack(
        style_block="watercolor style", negative_hint="blurry, lowres"))
    params: dict = {"prompt": "a cat", "negative": "custom negative"}
    _inject_style_pack("a cat", params)
    assert params["negative"] == "custom negative"


def test_no_blocks_passthrough(monkeypatch) -> None:
    _patch_explain(monkeypatch, _FakePack())
    params: dict = {"prompt": "a cat"}
    new_prompt, note = _inject_style_pack("a cat", params)
    assert new_prompt == "a cat"
    assert note == ""


def test_real_sniff_chinese_style_word() -> None:
    # 真实嗅探链：中文风格词不抛异常、返回 (str, str) 契约；命中包时
    # 注入发生（块非空），未命中走默认包兜底——两者都合法。
    params: dict = {"prompt": "水彩风格的少女在湖边"}
    new_prompt, note = _inject_style_pack(params["prompt"], params)
    assert isinstance(new_prompt, str) and isinstance(note, str)
    assert new_prompt.startswith("水彩风格的少女在湖边".rstrip(" ,.。")[:6])
