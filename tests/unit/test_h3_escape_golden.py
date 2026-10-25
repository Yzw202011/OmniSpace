"""④-批1（2026-10-24 建基 / 2026-10-25 补强）：H3 转义层 golden 基线。

方案书 docs/方案-②①④-结构性三项-2026-10-24.md ④ 转义层统一真源。
首版只断言 isinstance(str)——锁不住任何行为变化（假金）。本版把
确定性函数（detect_input_format / abc_to_official / abc_text_to_
official / abc_builder_from_desc 桩注入形态）的**实际输出**钉进
基线：批 2~3 改 import 消费 skills/h3_convert 真源后必须零 diff。

基线安全性：基线记录「改前现状」。批 2 若有**有意**的口径修正
（方案书批准的漂移收敛），须在新用例段另立基线并注明——原 10 组
的零 diff 红线不豁免。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import pytest

from src.services.inference.h3_prompt_normalizer import (
    abc_builder_from_desc,
    abc_text_to_official,
    abc_to_official,
    detect_input_format,
)

# 10 组典型 prompt（覆盖：纯中文/英文/A·B·C 三段/含标记/含台词/
# 空串/超长/特殊字符/嵌套引号/字面 \n 变体）
GOLDEN_PROMPTS = [
    "一个少女站在樱花树下",
    "A beautiful anime girl standing under cherry blossoms",
    "A. 全局风格：韩国网漫风\nB. 高密度世界观构建：少女设定\nC. 分镜时间轴：[0.0s-5.0s] 画面：少女特写",
    "包含<dlg>标记的提示词",
    "旁白：这是测试文案",
    "",
    "超长提示词" * 100,
    "特殊字符：!@#$%^&*()_+-=[]{}|;':\",./<>?",
    '嵌套引号："外层\'内层\'外层"',
    "A. 风格\\nB. 人物：夏沐沐\\nC. [0.0s-3.0s] 画面：测试",
]

# 批1 实录：格式探测基线（序号与 GOLDEN_PROMPTS 一一对应）
_DETECT_BASELINE = [
    "freeform", "freeform", "abc", "freeform", "freeform",
    "freeform", "freeform", "freeform", "freeform", "abc",
]


def _stub_parse(desc: str) -> dict:
    """注入桩：模拟 API 层 _parse_abc_shots 输出形状（单实体镜）。"""
    return {"shots": [{"text": "少女特写", "camera": "", "sfx": "",
                       "start": 1.0, "end": 3.0}], "layout": None}


def _tr(s: str) -> str:
    """注入桩：确定性翻译（截断大写，锁拼装序不锁译文）。"""
    return s.upper()[:20] if s else s


class TestDetectFormatGolden:
    @pytest.mark.parametrize("prompt,want", zip(GOLDEN_PROMPTS,
                                                _DETECT_BASELINE,
                                                strict=True))
    def test_detect(self, prompt: str, want: str) -> None:
        assert detect_input_format(prompt) == want


class TestAssembleGolden:
    """三段式确定性拼装基线（批2 改动禁区，零 diff）。"""

    def test_abc_to_official_anchored(self) -> None:
        out = abc_to_official(
            first_frame_anchored=True, shot_desc_en="A girl runs",
            style_en="webtoon", sfx_en="wind", has_music=False)
        assert out == (
            "For the target video, at 0.00 seconds into the target "
            "video, <Picture 1> (from [Shot 1]) is fully referenced."
            "\n\n"
            "integrated_multimodal_description: [Shot 1] webtoon "
            "A girl runs\n\n"
            "overall_soundscape: wind\n\n"
            "non_diegetic_music: N/A")

    def test_abc_to_official_bare_defaults(self) -> None:
        out = abc_to_official(
            first_frame_anchored=False, shot_desc_en="", style_en="",
            sfx_en="", has_music=True, music_en="piano")
        assert out == (
            "integrated_multimodal_description: [Shot 1] as described"
            "\n\n"
            "overall_soundscape: Quiet ambient atmosphere only; no "
            "prominent sound events.\n\n"
            "non_diegetic_music: piano")

    def test_abc_text_to_official_multishot(self) -> None:
        shots = [
            {"text": "少女进门", "camera": "缓推", "sfx": "开门声",
             "dialogue": "你来了", "start": 0.5, "end": 2.0},
            {"text": "回眸", "camera": "", "sfx": "开门声",
             "dialogue": "", "start": 2.0, "end": 4.5},
        ]
        out = abc_text_to_official(shots, style_en="Korean webtoon",
                                   shot_offset_s=0.0)
        assert out == (
            "integrated_multimodal_description: Korean webtoon "
            "[Shot 1] 少女进门. The camera motion: 缓推 (S1) says: "
            "<d>[Chinese] 你来了</d> "
            "[Shot 2] At 00:01.500, the camera cuts to 回眸\n\n"
            "overall_soundscape: 开门声.\n\n"
            "non_diegetic_music: N/A")


class TestBuilderGolden:
    """abc_builder_from_desc 桩注入形态基线（A 段头剥离+子件翻译序）。

    三组锚：中文标准头 / 字面 \n 变体（B 段标记仍命中）/ 英文
    Global style 头（当前 normalizer 剥英文头——批2 委托 convert
    真源后须保持等值，convert 侧需先补英文头剥离再切换）。
    """

    @pytest.mark.parametrize("prompt,want", [
        (GOLDEN_PROMPTS[2],
         "integrated_multimodal_description: 韩国网漫风 [Shot 1] "
         "少女特写\n\n"
         "overall_soundscape: Quiet ambient atmosphere only; no "
         "prominent sound events.\n\n"
         "non_diegetic_music: N/A"),
        # 真换行 A/B/C（GOLDEN_PROMPTS[9] 是字面反斜杠 n 变体——B 段
        # 标记不命中、整行漏进 style，不配做拼装锚；此处显式构造）
        ("\n".join(["A. 风格", "B. 人物：夏沐沐",
                    "C. [0.0s-3.0s] 画面：测试"]),
         "integrated_multimodal_description: 风格 [Shot 1] 少女特写"
         "\n\n"
         "overall_soundscape: Quiet ambient atmosphere only; no "
         "prominent sound events.\n\n"
         "non_diegetic_music: N/A"),
        ("A. Global style: anime\nB. setting\nC. [0.5s-2s] 画面：test",
         "integrated_multimodal_description: ANIME [Shot 1] 少女特写"
         "\n\n"
         "overall_soundscape: Quiet ambient atmosphere only; no "
         "prominent sound events.\n\n"
         "non_diegetic_music: N/A"),
    ])
    def test_builder(self, prompt: str, want: str) -> None:
        assert abc_builder_from_desc(
            prompt, parse_abc=_stub_parse, translate=_tr) == want


def test_baseline_count() -> None:
    """golden prompt 集=10 组（防意外缩减）。"""
    assert len(GOLDEN_PROMPTS) == 10
    assert len(_DETECT_BASELINE) == 10
