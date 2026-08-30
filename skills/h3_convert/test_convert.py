# ruff: noqa: E501  —— SAMPLE_DESC 为分镜描述词协议真实样例，协议行不可折行
"""h3_convert 转换层单测（P0）。

样例取自「竞品对齐-夏沐沐」项目真实描述词口径（4 镜 2×2 网格，
0.1s 起、含首帧保持段/台词/运镜/音效），覆盖：
- A/B/C 解析（含台词说话人提取、保持段跳过、网格判据）
- 资产别名/Subject 稳定编号/环境参考分流
- 六段结构字段顺序与 {{ref:}} 白名单
- 5s 下限钳制 + trim 裁时计划
- 翻译注入与诚实降级（translate=None 保留中文）
- merge_mode 贪婪合并
- 校验闸门（好包通过 / 坏包拦截）

运行：runtime/py310/python.exe -m pytest skills/h3_convert -q
"""
from __future__ import annotations

import convert as conv
import pytest
import validate as val

SAMPLE_DESC = """A. 全局风格：3D CG 二次元写实融合风格，细节刻画精致，光影层次丰富，画面锐利通透。全程无字幕、无背景音乐，仅保留环境自然音效与角色台词。
B. 高密度世界观构建：盛夏午后的热浪裹着栀子花香与浅淡咸湿海味漫过街道，柏油路面被晒得泛着微光。攥久了的地址条沾着指尖薄汗，初到异地的忐忑顺着行李箱拉杆轻轻漫上心口。
C. 分镜时间轴（分镜网格 2×2）：
[0.0s-0.1s] 画面：参考图保持 | 运镜：固定 | 音效：无
[0.1s-3.0s] 画面：盛夏临海街道向远方延伸，风卷细碎栀子花瓣缓缓飘落，行道树筛下碎金光斑运镜：平视大全景，沿街道轴向缓慢前推台词：无 | 音效：蝉鸣渐起、轻柔风声
[3.0s-5.5s] 画面：镜头落定在路口斑马线北侧，夏沐沐站在原地，右手指尖攥着皱巴巴的地址条，黑色行李箱立在脚边右手侧运镜：推镜后固定机位，平视人物中景台词：夏沐沐（小声呢喃）：望云亭苑 C 区二街 36 号…… 到底在哪儿呢。音效：行李箱滚轮轻响、风声持续
[5.5s-7.5s] 画面：夏沐沐沿街道缓步前行，裙摆随海风轻轻摆动运镜：侧面跟随镜头，中景台词：无 | 音效：脚步声、海浪声
[7.5s-10.0s] 画面：镜头沿街道轴向小幅匀速前推，画面逐步收窄到人物上半身，夏沐沐抬眼望向前方街道运镜：小幅匀速前推，从中景推至上半身近景音效：蝉鸣持续、布料轻微摩擦声"""

SAMPLE_ASSETS = [
    {"kind": "character", "name": "夏沐沐",
     "file_path": "comic_assets/xia_mumu.png", "prompt": "白T恤牛仔裤黑长直"},
    {"kind": "scene", "name": "临海街道",
     "file_path": "comic_assets/street.png", "prompt": "盛夏临海街道"},
    {"kind": "prop", "name": "黑色行李箱",
     "file_path": "comic_assets/suitcase.png", "prompt": "黑色硬壳拉杆行李箱"},
]


@pytest.fixture()
def result_no_trans() -> dict:
    return conv.convert_row_to_project(
        SAMPLE_DESC, SAMPLE_ASSETS, project_name="竞品对齐-夏沐沐")


@pytest.fixture()
def result_en() -> dict:
    # 假翻译器：剥中文只留长度指纹（保证正文无未翻译中文残留）
    import re as _re
    return conv.convert_row_to_project(
        SAMPLE_DESC, SAMPLE_ASSETS, project_name="竞品对齐-夏沐沐",
        translate=lambda t: "EN<{}>".format(len(_re.sub(r"\s", "", t))))


# ────────────────────────── 解析 ──────────────────────────
def test_parse_abc_structure():
    p = conv.parse_abc(SAMPLE_DESC)
    assert p["layout"] == "2x2"
    assert len(p["shots"]) == 4  # 首帧保持段 [0.0s-0.1s] 跳过；恒偶
    assert p["shots"][0]["start"] == 0.1
    assert "3D CG 二次元写实融合风格" in p["style"]
    assert "栀子花香" in p["world"]


def test_parse_dialogue_speaker_and_verbatim():
    p = conv.parse_abc(SAMPLE_DESC)
    sh = p["shots"][1]
    assert sh["speaker"] == "夏沐沐"
    # 逐字保留（含省略号与句号，不剥标点）
    assert sh["line"] == "望云亭苑 C 区二街 36 号…… 到底在哪儿呢。"
    assert p["shots"][0]["line"] == ""  # 台词：无
    assert p["shots"][2]["line"] == ""


def test_parse_camera_sfx_split():
    p = conv.parse_abc(SAMPLE_DESC)
    sh = p["shots"][1]
    assert "中景" in sh["camera"]
    assert "台词" not in sh["camera"]
    assert "行李箱滚轮轻响" in sh["sfx"]


# ────────────────────────── 资产映射 ──────────────────────────
def test_asset_map_subject_stable_numbering():
    h3_assets, characters, refs = conv.build_asset_map(SAMPLE_ASSETS)
    assert [c["n"] for c in characters] == [1]          # 单角色 Subject 1
    assert characters[0]["alias"] == "夏沐沐"
    aliases = {a["alias"] for a in h3_assets}
    assert {"夏沐沐", "临海街道", "黑色行李箱"} <= aliases
    assert refs["临海街道"]["kind"] == "scene"


def test_asset_alias_conflict_suffix():
    h3_assets, _, _ = conv.build_asset_map(
        [{"kind": "prop", "name": "行李箱", "file_path": "a.png"},
         {"kind": "prop", "name": "行李箱", "file_path": "b.png"}])
    aliases = [a["alias"] for a in h3_assets]
    assert len(set(aliases)) == 2  # 唯一化


# ────────────────────────── 工程包 ──────────────────────────
def test_project_schema(result_no_trans):
    project = result_no_trans["project"]
    assert project["schemaVersion"] == 4
    assert project["continuity"]["mode"] == "h3_av_latent"
    assert project["continuity"]["videoContextFrames"] == 22
    assert [s["id"] for s in project["shots"]] == [
        "shot_001", "shot_002", "shot_003", "shot_004"]
    # seed 逐镜派生 base+镜序
    seeds = [s["seed"] for s in project["shots"]]
    assert seeds == [123456790 + i for i in range(4)]


def test_first_frame_assets_registered_per_shot(result_no_trans):
    project = result_no_trans["project"]
    ff = [a for a in project["assets"] if a["alias"].endswith("_first_frame")]
    assert len(ff) == 4
    for a, sh in zip(ff, project["shots"], strict=False):
        assert a["shotIds"] == [sh["id"]]
        # fixed 素材排序优先（Theodore references.py）→ <Picture 1> 恒为首帧
        assert a["fixed"] is True
        assert a["fixedOrder"] >= 1
        assert f"{{{{ref:{a['alias']}}}}}" in sh["prompt"]


def test_segment_max_30s_clamp():
    from rules import clamp_segment_duration as c
    assert c(40.0) == 30.0   # 用户裁定 2026-08-29：每镜允许 5-30s
    assert c(16.0) == 16.0   # 超 H3 训练域但允许（P3 标定把关）
    assert c(2.0) == 5.0


def test_segment_duration_clamped_and_trim_plan(result_no_trans):
    plan = result_no_trans["plan"]
    for p, sh in zip(plan, result_no_trans["project"]["shots"], strict=False):
        assert sh["durationSeconds"] == 5.0  # 2.9/2.5/2.0/2.5s → 5s 下限
        assert p["gen_seconds"] == 5.0
        for ts in p["trim_specs"]:
            assert ts["trim_to"] < 5.0       # 裁时回 C 段真实时长
    assert plan[0]["trim_specs"][0]["trim_to"] == pytest.approx(2.9)
    assert plan[3]["trim_specs"][0]["trim_to"] == pytest.approx(2.5)


def test_six_field_order(result_en):
    for sh in result_en["project"]["shots"]:
        fields = [m.group(1) for m in
                  __import__("re").finditer(r"(?m)^([a-z_]+):", sh["prompt"])]
        assert tuple(fields) == val.FULL_FIELDS


def test_dialogue_verbatim_in_d_tag(result_no_trans):
    prompt = result_no_trans["project"]["shots"][1]["prompt"]
    assert "<d>[cmn] 望云亭苑 C 区二街 36 号…… 到底在哪儿呢。</d>" in prompt


def test_music_na_when_no_bgm(result_no_trans):
    for sh in result_no_trans["project"]["shots"]:
        assert "non_diegetic_music:\nN/A" in sh["prompt"]


def test_honest_degrade_without_translator(result_no_trans):
    assert result_no_trans["degraded"] is True
    assert result_no_trans["degrade_reason"] == "translation_unavailable"
    # 中文正文保留 + 对白仍带 <d> 标签
    prompt = result_no_trans["project"]["shots"][0]["prompt"]
    assert "盛夏临海街道" in prompt


def test_translate_injected_body_english(result_en):
    prompt = result_en["project"]["shots"][0]["prompt"]
    assert "EN<" in prompt                       # 正文经翻译
    assert "<d>[cmn]" in result_en["project"]["shots"][1]["prompt"]
    body = prompt.split("<d>")[0]
    assert "盛夏临海街道" not in body             # 正文不含未翻译中文
    assert result_en["degraded"] is False


# ────────────────────────── 合并模式 ──────────────────────────
def test_merge_mode_groups_to_min5():
    r = conv.convert_row_to_project(
        SAMPLE_DESC, SAMPLE_ASSETS, merge_mode=True)
    # 4 镜 2.9+2.5+2.0+2.5：贪婪 → [0,1](5.4s) + [2,3](4.5s→钳5s)
    assert [len(p["cells"]) for p in r["plan"]] == [2, 2]
    assert r["plan"][0]["gen_seconds"] == pytest.approx(5.4)
    assert r["plan"][1]["gen_seconds"] == 5.0
    assert r["project"]["shots"][0]["latentRelay"] is False   # 首段
    assert r["project"]["shots"][1]["latentRelay"] is True    # 接续段


def test_merge_mode_relay_ctx_frames_and_offset_trim():
    r = conv.convert_row_to_project(
        SAMPLE_DESC, SAMPLE_ASSETS, merge_mode=True)
    p0, p1 = r["plan"]
    # 首段无上下文前缀；接续段带 22 帧 relay 上下文（P2 偏移切分依据）
    assert p0["latent_relay"] is False and p0["ctx_frames"] == 0
    assert p1["latent_relay"] is True and p1["ctx_frames"] == 22
    # 首段段内偏移：镜2 start_in_segment = 镜1 时长 2.9
    assert p0["trim_specs"][1]["start_in_segment"] == pytest.approx(2.9)
    # 接续段段内偏移：镜4 start_in_segment = 镜3 时长 2.0（不含 ctx 前缀）
    assert p1["trim_specs"][1]["start_in_segment"] == pytest.approx(2.0)
    # 裁时总和 = C 段内容总时长（拼接后时间轴无损）
    total = sum(ts["trim_to"] for p in r["plan"] for ts in p["trim_specs"])
    assert total == pytest.approx(9.9)  # 2.9+2.5+2.0+2.5


def test_per_shot_mode_no_relay_ctx():
    r = conv.convert_row_to_project(SAMPLE_DESC, SAMPLE_ASSETS)
    for p in r["plan"]:
        assert p["latent_relay"] is False
        assert p["ctx_frames"] == 0


# ────────────────────────── 校验闸门 ──────────────────────────
def test_validate_pass_on_good_package(result_en):
    errors, _warns = val.validate_result(result_en)
    assert errors == []
    val.assert_submittable(result_en)  # 不抛


def test_validate_rejects_unregistered_ref(result_en):
    bad = result_en["project"]["shots"][0]["prompt"].replace(
        "{{ref:临海街道}}", "{{ref:未登记场景}}")
    result_en["project"]["shots"][0]["prompt"] = bad
    errors, _ = val.validate_result(result_en)
    assert any("未登记素材" in e for e in errors)
    with pytest.raises(ValueError, match="校验失败"):
        val.assert_submittable(result_en)


def test_validate_rejects_bad_duration(result_en):
    result_en["project"]["shots"][0]["durationSeconds"] = 3.0
    errors, _ = val.validate_result(result_en)
    assert any("不在" in e for e in errors)


def test_validate_rejects_official_numbering(result_en):
    result_en["project"]["shots"][0]["prompt"] += "\n<Picture 1> extra"
    errors, _ = val.validate_result(result_en)
    assert any("官方" in e for e in errors)


def test_validate_rejects_trim_over_gen(result_en):
    result_en["plan"][0]["trim_specs"][0]["trim_to"] = 8.0
    errors, _ = val.validate_result(result_en)
    assert any("trim_to" in e for e in errors)


def test_validate_subject_number_drift(result_en):
    p1 = result_en["project"]["shots"][0]["prompt"]
    p2 = result_en["project"]["shots"][1]["prompt"]
    result_en["project"]["shots"][1]["prompt"] = p2.replace(
        "<Subject 1> is the recurring character from {{ref:夏沐沐}}",
        "<Subject 2> is the recurring character from {{ref:夏沐沐}}")
    assert "<Subject 1>" in p1  # 对照：镜1 为 Subject 1
    errors, _ = val.validate_result(result_en)
    assert any("编号漂移" in e for e in errors)


def test_empty_shots_raises():
    with pytest.raises(ValueError, match="未解析到实体镜头"):
        conv.convert_row_to_project("A. 全局风格：测试\nB. 高密度世界观构建：测试")
