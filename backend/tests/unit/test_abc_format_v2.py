"""描述词格式 v2（多行镜头块）解析回归测试（2026-09-02 用户裁定改版）。

用户样例（原样入测）：C 段镜头为多行块——
    [0.1s-3.0s] 画面：…画面内容…
    运镜：平视大全景，沿街道轴向缓慢前推，机位高度 1.7 米
    台词：无 | 音效：蝉鸣渐起、轻柔风声
锁定三点：
  1. _parse_abc_shots 对 v2 格式解析出完整 shots（时间码/运镜/台词/音效）；
  2. v1 单行格式回归不破（续行合并幂等）；
  3. _merge_multiline_shot_blocks 幂等（合并后的单行不再二次变化）。
"""
from __future__ import annotations

import re

from backend.api.manga.common import (
    _merge_multiline_shot_blocks,
    _normalize_shot_line,
    _parse_abc_shots,
)

V2_SAMPLE = """A. 全局风格：3D CG 二次元写实融合风格，高精度 3D 建模，PBR 物理渲染，清晰硬表面质感，游戏实时光影，干净明暗过渡，二次元游戏质感，画面锐利通透，暗部细节完整。全程无字幕、无背景音乐，仅保留环境自然音效与角色台词。
B. 高密度世界观构建：盛夏午后的热浪裹着栀子花香与浅淡咸湿海味漫过街道，柏油路面被晒得泛着微光，碎金光斑在纸页上轻轻晃动。攥久了的地址条沾着指尖薄汗，陌生街景在视野里无限铺展，初到异地的忐忑顺着行李箱拉杆轻轻漫上心口。
C. 分镜时间轴
[0.1s-3.0s] 画面：盛夏临海街道向远方（正北）延伸，风卷细碎栀子花瓣缓缓飘落，澄澈蓝天铺着蓬松云絮，行道树筛下碎金光斑
运镜：平视大全景，沿街道轴向缓慢前推，机位高度 1.7 米
台词：无 | 音效：蝉鸣渐起、轻柔风声
[3.0s-7.0s] 画面：镜头落定在路口斑马线北侧，夏沐沐站在原地，右手指尖攥着皱巴巴的地址条，低头反复扫视纸上字迹，黑色行李箱立在脚边右手侧（画面左侧）
运镜：推镜后固定机位，平视人物中景
台词：夏沐沐（小声呢喃）：望云亭苑 C 区二街 36 号…… 到底在哪儿呢。
音效：行李箱滚轮轻响、风声持续
[7.0s-10.0s] 画面：镜头沿街道轴向小幅匀速前推，画面逐步收窄到人物上半身；夏沐沐抬眼望向前方街道（正北方向，画面后景），轻轻咬了咬下唇，右手指尖微微收紧攥住纸条，右手握着的行李箱拉杆随动作轻轻晃了一下
运镜：小幅匀速前推，从中景推至上半身近景
台词：无 | 音效：蝉鸣持续、布料轻微摩擦声"""

V1_SAMPLE = """A. 全局风格：日系动漫风、干净线稿。全程无字幕、无背景音乐、只有音效。
B. 高密度世界观构建：短发微卷、浅灰连帽卫衣的小林，低头整理货架。雨声滴落，便利店，夜，阴雨。
C. 分镜时间轴（分镜网格 1×2）：
[0.0s-0.1s] 画面：参考图保持 | 运镜：固定 | 音效：无
[0.1s-3.0s] 画面：[雨夜货架] 远景，小林背对镜头整理货架 | 运镜：Static Shot | 音效：雨滴声"""


def test_v2_multiline_blocks_parsed() -> None:
    parsed = _parse_abc_shots(V2_SAMPLE)
    shots = parsed["shots"]
    assert len(shots) == 3, f"应解析出 3 镜，实得 {len(shots)}"
    # 镜 1：时间码 + 运镜（含机位高度）+ 台词「无」归空 + 音效
    s1 = shots[0]
    assert (s1["start"], s1["end"]) == (0.1, 3.0)
    assert "机位高度 1.7 米" in s1["camera"]
    assert "平视大全景" in s1["camera"]
    assert s1["dialogue"] == ""
    assert "蝉鸣渐起" in s1["sfx"]
    assert "栀子花瓣" in s1["text"]
    # 镜 2：台词提取（角色名+语气+原文）
    s2 = shots[1]
    assert (s2["start"], s2["end"]) == (3.0, 7.0)
    assert "夏沐沐（小声呢喃）" in s2["dialogue"]
    assert "望云亭苑" in s2["dialogue"]
    assert "推镜后固定机位" in s2["camera"]
    assert "行李箱滚轮轻响" in s2["sfx"]
    # 镜 3
    s3 = shots[2]
    assert (s3["start"], s3["end"]) == (7.0, 10.0)
    assert "从中景推至上半身近景" in s3["camera"]
    assert s3["dialogue"] == ""
    # dur 合计 ≈ 10s（时间轴连续无缝）
    assert abs(sum(s["dur"] for s in shots) - 9.9) < 0.2


def test_v2_camera_line_preserved_in_normalized_row() -> None:
    """归一化行内保留运镜全文（下游 _segment_prompt 拼装依赖）。"""
    merged = _merge_multiline_shot_blocks(V2_SAMPLE)
    row = next(ln for ln in merged.splitlines()
               if ln.startswith("[0.1s-3.0s]"))
    norm = _normalize_shot_line(row)
    assert norm and "机位高度 1.7 米" in norm
    assert "蝉鸣渐起" in norm


def test_merge_is_idempotent() -> None:
    once = _merge_multiline_shot_blocks(V2_SAMPLE)
    twice = _merge_multiline_shot_blocks(once)
    assert once == twice


def test_v1_single_line_format_unchanged() -> None:
    """v1 格式回归：解析结果与合并前一致（续行合并零改动）。"""
    before = _parse_abc_shots(V1_SAMPLE)
    after = _parse_abc_shots(_merge_multiline_shot_blocks(V1_SAMPLE))
    assert before["layout"] == "1x2" == after["layout"]
    assert len(after["shots"]) == 1
    s = after["shots"][0]
    assert s["title"] == "[雨夜货架]"
    assert "Static Shot" in s["camera"]
    assert s["dialogue"] == ""  # v1 无台词字段
    assert "雨滴声" in s["sfx"]


def test_six_section_window_consumes_v2() -> None:
    """H3 链式的 C 段时间窗截取对 v2 多行块成立（块起点判窗）。"""
    from backend.services.inference.h3_chain_engine import _timeline_window
    abc_c = V2_SAMPLE.split("C. 分镜时间轴", 1)[1]
    win = _timeline_window("分镜时间轴：" + abc_c, 4.0)
    # 4s 窗口：镜1（0.1s）保留、镜3（7.0s）丢弃、镜2（3.0s 起点）保留
    assert "0.1s-3.0s" in win
    assert "3.0s-7.0s" in win
    assert "7.0s-10.0s" not in win
    # 块内多行保留（运镜/台词不因截窗丢失）
    assert "机位高度 1.7 米" in win


def test_finalize_backfills_missing_timecodes() -> None:
    """4B 对 v2 遵循不足时输出「[标题] 画面：…」缺时间码——finalize
    必须按时长等分补齐（否则网格标记丢失→视频退化单帧，2026-09-02
    实测镜1）。"""
    raw = """氛围：雨夜冷光。

B. 高密度世界观构建：浅灰卫衣的小林，便利店，夜，阴雨。

[雨夜货架] 画面：小林低头整理货架，指尖轻触货架边缘
运镜：平视远景，沿货架轴向缓慢前推，机位高度 2.1 米
台词：无 | 音效：雨声滴落
[玻璃倒影] 画面：玻璃门映出街灯，小林身影倒映
运镜：固定机位
台词：无 | 音效：雨声"""
    from backend.api.manga.common import _finalize_abc_body
    out = _finalize_abc_body(raw, "日系动漫风、干净线稿",
                             required_shots=2, duration=10.0)
    assert "C. 分镜时间轴（分镜网格 1×2）" in out, "网格标记必须由重拼补齐"
    assert "[0.1s-" in out and re.search(r"s-10s\]", out)
    parsed = _parse_abc_shots(out)
    assert len(parsed["shots"]) == 2
    assert (parsed["shots"][1]["start"],
            parsed["shots"][1]["end"]) == (5.0, 10.0)
    assert "机位高度 2.1 米" in parsed["shots"][0]["camera"]
