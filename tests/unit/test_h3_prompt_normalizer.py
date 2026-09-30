"""H3 Prompt 转义层单测（2026-09-30 拍板 C：批3 转义层）。

覆盖（零 GPU：LLM 路径经 fake chat 注入）：
- 格式探测（abc / freeform 判据）
- 确定性三段式拼装（abc_to_official：指令行+三字段+N/A）
- 自由文本改写（成功/校验失败重试/降级 None/引擎异常降级）
- 统一入口（abc 走 builder / freeform 降级原文零阻断）
"""
from __future__ import annotations

from src.services.inference import h3_prompt_normalizer as norm

ABC_DESC = """A. 全局风格：3D CG 二次元写实融合风格。
B. 高密度世界观构建：盛夏午后的热浪漫过街道。
C. 分镜时间轴（分镜网格 2×2）：
[0.1s-3.0s] 画面：盛夏临海街道向远方延伸 | 音效：蝉鸣渐起
"""

FREEFORM_DESC = "夏沐沐拖着行李箱走在傍晚的海边街道上，风吹起她的长发，她停下来望向远处的灯塔"


# ───────────────────────── 格式探测 ─────────────────────────

def test_detect_abc():
    assert norm.detect_input_format(ABC_DESC) == "abc"


def test_detect_freeform():
    assert norm.detect_input_format(FREEFORM_DESC) == "freeform"
    assert norm.detect_input_format("") == "freeform"
    assert norm.detect_input_format("A girl walks along a beach") == "freeform"


# ─────────────────── 确定性三段式拼装 ───────────────────

def test_abc_to_official_structure():
    out = norm.abc_to_official(
        first_frame_anchored=True, shot_desc_en="She walks along the shore",
        style_en="3D CG style", sfx_en="Waves and seagulls")
    assert out.startswith("For the target video, at 0.00 seconds")
    assert "<Picture 1> (from [Shot 1]) is fully referenced." in out
    assert "integrated_multimodal_description: [Shot 1] 3D CG style" in out
    assert "overall_soundscape: Waves and seagulls" in out
    assert out.rstrip().endswith("non_diegetic_music: N/A")


def test_abc_to_official_no_anchor_and_music():
    out = norm.abc_to_official(
        first_frame_anchored=False, shot_desc_en="", style_en="",
        sfx_en="", has_music=True, music_en="Soft piano")
    assert not out.startswith("For the target video")   # 无锚不写指令行
    assert "[Shot 1] as described" in out                # 空描述兜底
    assert "no prominent sound events" in out            # sfx 兜底
    assert "non_diegetic_music: Soft piano" in out


# ─────────────────── LLM 改写（fake chat） ───────────────────

_GOOD_OUT = (
    "integrated_multimodal_description: [Shot 1] Cinematic, a girl with "
    "long black hair drags a suitcase along a seaside street at dusk. "
    "The camera tracks right at medium speed as she stops and gazes at "
    "a distant lighthouse.\n\n"
    "overall_soundscape: Steady waves, distant seagulls, and the rolling "
    "wheels of the suitcase.\n\n"
    "non_diegetic_music: N/A"
)

_BAD_OUT = "这是一段没有字段的中文输出"


def _chat_ok(messages, **kwargs):
    assert "OFFICIAL GUIDE BEGIN" in messages[0]["content"]
    return _GOOD_OUT


def test_freeform_success():
    out = norm.freeform_to_official(FREEFORM_DESC, chat=_chat_ok)
    assert out is not None
    assert "integrated_multimodal_description:" in out


def test_freeform_retry_then_success():
    calls = {"n": 0}

    def chat_flaky(messages, **kwargs):
        calls["n"] += 1
        return _BAD_OUT if calls["n"] == 1 else _GOOD_OUT

    out = norm.freeform_to_official(FREEFORM_DESC, chat=chat_flaky)
    assert out is not None and calls["n"] == 2


def test_freeform_degrade_after_retries():
    out = norm.freeform_to_official(
        FREEFORM_DESC, chat=lambda m, **k: _BAD_OUT, max_retries=1)
    assert out is None


def test_freeform_engine_exception_degrades():
    def chat_boom(messages, **kwargs):
        raise RuntimeError("engine down")

    assert norm.freeform_to_official(
        FREEFORM_DESC, chat=chat_boom) is None


def test_validate_output_rules():
    assert norm._validate_output(_GOOD_OUT)
    assert not norm._validate_output(_BAD_OUT)
    # <d> 内中文合法，<d> 外大量中文判失败
    mixed = _GOOD_OUT.replace(
        "a distant lighthouse.",
        "a distant lighthouse. (S1) says: <d>[cmn] 她停下来。</d>")
    assert norm._validate_output(mixed)
    bad_cjk = _GOOD_OUT.replace("seaside street", "海边街道上的漫长行走")
    assert not norm._validate_output(bad_cjk)


def test_strip_fence():
    assert norm._strip_fence(f"```json\n{_GOOD_OUT}\n```") == _GOOD_OUT
    assert norm._strip_fence(f"Final Prompt: {_GOOD_OUT}") == _GOOD_OUT


# ───────────────────────── 统一入口 ─────────────────────────

def test_normalize_abc_uses_builder():
    called = []

    def builder():
        called.append(1)
        return "OFFICIAL_FROM_BUILDER"

    assert norm.normalize_prompt(ABC_DESC, abc_builder=builder) \
        == "OFFICIAL_FROM_BUILDER"
    assert called == [1]


def test_normalize_abc_without_builder_passthrough():
    # abc 无 builder（既有链处理）→ 原样返回（仅首尾空白规整）
    assert norm.normalize_prompt(ABC_DESC) == ABC_DESC.strip()


def test_normalize_freeform_zero_blocking():
    # 改写降级 → 原文直喂（零阻断铁律）
    assert norm.normalize_prompt(
        FREEFORM_DESC, chat=lambda m, **k: _BAD_OUT) == FREEFORM_DESC


# ──────────── 普通单段链 A/B/C 拼装（2026-10-01 补缺口）────────────

def test_abc_text_to_official_multishot():
    """多镜合并：首镜无时间戳、切镜 At 分:秒.毫秒、台词 <d>、声音合并。"""
    shots = [
        {"text": "She stands on the pier", "camera": "static shot",
         "sfx": "Waves", "dialogue": "到底在哪儿呢。", "start": 0.1},
        {"text": "She looks at the paper", "camera": "slow push in",
         "sfx": "Waves", "dialogue": "", "start": 3.0},
    ]
    out = norm.abc_text_to_official(shots, style_en="3D CG style")
    assert "integrated_multimodal_description:" in out
    assert out.startswith("integrated_multimodal_description: 3D CG style [Shot 1] She stands")  # 风格锚前置+首镜无时间戳
    assert "At 00:02.900, the camera cuts to" in out     # 切镜毫秒格式
    assert "<d>[Chinese] 到底在哪儿呢。</d>" in out        # 台词逐字
    assert out.count("Waves") == 1                        # sfx 去重
    assert out.rstrip().endswith("non_diegetic_music: N/A")


def test_abc_builder_from_desc_flow(monkeypatch):
    """入口函数：abc 输入→拼装；非 abc→空串（回退自由文本路）。"""
    desc = ("A. 全局风格：3D CG。\nB. 世界观：临海街道。\n"
            "C. 分镜时间轴：\n"
            "[0.1s-3.0s] 画面：她站在码头上 | 音效：海浪声")
    assert norm.detect_input_format(desc) == "abc"

    def fake_parse(d):
        return {"shots": [{"text": "She stands on the pier", "camera": "",
                           "sfx": "Waves", "dialogue": "", "start": 0.1}]}

    out = norm.abc_builder_from_desc(
        desc, parse_abc=fake_parse, translate=lambda t: f"EN<{t}>")
    assert "integrated_multimodal_description:" in out
    assert "EN<" in out  # 翻译注入生效

    # 非 abc → 空串
    assert norm.abc_builder_from_desc(
        "plain text", parse_abc=fake_parse, translate=lambda t: t) == ""
    # 拆解无镜头 → 空串
    assert norm.abc_builder_from_desc(
        desc, parse_abc=lambda d: {"shots": []},
        translate=lambda t: t) == ""


def test_abc_builder_exception_degrades():
    """拼装异常 → 空串（零阻断，调用方回退既有链）。"""
    def boom(d):
        raise RuntimeError("parse fail")

    assert norm.abc_builder_from_desc(
        "C. 分镜时间轴：\n[0.1s-3.0s] 画面：测试",
        parse_abc=boom, translate=lambda t: t) == ""
