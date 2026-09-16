"""A/B/C 分镜描述词 → MiniMax H3 (ComfyUI V7.2 TheodoreDirector) 转换层。

P0 转换层（2026-08-29 计划裁定）：纯函数、零后端依赖，后端经
sys.path 注入本目录后 import 使用（P1 接线）。输入输出：

输入  desc      行 A/B/C 描述词（C 段时间轴含台词/运镜/音效）
      assets    绑定资产 [{kind: character|scene|prop, name, file_path, prompt}]
      translate 中文→英文翻译函数（可 None：诚实降级保留中文）

输出  {"project": TheodoreDirector schemaVersion 4 JSON（注入
        V7.2 导播台 TheodoreDirector_Project 节点 widgets_values[0]），
       "plan": 逐镜裁时/拆格计划（P1 ffmpeg 与网格拆格用），
       "asset_files": {别名: 源路径}（P1 拷贝进 ComfyUI/input 清单），
       "warnings": [...], "degraded": bool, "degrade_reason": str}

关键适配（skills/_extracted/h3-seg-prompt-design 规则 × 本项目铁律）：
- H3 生成段 5-30s（用户裁定 2026-08-29，>15s 为训练域外超域生成）×
  我们 C 段 2-4s/镜 → 生成段取 5s 下限，plan.trim_to 记录 C 段真实
  时长，生成毕 ffmpeg 裁时回时间轴
- prompt 正文英文（translate 注入）；对白原文 <d>[cmn]...</d>
- 资产引用 {{ref:别名}}（素材集锁定 = asset_ids 绑定铁律）；
  角色 <Subject N> 全项目稳定编号；网格首帧 = 每镜时间对齐锚 ref
"""
from __future__ import annotations

import re
from collections.abc import Callable

from rules import (
    SEGMENT_MIN_S,
    assemble_prompt,
    clamp_segment_duration,
    detailed_description_section,
    dialogue_cutoff_mark,
    non_diegetic_music_section,
    overall_soundscape_section,
    retention_analysis_section,
    subject_definitions_section,
    summary_section,
)

SCHEMA_VERSION = 4
_VIDEO_CTX_FRAMES = 22     # V7.2 导播台默认（MotionContext）
_AUDIO_CTX_FRAMES = 24

_KIND_ZH = {"character": "角色", "scene": "场景", "prop": "道具"}


# ────────────────────────── A/B/C 解析 ──────────────────────────
# 口径对齐 src/api/manga/common.py（_normalize_shot_line /
# _parse_abc_shots），超集：补台词提取（<d> 标签需要）。
_TC = r"[\[【]?\s*(\d[\d.]*)s?\s*[-–~—]\s*(\d[\d.]*)s?\s*[\]】]?"


def _parse_dialogue(body: str) -> tuple[str, str]:
    """镜头行正文 → (说话人, 台词原文)。格式：台词：夏沐沐（小声呢喃）：……
    说话人=首个分隔符前文本；无说话人前缀时返回空说话人。"""
    m = re.search(r"台词[：:]\s*(.+?)(?=\s*(?:\||音效[：:]|$))", body)
    if not m:
        return "", ""
    raw = m.group(1).strip()  # 对白逐字保留（含尾部标点，skill 铁律）
    if raw in {"无", "无台词", "N/A", "-"}:
        return "", ""
    # 形态1：说话人（语气）：台词 —— 语气括号吞掉不捕获
    sp = re.match(r"^([^（(：:]{1,12})\s*[（(][^）)]*[）)]?\s*[：:]\s*(.+)$", raw)
    if sp:
        return sp.group(1).strip(), sp.group(2).strip()
    sp = re.match(r"^([^：:]{1,12})[：:]\s*(.+)$", raw)
    if sp:
        return sp.group(1).strip(), sp.group(2).strip()
    return "", raw


def parse_abc(desc: str) -> dict:
    """A/B/C 描述词 → {style, world, shots[], layout}。

    shots 每项 {start, end, dur, title, text, camera, sfx, speaker, line}
    （首帧保持段 [0.0s-…] 跳过，与 common._parse_abc_shots 同口径）。
    """
    text = (desc or "").strip()
    layout = None
    m = re.search(r"C[.、．]\s*分镜时间轴\s*[（(]\s*分镜网格\s*([12])\s*[×x]\s*"
                  r"([12])\s*[）)]", text)
    if m:
        layout = f"{m.group(1)}x{m.group(2)}"

    # A/B 段（风格锚 + 世界观锚）
    mb = re.search(r"(?m)^B[.、．]", text)
    mc = re.search(r"(?m)^C[.、．]", text)
    a_text = text[:mb.start()].strip() if mb else text[:mc.start()].strip() if mc else ""
    style = re.sub(r"^A[.、．]\s*全局风格[：:]?\s*", "", a_text).strip()
    world = ""
    if mb:
        b_end = mc.start() if mc else len(text)
        bm = re.search(
            r"(?m)^B[.、．].*?[：:]?\s*(.*?)(?=^C[.、．]|\Z)",
            text[mb.start():b_end], flags=re.S)
        if bm:
            world = bm.group(1).strip()

    shots: list[dict] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("A", "B", "C")) and re.match(r"^[ABC][.、．]", s):
            continue
        m = re.match(_TC + r"\s*", s)
        if not m:
            continue
        start, end = float(m.group(1)), float(m.group(2))
        if start == 0.0:
            continue  # 首帧保持段（视频协议 artifact）
        body = s[m.end():].strip()
        body = re.sub(r"^画面[：:]\s*", "", body)
        title = ""
        tm = re.match(r"^(\[[^\[\]]+\])\s*", body)
        if tm:
            title, body = tm.group(1), body[tm.end():].strip()
        seg_text = body.split("|")[0].strip()
        cm = re.search(r"运镜[：:]\s*([^|]+?)(?=\s*(?:\||台词[：:]|音效[：:]|$))", body)
        sm = re.search(r"音效[：:]\s*(.+?)(?=\s*(?:\||台词[：:]|$))", body)
        speaker, line_txt = _parse_dialogue(body)
        shots.append({
            "start": start, "end": end,
            "dur": round(max(end - start, 0.5), 2),
            "title": title, "text": seg_text,
            "camera": (cm.group(1).strip() if cm else ""),
            "sfx": (sm.group(1).strip() if sm else ""),
            "speaker": speaker, "line": line_txt,
        })
    return {"style": style, "world": world, "shots": shots, "layout": layout}


# ──────────────────────── 资产与 Subject 映射 ────────────────────────
def _clean_alias(name: str, idx: int) -> str:
    """资产名 → {{ref:}} 别名（保留中文字符，去标点空白；空名兜底）。"""
    alias = re.sub(r"[\s　]+", "", name or "").strip()
    alias = re.sub(r"[^\w\u4e00-\u9fff-]", "", alias)
    return alias or f"asset_{idx + 1}"


def build_asset_map(assets: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """绑定资产 → (h3_assets[], characters[], refs_meta)。

    - 别名唯一化（冲突加尾号）
    - character → <Subject N> 全项目稳定编号（skill：按项目级顺序分配，
      跨分镜不重排）
    - scene → 环境参考（不建 Subject）；prop → 直接 ref 引用
    """
    h3_assets: list[dict] = []
    characters: list[dict] = []
    refs_meta: dict[str, dict] = {}
    used: set[str] = set()
    subj_n = 0
    for i, a in enumerate(assets or []):
        kind = (a.get("kind") or "").strip()
        alias = _clean_alias(a.get("name") or "", i)
        while alias in used:
            alias = f"{alias}_{len(used) + 1}"
        used.add(alias)
        refs_meta[alias] = {"kind": kind, "file_path": a.get("file_path") or "",
                            "name": a.get("name") or ""}
        h3_assets.append({
            "id": alias, "alias": alias, "kind": "image",
            "path": f"{alias}.png",  # P1 拷贝进 ComfyUI/input 后的相对名
            "enabled": True, "fixed": False, "fixedOrder": i + 1,
            "shotIds": [], "includeVideoAudio": False,
            "durationSeconds": None, "audioDurationSeconds": None,
            "fingerprint": "",
        })
        if kind == "character":
            subj_n += 1
            characters.append({"n": subj_n, "alias": alias})
    return h3_assets, characters, refs_meta


# ────────────────────────── 说话人编号 ──────────────────────────
def build_speaker_map(shots: list[dict]) -> dict[str, str]:
    """台词说话人 → 稳定 S1..Sn（skill：按全片首次实际发声顺序）。"""
    speakers: dict[str, str] = {}
    for sh in shots:
        sp = (sh.get("speaker") or "").strip()
        if sp and sp not in speakers:
            speakers[sp] = f"S{len(speakers) + 1}"
    return speakers


# ────────────────────── 逐镜六段提示词组装 ──────────────────────
def _first_frame_alias(idx: int) -> str:
    """网格拆格首帧 ref 别名（ASCII，兼作 P1 拷贝文件名主干）。"""
    return f"shot_{idx + 1:03d}_first_frame"


def _maybe_en(text: str, translate: Callable[[str], str] | None,
              warnings: list[str], flag: list[bool]) -> str:
    """中文 → 英文（translate 注入；None 时诚实降级保留中文）。"""
    t = (text or "").strip()
    if not t:
        return ""
    if translate is None:
        if not flag[0]:
            flag[0] = True
            warnings.append("未提供翻译函数：提示词正文保留中文"
                            "（H3 英文规范未满足，诚实降级）")
        return t
    try:
        out = translate(t)
        return (out or t).strip()
    except Exception as exc:  # noqa: BLE001
        if not flag[0]:
            flag[0] = True
            warnings.append(f"翻译失败，保留中文: {exc}")
        return t


def _shot_prompt(sh: dict, idx: int, style_en: str, world_en: str,
                 env_refs: list[str], subjects: list[dict],
                 translate, warnings: list[str],
                 flag: list[bool], speaker_map: dict[str, str],
                 has_music: bool) -> str:
    """单镜 → 六段式 H3 prompt（字段顺序由 rules.assemble_prompt 锁定）。"""
    ff = _first_frame_alias(idx)
    action_en = _maybe_en(f"{sh.get('text', '')}", translate, warnings, flag)
    camera_en = _maybe_en(sh.get("camera", ""), translate, warnings, flag)
    sfx_en = _maybe_en(sh.get("sfx", ""), translate, warnings, flag)
    style_t = _maybe_en(style_en, translate, warnings, flag)

    dialogues: list[dict] = []
    line_txt = (sh.get("line") or "").strip()
    if line_txt:
        speaker = (sh.get("speaker") or "").strip()
        dialogues.append({
            "speaker": speaker or "Subject",
            "speaker_id": speaker_map.get(speaker, "S1"),
            "line": line_txt,  # 对白原文，不翻译（skill 铁律）
            "manner": "says",
        })

    w_en = _maybe_en(world_en, translate, warnings, flag)
    subj_blocks = [{"n": s["n"], "alias": s["alias"], "desc_en": w_en}
                   for s in subjects]
    return assemble_prompt({
        "subject_definitions": subject_definitions_section(
            env_refs, subj_blocks, ff),
        "summary": summary_section(action_en or w_en or "the scripted action"),
        "retention_analysis": retention_analysis_section(
            ff, subj_blocks, env_refs),
        "detailed_description": detailed_description_section(
            style_t, action_en or w_en, camera_en, dialogues),
        "overall_soundscape": overall_soundscape_section(sfx_en),
        "non_diegetic_music": non_diegetic_music_section(has_music),
    })


# ────────────────────────── 主入口 ──────────────────────────
def convert_row_to_project(
    desc: str,
    assets: list[dict] | None = None,
    *,
    project_name: str = "",
    project_id: str = "",
    base_seed: int = 123456790,
    fps: int = 24,
    merge_mode: bool = False,
    has_music: bool = False,
    translate: Callable[[str], str] | None = None,
) -> dict:
    """分镜行 A/B/C 描述词 → TheodoreDirector schemaVersion 4 工程包。

    merge_mode=False（默认）：逐镜独立生成段（网格协议首帧逐镜锚定），
    每段 5s 下限生成、plan.trim_to 记录 C 段精确时长供裁时。
    merge_mode=True：相邻镜头贪婪合并至 ≥5s 段（latentRelay 接续，
    MotionContext 跨段一致性；首帧取段首镜网格格）。
    """
    parsed = parse_abc(desc)
    shots = parsed["shots"]
    warnings: list[str] = []
    flag = [False]
    if not shots:
        raise ValueError("C 段时间轴未解析到实体镜头（首帧保持段除外）")

    h3_assets, characters, refs_meta = build_asset_map(assets or [])
    env_refs = [a["alias"] for a in h3_assets
                if refs_meta[a["alias"]]["kind"] == "scene"]
    speaker_map = build_speaker_map(shots)

    # 段划分：逐镜 / 贪婪合并（≥5s）
    if merge_mode:
        groups: list[list[int]] = []
        cur: list[int] = []
        acc = 0.0
        for i, sh in enumerate(shots):
            cur.append(i)
            acc += sh["dur"]
            if acc >= SEGMENT_MIN_S:
                groups.append(cur)
                cur, acc = [], 0.0
        if cur:
            # 尾段不足 5s：独立成段（钳 5s 生成后裁时），不回吞前段
            # ——回吞会让段首镜网格首帧锚漂移到前段中间
            groups.append(cur)
    else:
        groups = [[i] for i in range(len(shots))]

    proj_shots: list[dict] = []
    plan: list[dict] = []
    for gi, grp in enumerate(groups):
        first = shots[grp[0]]
        gen_dur = clamp_segment_duration(
            sum(shots[i]["dur"] for i in grp))
        prompt = _shot_prompt(
            first, grp[0], parsed["style"], parsed["world"], env_refs,
            characters, translate, warnings, flag, speaker_map, has_music)
        ff = _first_frame_alias(grp[0])
        proj_shots.append({
            "id": f"shot_{gi + 1:03d}",
            "title": (first.get("title") or f"Shot {gi + 1}").strip("[]"),
            "prompt": prompt,
            "negativePrompt": "",
            "durationSeconds": gen_dur,
            "enabled": True,
            "seed": int(base_seed) + grp[0],  # 逐镜派生 base+镜序（项目铁律）
            "disabledAssetIds": [],
            "latentRelay": merge_mode and gi > 0,
            "secondSampling": False,
        })
        plan.append({
            "shot_id": f"shot_{gi + 1:03d}",
            "cells": grp,                      # 对应网格拆格序号（0-based）
            "first_frame_alias": ff,
            "gen_seconds": gen_dur,
            # 接续段重复上下文帧数（P2 latentRelay：生成段头部含 22 帧
            # 上一段尾帧锚，ffmpeg 裁时前先剥掉 22/24s 前缀）
            "ctx_frames": _VIDEO_CTX_FRAMES if (merge_mode and gi > 0) else 0,
            "trim_specs": [                    # 生成毕裁时回 C 段精确时长
                {"cell": i, "trim_to": shots[i]["dur"],
                 "start_in_segment": round(
                     sum(shots[j]["dur"] for j in grp[:k]), 2)}
                for k, i in enumerate(grp)],
            "latent_relay": merge_mode and gi > 0,
        })

    # 首帧锚注册为逐镜图像资产（fixed 素材排序优先 → <Picture 1>
    # 恒为本镜网格首帧；shotIds 单镜定域；P1 拆格落盘）
    for k, p in enumerate(plan):
        h3_assets.append({
            "id": p["first_frame_alias"], "alias": p["first_frame_alias"],
            "kind": "image",
            "path": f"{p['first_frame_alias']}.png",
            "enabled": True, "fixed": True, "fixedOrder": k + 1,
            "shotIds": [p["shot_id"]], "includeVideoAudio": False,
            "durationSeconds": None, "audioDurationSeconds": None,
            "fingerprint": "",
        })

    project = {
        "schemaVersion": SCHEMA_VERSION,
        "project": {"id": project_id or "omnispace_row",
                    "name": project_name or "OmniSpace 分镜",
                    "runId": "1"},
        "defaults": {"fps": fps, "baseSeed": int(base_seed)},
        "promptPrefix": "",
        "promptSuffix": "",
        "continuity": {"mode": "h3_av_latent",
                       "videoContextFrames": _VIDEO_CTX_FRAMES,
                       "audioContextFrames": _AUDIO_CTX_FRAMES,
                       "durationMode": "final_output"},
        "assets": h3_assets,
        "shots": proj_shots,
    }
    asset_files = {alias: m["file_path"] for alias, m in refs_meta.items()
                   if m["file_path"]}
    return {
        "project": project,
        "plan": plan,
        "asset_files": asset_files,
        "warnings": warnings,
        "degraded": flag[0],
        "degrade_reason": ("translation_unavailable" if flag[0] else ""),
    }


# 台词截断标记透出（P1 拼接裁时判定用）
DIALOGUE_CUTOFF = dialogue_cutoff_mark()
