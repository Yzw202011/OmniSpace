"""P0-3 多角色锚协议纯逻辑测试（2026-08-28）。

B9（2026-09-13）：AST 沙箱改真 import。桩注入改走模块级 monkeypatch：
detect_style 桩 + _SHOT_FRAMING 受控景别表经 fixture 注入并在测试后
自动还原（原沙箱靠 ns 作用域隔离，真模块必须显式打桩）。

覆盖（P0-3 残留补口：外层锚曾写死「始终只有这一个角色」，与调用
方多角色锚「共 N 位」同句矛盾）：
  - 单角色行：保留 v26「同一角色 + 唯一性禁令」措辞
  - 多角色行：外层为中性「角色锚」措辞，禁止出现单角色唯一性禁令
  - 无角色绑定行：不注入 char 锚
  - 默认参数（char_count=0）与单角色行为一致（向后兼容）
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import pytest

import src.api.manga.keyframe as _kf_mod

_FUNCS = {"_shot_image_prompt_from_abc", "_strip_prop_colors",
          "_strip_char_appearance", "_strip_hand_flush"}
_CONSTS = {"_PROP_COLOR", "_CHAR_APPEAR_PAT", "_HAND_FLUSH_PAT"}


@pytest.fixture()
def shot_prompt_ns(monkeypatch: pytest.MonkeyPatch) -> dict:
    """真 import 目标函数 + 注入受控风格/景别桩（测试后自动还原）。"""
    ns: dict = {n: getattr(_kf_mod, n) for n in _FUNCS}
    for c in _CONSTS:
        ns[c] = getattr(_kf_mod, c)

    class _Pack:
        sid = "default"
        post = "realistic"
        style_block = ""
        quality_block = ""

    monkeypatch.setattr(_kf_mod, "detect_style", lambda text: _Pack())
    monkeypatch.setattr(_kf_mod, "_SHOT_FRAMING",
                        {"1x2": ["Medium shot", "Medium shot"]})
    ns["detect_style"] = _kf_mod.detect_style
    ns["_SHOT_FRAMING"] = _kf_mod._SHOT_FRAMING
    return ns


def _build(ns: dict, char_prompt: str, char_count: int = 0) -> str:
    f = ns["_shot_image_prompt_from_abc"]
    grid = {"layout": "1x2", "shots": [
        {"text": "少女站在灯塔下眺望海面"},
        {"text": "海面泛起微光，灯塔亮起"}]}
    return f("A. 全局风格：日系网漫。\nC. 1. 少女站在灯塔下眺望海面。"
             "2. 海面泛起微光。", grid, 0, char_prompt, "", "", "",
             char_count=char_count)


def test_single_char_keeps_uniqueness_anchor(shot_prompt_ns: dict) -> None:
    ns = shot_prompt_ns
    out = _build(ns, "小满，外貌、服装与角色设定图严格一致", char_count=1)
    assert "同一角色（各镜严格一致，画面中始终只有这一个角色，" \
           "除该角色外不得出现任何人物）：小满" in out
    assert "角色锚（各镜严格一致）：" not in out


def test_multi_char_anchor_has_no_contradiction(shot_prompt_ns: dict) -> None:
    ns = shot_prompt_ns
    cp = ("画面中的角色共2位：小满、阿澈，各自外貌、服装与角色设定图"
          "严格一致；除这些角色外不得出现任何其他人物")
    out = _build(ns, cp, char_count=2)
    # 中性措辞 + 逐名列举完整保留
    assert f"角色锚（各镜严格一致）：{cp}" in out
    # 单角色唯一性禁令不得与「共 N 位」同句出现（P0-3 残留矛盾）
    assert "始终只有这一个角色" not in out
    assert "除这些角色外不得出现任何其他人物" in out


def test_no_char_binding_no_anchor(shot_prompt_ns: dict) -> None:
    ns = shot_prompt_ns
    out = _build(ns, "")
    assert "同一角色" not in out
    assert "角色锚" not in out


def test_default_char_count_backcompat(shot_prompt_ns: dict) -> None:
    """不传 char_count（旧调用方）保持单角色行为。"""
    ns = shot_prompt_ns
    f = ns["_shot_image_prompt_from_abc"]
    grid = {"layout": "1x2", "shots": [{"text": "少女站在灯塔下"}]}
    out = f("A. 全局风格：日系网漫。", grid, 0,
            "小满，外貌、服装与角色设定图严格一致")
    assert "始终只有这一个角色" in out
