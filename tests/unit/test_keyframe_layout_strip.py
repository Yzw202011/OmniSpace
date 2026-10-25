"""④批4（2026-10-25）：关键帧链版式指令剥除回归。

病灶（④ 方案书 M29 关联）：含「四视图/四宫格/分镜网格」的提示词
（用户粘贴四视图资产描述 / 描述词生成器带入版式句）进关键帧/分镜
图链 → 静态图画出多视图拼贴。真源 =
services/inference/h3_prompt_normalizer.strip_layout_instruction（句内
短语级、两级触发判据），关键帧链三入口接线（本测试同时钉接线存在）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

from pathlib import Path

from src.services.inference.h3_prompt_normalizer import (
    strip_layout_instruction,
)

_ROOT = Path(__file__).resolve().parents[2]
_KEYFRAME = (_ROOT / "src" / "api" / "manga" / "keyframe.py").read_text(
    encoding="utf-8")


def test_pure_layout_input_stripped_to_empty() -> None:
    """纯版式指令 → 空串（调用方按降级铁律回退原文）。"""
    assert strip_layout_instruction("四宫格。") == ""
    assert strip_layout_instruction("四视图设定图，横向等宽排成一行") == ""


def test_layout_clauses_dropped_content_kept() -> None:
    """混排句：只丢版式短语，正文与句读结构保留。"""
    out = strip_layout_instruction(
        "少女站在路口，四视图设定图，横向等宽排成一行。风吹起长发。")
    assert out == "少女站在路口。风吹起长发。"
    out2 = strip_layout_instruction(
        "A段风格：韩国网漫风。四视图横排参考，白底。")
    assert out2 == "A段风格：韩国网漫风。白底。"


def test_content_mention_not_false_positive() -> None:
    """两级判据：歧义词（四视图）无版式副信号=内容短语不误伤。"""
    text = "他望着四视图作品出神，若有所思。"
    assert strip_layout_instruction(text) == text


def test_no_trigger_zero_cost_passthrough() -> None:
    """无触发词原样返回（同一对象语义由实现保证，此处锁值等值）。"""
    text = "雨夜便利店，霓虹灯牌，少女撑伞回眸。"
    assert strip_layout_instruction(text) == text


def test_keyframe_chain_wired() -> None:
    """源码哨兵：三入口（逐镜 sp / 单帧 ABC / 直文本兜底）全部接线。"""
    assert "_strip_layout_instruction(sp) or sp" in _KEYFRAME, (
        "逐镜路径版式剥除接线丢失")
    assert "_strip_layout_instruction(gen_prompt) or gen_prompt" in (
        _KEYFRAME), "单帧 ABC 路径版式剥除接线丢失"
    assert "_strip_layout_instruction(prompt) or prompt" in _KEYFRAME, (
        "直文本兜底路径版式剥除接线丢失")
