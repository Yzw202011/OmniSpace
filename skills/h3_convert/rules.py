"""H3 六段式提示词规则编码（skills/h3_convert P0 转换层）。

规则真源 = skills/_extracted/h3-seg-prompt-design（V7 Theodore 导播台
专用 skill，2026-08-29 说明.txt 裁定 V7 路线用本 skill）。本模块把
skill 的非协商规则固化为纯函数，供 convert.py 组装调用：

- 六段结构字段顺序固定（validate_project.py FULL_FIELDS）
- 正文英文；对白逐字保留原文于 <d>[Language] ...</d>
- 素材引用 {{ref:别名}}；<Subject N> 是复现单元不是素材引用
- overall_soundscape 不含对白/配乐；non_diegetic_music 无配乐写 N/A
- 每段 5-15s（我们 C 段 2-4s/镜 → 生成段取 5s 下限，生成毕裁时）
"""
from __future__ import annotations

# 六段字段顺序（与 skill 校验脚本 FULL_FIELDS 一致，不得增删排序）
FULL_FIELDS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)

# 生成段时长边界（用户裁定 2026-08-29：每镜允许 5-30s；
# 注意 H3 训练帧范围约 124-362 帧（5-15s），>15s 段为超域生成，
# 质量风险由 P3 标定把关）
SEGMENT_MIN_S = 5.0
SEGMENT_MAX_S = 30.0

# 每段参考素材上限（skill 非协商项：9 图 / 3 视频 / 3 音频）
MAX_REF_IMAGES = 9
MAX_REF_VIDEOS = 3
MAX_REF_AUDIOS = 3

# 对白语言标签（我们台词为中文普通话）
DIALOGUE_LANG = "cmn"


def clamp_segment_duration(seconds: float) -> float:
    """C 段镜头时长 → H3 生成段时长（5-30s，用户裁定 2026-08-29）。

    我们分镜每镜 2-4s，低于 H3 5s 下限：按 5s 生成，P1 生成毕
    ffmpeg 裁时回 C 段精确时长（trim_to 由 convert.py 记录）。
    """
    return max(SEGMENT_MIN_S, min(SEGMENT_MAX_S, float(seconds)))


def subject_definitions_section(env_refs: list[str],
                                subjects: list[dict],
                                first_frame_alias: str) -> str:
    """subject_definitions 段。

    env_refs: 场景资产别名（环境参考，不建 Subject）；
    subjects: [{"n": 1, "alias": "夏沐沐", "desc_en": "..."}]；
    first_frame_alias: 网格拆格首帧（时间对齐锚点，自定义 ref 直引）。
    """
    lines: list[str] = []
    for alias in env_refs:
        lines.append(
            f"{{{{ref:{alias}}}}} is the environment reference for the "
            f"location in this segment, preserving the referenced "
            f"architecture, spatial layout, lighting and props.")
    for s in subjects:
        desc = s.get("desc_en") or ""
        lines.append(
            f"<Subject {s['n']}> is the recurring character from "
            f"{{{{ref:{s['alias']}}}}}, preserving the referenced face, "
            f"hairstyle, outfit and body proportions"
            + (f": {desc}" if desc else "."))
    lines.append(
        f"The image {{{{ref:{first_frame_alias}}}}} is the exact opening "
        f"frame of this segment at 0.00 seconds; it defines the camera "
        f"placement, composition, and all object states at the start.")
    return "\n".join(lines)


def summary_section(action_en: str) -> str:
    """summary 段：一句话主行动（英文）。"""
    return f"[reference generation] {action_en.strip()}"


def retention_analysis_section(first_frame_alias: str,
                               subjects: list[dict],
                               env_refs: list[str]) -> str:
    """retention_analysis 段：首帧保持声明（C 段「首帧保持段」语义）。

    首帧 fully_preserved + 角色/场景 fully_preserved（部分变化项由
    调用方通过 partial 列表降级标注）。
    """
    lines = [
        f"{{{{ref:{first_frame_alias}}}}} (opening frame): "
        f"fully_preserved - the segment opens exactly from this frame, "
        f"reproducing camera, composition, lighting and every object "
        f"state at 0.00 seconds before motion begins."]
    for s in subjects:
        lines.append(
            f"<Subject {s['n']}> ({s['alias']}): fully_preserved - "
            f"identity, face, hairstyle, outfit and proportions remain "
            f"consistent with the reference asset throughout.")
    for alias in env_refs:
        lines.append(
            f"{{{{ref:{alias}}}}} (environment): fully_preserved - "
            f"architecture, spatial layout and lighting state remain "
            f"consistent with the reference image.")
    return "\n".join(lines)


def detailed_description_section(style_en: str,
                                 action_en: str,
                                 camera_en: str,
                                 dialogues: list[dict]) -> str:
    """detailed_description 段。

    style_en: A 段全局风格（英文，含项目 art_style 与氛围）；
    action_en: 本镜画面/动作（英文）；camera_en: 运镜（英文）；
    dialogues: [{"speaker": "夏沐沐", "speaker_id": "S1",
                 "line": "望云亭苑 C 区二街 36 号……", "manner": "murmurs"}]
    —— 对白逐字保留原文于 <d>[cmn] ...</d>，不翻译不润色。
    """
    parts = [
        "The target video uses the established global art style: "
        + (style_en.strip() or "as defined by the project style")
        + ". No text, logos, borders, or watermarks appear.",
        f"[Shot 1] At 0.00 seconds, {action_en.strip()}",
    ]
    if camera_en:
        parts.append(f"Camera: {camera_en.strip()}")
    for d in dialogues:
        manner = d.get("manner") or "says"
        parts.append(
            f"<{d['speaker_id']}> {manner}: <d>[{DIALOGUE_LANG}] "
            f"{d['line']}</d>")
    return "\n".join(parts)


def overall_soundscape_section(sfx_en: str) -> str:
    """overall_soundscape 段：仅环境声/动作声/非语言人声，不含对白。"""
    sfx = (sfx_en or "").strip()
    if not sfx or sfx in {"无", "none", "N/A"}:
        return "Quiet ambient atmosphere only; no prominent sound events."
    return sfx


def non_diegetic_music_section(has_music: bool = False,
                               music_en: str = "") -> str:
    """non_diegetic_music 段：我们分镜「全程无背景音乐」→ N/A。"""
    if not has_music:
        return "N/A"
    return (music_en or "").strip() or "N/A"


def dialogue_cutoff_mark() -> str:
    """台词被镜头结尾截断时的标记（skill 规则 <cutoff>）。"""
    return "<cutoff>"


def assemble_prompt(sections: dict[str, str]) -> str:
    """六段按固定字段顺序拼装（缺段报错，防静默降段）。

    输出形态与 skill/demo 工程一致：每段「字段名:」独立一行 + 内容。
    """
    missing = [k for k in FULL_FIELDS if not (sections.get(k) or "").strip()]
    if missing:
        raise ValueError(f"缺少提示词字段: {', '.join(missing)}")
    return "\n\n".join(f"{k}:\n{sections[k].strip()}" for k in FULL_FIELDS)
