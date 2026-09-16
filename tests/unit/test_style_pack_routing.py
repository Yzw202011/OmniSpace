"""风格包路由金标准（2026-09-02 词序坑治理·护栏）。

style_routing_golden.json 是「首序匹配时代」真实行为的实测快照：
32 条预置卡中文定族行 + 1117 张种子卡（族回填口径 name+prompt）+
20 条对抗复合样例。detect_style 裁决机制的任何改动（关键词/优先
级/遮蔽规则）必须与之零差异；确需变更行为时，先重录快照并在提交
说明写明依据——这是「3D国漫悬疑被国漫抢走」类词序事故的防复发闸。

治理四步（同日落地的其他件）：
  ① 护栏=本文件 + tools/style_pack_lint.py；
  ② 显式化=PACK_PRIORITY（顺序从列表位置升格为声明数据）+
     comic_asset._PRESET_STYLE_PACK（预置卡定族弃「中文行跑正则」）；
  ③ 换裁决=keywords 化 + 同位遮蔽（maximal munch，复合词结构性
     压过碎片词）+ 优先级裁决（CSS 思路）；三处复制粘贴的匹配
     循环（嗅探/预置定族/族回填）收口到 detect_style；
  ④ 可解释=explain_style（裁决凭据：命中词/遮蔽关系）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import json
from pathlib import Path

from src.api.manga.comic_asset import _ART_STYLE_ZH, _PRESET_STYLE_PACK
from src.services.inference.gen_router import (
    DEFAULT_PACK,
    PACK_PRIORITY,
    STYLE_PACKS,
    detect_style,
    explain_style,
    parse_custom_pack,
)

_HERE = Path(__file__).parent
GOLDEN = json.loads((_HERE / "style_routing_golden.json").read_text("utf-8"))


def test_golden_preset_zh_lines():
    for key, want in GOLDEN["zh_lines"].items():
        assert key in _ART_STYLE_ZH, f"快照有而映射缺失: {key}"
        got = detect_style(_ART_STYLE_ZH[key]).sid
        assert got == want, f"预置卡 {key}: 期望 {want} 实得 {got}"


def test_golden_seed_cards():
    seed = json.loads((_HERE.parents[2] / "src" / "data" / "art_styles_seed_final.json")
                      .read_text("utf-8"))
    want = dict(GOLDEN["seed_cards"])
    drift = [it["name"] for it in seed
             if detect_style(f"{it['name']}、{it['prompt']}").sid
             != want.get(it["name"])]
    assert not drift, f"种子卡路由漂移 {len(drift)} 张: {drift[:10]}"


def test_golden_adversarial_compounds():
    for text, want in GOLDEN["adversarial"].items():
        got = detect_style(text).sid
        assert got == want, f"对抗样例「{text}」: 期望 {want} 实得 {got}"


def test_preset_static_map_matches_sniff():
    """预置卡静态映射 ⇄ 嗅探双向等价（静态映射的合法性来源）。"""
    for key, sid in _PRESET_STYLE_PACK.items():
        assert key in _ART_STYLE_ZH, f"静态映射有而中文行缺失: {key}"
        assert sid in {p.sid for p in STYLE_PACKS}, f"未知 sid: {sid}"
        assert detect_style(_ART_STYLE_ZH[key]).sid == sid, \
            f"预置卡 {key} 静态值 {sid} 与嗅探结果不一致"


def test_pack_priority_registry_exact():
    """优先级表必须与 STYLE_PACKS 逐一对应（漏登记/多登记都拦）。"""
    assert set(PACK_PRIORITY) == {p.sid for p in STYLE_PACKS}


def test_priority_sentinels():
    """复合词包必须压过碎片词包（平局时的裁决序，历史行为快照口径）。"""
    must_before = [
        ("xianxia_cg", "guofeng3d", "「仙侠」碎片词 vs「次世代二次元」复合词"),
        ("mystery3d", "donghua3d", "「国漫」碎片词 vs「国漫悬疑」复合词"),
        ("mystery3d", "cg3d", "「3D」强信号 vs 悬疑复合词"),
        ("donghua3d", "cg3d", "「3D」强信号 vs 国漫复合词"),
        ("guofeng_hist", "guofeng2d", "裸「古风」vs「3D古风」复合词"),
        ("guofeng_hist", "cg3d", "「3D」强信号 vs 3D古风复合词"),
    ]
    for a, b, why in must_before:
        assert PACK_PRIORITY[a] < PACK_PRIORITY[b], f"{a} 必须先于 {b}：{why}"


def test_shadowing_structural():
    """同位遮蔽是结构性规则：复合词胜出与凭据可解释。"""
    pack, note = explain_style("3D国漫悬疑谋杀案")
    assert pack.sid == "mystery3d"
    assert "国漫悬疑" in note and "遮蔽" in note
    # 裸碎片词仍归国漫包（未被复合词误伤）
    assert detect_style("国漫写实打斗").sid == "donghua3d"


def test_keywords_wellformed():
    for p in STYLE_PACKS:
        assert all(k.strip() for k in p.keywords), f"{p.sid} 存在空白关键词"
        assert len(set(p.keywords)) == len(p.keywords), f"{p.sid} 包内关键词重复"


def test_custom_pack_never_sniffs():
    pack, err = parse_custom_pack(json.dumps({
        "name": "测试包", "style_block": "test style", "post": "stylized"}))
    assert not err and pack is not None
    assert pack.longest_hit("国漫 悬疑 3D 水墨 anime") == 0, "自定义包不得参与嗅探"


def test_empty_text_default():
    assert detect_style("").sid == DEFAULT_PACK.sid == "default"
    assert detect_style("完全无关的纯叙事文本").sid == "default"
