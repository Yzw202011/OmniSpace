"""H3 链式引擎纯逻辑直测（B9 高危模块补测 2026-09-13）。

背景（09-12 审计）：h3_chain_engine 是漫剧成片核心链，此前仅
_timeline_window 一个 helper 有直接测试。本文件以真 import 补齐
纯逻辑面的行为级用例（计划单六段式 / A-B-C 切分 / 绑定解析 /
模糊匹配打分）——DB 依赖的 _collect_refs 与进程链路另批覆盖。
红线：只加测试，不动引擎与路由（H3 主力锁定令）。
"""
from __future__ import annotations

import pytest

from backend.services.inference.h3_chain_engine import (
    _lcs_len,
    _parse_id_list,
    _six_section,
    _split_abc,
)

# ── _lcs_len：绑定名 vs 资产名模糊匹配打分 ─────────────────────────

def test_lcs_len_scores_substring_binding() -> None:
    # 行文里写「行李箱」应命中资产「黑色行李箱」（公共子串 3）
    assert _lcs_len("行李箱", "黑色行李箱") == 3
    assert _lcs_len("灯塔", "海岸灯塔远景") == 2  # ≥2 字门槛的临界样例


def test_lcs_len_no_common_and_empty() -> None:
    assert _lcs_len("小满", "阿澈") == 0
    assert _lcs_len("", "任意") == 0
    assert _lcs_len("任意", "") == 0


# ── _parse_id_list：asset_ids 三形态兼容（绑定真源）───────────────

def test_parse_id_list_three_forms() -> None:
    assert _parse_id_list(["a", " b ", ""]) == ["a", "b"]  # list+去空白
    assert _parse_id_list('["x1", "x2"]') == ["x1", "x2"]  # JSON 字符串
    assert _parse_id_list(" y1 , y2 ") == ["y1", "y2"]     # 逗号分隔兜底


def test_parse_id_list_dirty_inputs() -> None:
    assert _parse_id_list(None) == []
    assert _parse_id_list("") == []
    assert _parse_id_list("not-json") == ["not-json"]  # 非法 JSON→逗号兜底
    assert _parse_id_list('{"k": 1}') == ['{"k": 1}']  # JSON 对象非 list→兜底


# ── _split_abc：A/B/C 段切分（标记位置两容忍 + 词中不误命中）──────

def test_split_abc_line_start_and_after_punctuation() -> None:
    text = "A. 全局风格：日系网漫。\nB. 高密度世界观：海边小镇。\nC. 1. 画面：少女眺望。"
    abc = _split_abc(text)
    assert abc["a"].startswith("全局风格")
    assert abc["b"].startswith("高密度世界观")
    assert "少女眺望" in abc["c"]


def test_split_abc_inline_marks_after_sentence_punct() -> None:
    # 08-31 镜3 实测形态：AI 把标记写在同一行句读后（A 行首 + B/C 句读后）
    text = "A. 全局风格：日系。B.世界观补全。C.1. 画面：灯塔亮起。"
    abc = _split_abc(text)
    assert abc["a"].startswith("全局风格")
    assert abc["b"].startswith("世界观补全")
    assert "灯塔亮起" in abc["c"]


def test_split_abc_mid_word_not_matched_and_plain_text() -> None:
    # 词中字母不误命中（前置非法）：「XAB.」里 A 前是字母 X
    abc = _split_abc("测试 XAB. 误命中。正文继续。")
    assert abc["a"] == "" and abc["b"] == "" and abc["c"] == ""
    # 纯无标记文本三段皆空（调用方兜底原文）
    assert _split_abc("只有一段朴素描述。") == {"a": "", "b": "", "c": ""}


# ── _six_section：六段式计划单（产品核心输出契约）──────────────────

def test_six_section_structure_and_ref_numbering() -> None:
    refs = [
        {"kind": "character", "name": "小满", "src": "x"},
        {"kind": "scene", "name": "灯塔", "src": "y"},
        {"kind": "prop", "name": "行李箱", "src": "z"},
    ]
    desc = ("A. 全局风格：日系网漫。\n"
            "B. 世界观：海边小镇，夏夜。\n"
            "C. 1. 画面：少女在灯塔下拖行李箱前行。")
    plan = _six_section(desc, refs, 8.0)
    # 六段标题齐全且有序
    for section in ("subject_definitions:", "summary:",
                    "retention_analysis:", "detailed_description:",
                    "overall_soundscape:", "non_diegetic_music:"):
        assert section in plan
    # 参考图编号按序映射 + 中文类型
    assert "<Picture 1> 是角色形象`小满`的唯一视觉参考" in plan
    assert "<Picture 2> 是场景`灯塔`的唯一视觉参考" in plan
    assert "<Picture 3> 是道具`行李箱`的唯一视觉参考" in plan
    # 时长写进 soundscape
    assert "约 8 秒" in plan


def test_six_section_summary_skips_transition_shot() -> None:
    # 摘要首拍跳过「衔接上一镜头」承上空拍（08-31 镜3 修复语义）；
    # summary 标题行与内容分两行，取整段核对
    desc = ("B. 世界观。\n"
            "C. 1. 画面：衔接上一镜头的余韵。\n"
            "2. 画面：少女转身拉动行李箱。")
    plan = _six_section(desc, [], 6.0)
    lines = plan.splitlines()
    idx = lines.index("summary:")
    summary_block = "\n".join(lines[idx:idx + 2])
    assert "衔接上一镜头" not in summary_block
    assert "少女转身拉动行李箱" in summary_block


@pytest.mark.parametrize("seconds", [5.0, 15.0])
def test_six_section_reports_declared_duration(seconds: float) -> None:
    plan = _six_section("C. 1. 画面：静态注视。", [], seconds)
    assert f"约 {seconds:.0f} 秒" in plan
