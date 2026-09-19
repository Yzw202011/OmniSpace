"""批3（2026-09-19）单测：写作台三参数（P4）+ 降AI味档位（P13）。

锁定：_parse_plan_meta 越界钳制与缺省、大纲 prompt 篇幅规划注入、
正文 prompt 文风档位注入与目标字数。
"""
# 本项目仅供学习使用，商业授权请+Q 3553191368
from __future__ import annotations

from src.api.novel import _parse_plan_meta
from src.services.novel_service import (
    build_chapter_prompt,
    build_outline_prompt,
)


def test_plan_meta_defaults() -> None:
    m = _parse_plan_meta({})
    assert m == {"plan_chapters": 20, "volume_count": 0,
                 "words_per_chapter": 2000, "style_preset": "standard"}


def test_plan_meta_clamps_out_of_range() -> None:
    m = _parse_plan_meta({"plan_chapters": 9999, "volume_count": -3,
                          "words_per_chapter": 10, "style_preset": "hack"})
    assert m["plan_chapters"] == 200
    assert m["volume_count"] == 0
    assert m["words_per_chapter"] == 800
    assert m["style_preset"] == "standard"


def test_plan_meta_passthrough_valid() -> None:
    m = _parse_plan_meta({"plan_chapters": 30, "volume_count": 3,
                          "words_per_chapter": 2500, "style_preset": "heavy"})
    assert m == {"plan_chapters": 30, "volume_count": 3,
                 "words_per_chapter": 2500, "style_preset": "heavy"}


def test_outline_prompt_injects_plan() -> None:
    p = build_outline_prompt(description="d", genre="g", style_notes="",
                             plan_chapters=30, volume_count=2,
                             words_per_chapter=2500)
    assert "约 30 章" in p and "分 2 卷" in p and "约 2500 字" in p
    # 有计划时不再出现历史缺省文案
    assert "总章数不超过 15" not in p


def test_outline_prompt_legacy_default_without_plan() -> None:
    p = build_outline_prompt(description="d", genre="g", style_notes="")
    assert "总章数不超过 15" in p  # 未传参数=历史行为完全兼容


def test_chapter_prompt_style_presets() -> None:
    base = dict(project_name="书", genre="", style_notes="", chapter_title="一",
                outline_text="纲", prev_summaries=[], prev_tail="",
                characters=[], worldview="")
    std = build_chapter_prompt(**base, style_preset="standard")
    light = build_chapter_prompt(**base, style_preset="light")
    heavy = build_chapter_prompt(**base, style_preset="heavy")
    assert "拟真" not in std
    assert "拟真要求·轻" in light and "句长" in light
    assert "拟真要求·重" in heavy and "低频" not in heavy  # heavy 措辞为「通用高频词」
    assert "仿佛" in heavy  # 禁模板比喻条目在
    assert "情感" in heavy


def test_chapter_prompt_target_words() -> None:
    p = build_chapter_prompt(project_name="书", genre="", style_notes="",
                             chapter_title="一", outline_text="纲",
                             prev_summaries=[], prev_tail="", characters=[],
                             worldview="", target_words=3000)
    assert "约 3000 字" in p
