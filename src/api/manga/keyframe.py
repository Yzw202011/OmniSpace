"""漫剧关键帧域路由：关键帧生成 / 批量 / 列表 / 重生成 / 回滚 / 故事生图。

TASK-P2-01 自 manga.py 按路由域拆出（原文件 4521 行 → 包）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Query

from ...config import (
    DATA_DIR,
    ROOT_DIR,
)
from ...data.database import Database, get_db_safe
from ...data.models import (
    KeyframeBatchRequest,
    KeyframeGenerateRequest,
    StoryKeyframeRequest,
)
from ...engines.vllm_service import get_vllm_service, pil_images_to_b64
from ...middleware.error_handler import ApiError, ok
from ...middleware.feature_lock import acquire_or_raise
from ...services.event_log import log_event
from ...services.image_queue import get_image_queue
from ...services.inference import face_similarity as face_sim
from ...services.inference.comfy_paint_engine import (
    comfy_paint_available,
    get_comfy_paint_engine,
    pulid_available,
)
from ...services.inference.gen_router import detect_style, explain_style, resolve_route
from ...services.inference.paint_engine import get_paint_engine
from ...services.offload import run_blocking
from .common import (
    _ABC_MARK,
    _IMG_GEN_MAX,
    _KEYFRAME_DIR,
    _KF_COLS,
    _SB_ROW_COLS,
    _STYLE_NEGATIVE,
    IMG_TARGET_H,
    IMG_TARGET_W,
    _fetch_bound_assets,
    _gen_size_for_target,
    _load_project_rows,
    _normalize_direct_abc,
    _now,
    _parse_abc_shots,
    _upscale_to,
    broadcast_gen_progress,
)

# 显存协商三件套（2026-08-31 提升至 common 共享层，视频链路同权接入）：
# 语义与 S7 时期一致——睡眠/唤醒 vLLM + 生成毕卸绘画管线
from .common import (
    sleep_vllm_for_generation as _sleep_vllm_for_vram,
)
from .common import (
    unload_paint_pipeline as _unload_paint_after_gen,
)


def _schedule_debounced_wake(source: str) -> None:
    """生成收尾的去抖唤醒（批3 2026-09-10，方案 §3.4）。

    keyframe 重抽释锁后不再立即唤醒 vLLM——立即唤醒会被紧邻的下一
    任务（绘画/视频队列）功能锁门禁拒绝且无人重试；经协调器去抖窗
    到点且重型生成域全空闲才唤醒。非阻塞（协调器内部起定时线程）。
    """
    from ...services.inference.gpu_budget import (
        get_yield_coordinator,
        heavy_generation_idle,
    )

    get_yield_coordinator().schedule_wake_if_idle(
        source, heavy_generation_idle)

if TYPE_CHECKING:
    from collections.abc import Callable

    from PIL import Image

    from ...services.cloud_provider_service import CloudEndpoint

router = APIRouter()
log = logging.getLogger("omnispace.api.manga.keyframe")



def _ensure_source_mode_column(db: Database) -> None:
    """keyframes 补 source_mode 列（幂等，2026-09-03 方案A：兜底出图标注）。

    方案A 用户裁定：无描述词出图（原文直出兜底）必须在行内/详情可见，
    防止「描述词失败角标 + 兜底成功出图」同行的误导。describe=按描述
    词生成；fallback=原文直出；旧数据空串=未知（不标注、不臆测）。
    """
    cols = {r["name"] for r in db.query("PRAGMA table_info(keyframes)")}
    if "source_mode" not in cols:
        db.sql("ALTER TABLE keyframes ADD COLUMN source_mode TEXT DEFAULT ''")


def _kf_row_to_dict(r: dict) -> dict:
    return {"keyframe_id": r["id"], "row_id": r.get("row_id", ""),
            "project_id": r.get("project_id", ""),
            "version": int(r.get("version", 1)),
            "file_path": r.get("file_path", ""),
            "prompt": r.get("prompt", ""),
            "status": r.get("status", "done"),
            "error": r.get("error", ""),
            "is_current": bool(r.get("is_current", 1)),
            "shot_seeds": r.get("shot_seeds", "[]"),
            "consistency": r.get("consistency", ""),
            "source_mode": r.get("source_mode", ""),
            "created_at": r.get("created_at", 0)}


def _image_prompt_from_abc(desc: str) -> str:
    """A/B/C 结构化描述词 → SDXL 生图提示词（2026-08-25 竞品对齐）。

    取 A 段风格（剥视频负向声明）+ B 段世界观 + C 段首个实质镜头的
    画面内容（跳过 [0.0s-0.1s] 参考图保持段；剥[标题]/运镜/音效，
    时间轴与声音指令对静态图无意义）。解析失败回退全文。
    """
    # A 段：首个 B 段标记行前，剥"A. 全局风格："前缀与视频负向声明
    m = re.search(r"(?m)^B[.、．]", desc)
    a_text = desc[:m.start()].strip() if m else desc.strip()
    a_text = re.sub(r"^A[.、．]\s*全局风格[：:]\s*", "", a_text)
    a_text = a_text.replace("全程无字幕、无背景音乐、只有音效。", "")
    a_text = a_text.strip("，。 ")
    # B 段：B 标记行起至 C 标记/首个镜头行止（标题可退化——2026-08-25
    # 实测 4B 常输出「B. 正文」或「B. 世界状态快照：…」形态）
    m = re.search(
        r"(?m)^B[.、．]\s*(?:高密度世界观构建|世界状态快照|世界观构建)?"
        r"[：:]?\s*(.*?)(?=^C[.、．]|^\[?\d[\d.]*s?\s*[-–~]|\Z)",
        desc, flags=re.S)
    b_text = m.group(1).strip() if m else ""
    # C 段首个实质镜头：跳过首帧保持段，取"画面："至首个"|"
    #（容忍时间码无方括号/无 s、[标题]前置等 4B 实测形态）
    c_text = ""
    for line in desc.splitlines():
        line = line.strip()
        m = re.match(r"^\[[^\[\]]+\]\s*(?=[\[\d])", line)  # 剥前置[标题]
        if m:
            line = line[m.end():]
        if not re.match(r"^\[?\d[\d.]*s?", line) or re.match(r"^\[?0\.0s", line):
            continue
        m = re.search(r"画面[：:]\s*(.*)", line)
        if not m:
            continue
        shot = m.group(1).split("|")[0].strip()
        c_text = re.sub(r"^\[[^\]]*\]\s*", "", shot)  # 剥[标题]
        break
    parts = [p.strip("，。 ") for p in (a_text, b_text, c_text)
             if p.strip("，。 ")]
    return "，".join(parts) if parts else desc


def _grid_image_prompt_from_abc(desc: str, grid: dict) -> str:
    """A/B/C 描述词（含网格标记）→ 分镜网格拼图提示词。

    竞品协议（2026-08-25）：一图多镜拼图，每格 = 对应视频镜头首帧。
    A 段风格 + B 段世界观 + 逐格画面（格序 = 镜头时间序，阅读顺序
    左上→右上→左下→右下）。运镜/音效对静帧无意义，剥除。

    景别显式化（2026-08-26 竞品对齐）：竞品图 5 四格严格按 C 段
    景别递进构图（远景→中景→近景→特写），FLUX 对画面文本中隐含
    的「远景/特写」构图遵循度不足以稳定复现——升格为每格独立的
    构图指令前缀，显式锚定格内镜头距离。
    """
    m = re.search(r"(?m)^B[.、．]", desc)
    a_text = desc[:m.start()].strip() if m else desc.strip()
    a_text = re.sub(r"^A[.、．]\s*全局风格[：:]\s*", "", a_text)
    a_text = a_text.replace("全程无字幕、无背景音乐、只有音效。", "")
    a_text = a_text.strip("，。 ")
    m = re.search(
        r"(?m)^B[.、．]\s*(?:高密度世界观构建|世界状态快照|世界观构建)?"
        r"[：:]?\s*(.*?)(?=^C[.、．]|^\[?\d[\d.]*s?\s*[-–~]|\Z)",
        desc, flags=re.S)
    b_text = m.group(1).strip() if m else ""

    # 景别 → FLUX 构图指令（按镜位递进，2 镜 1×2 / 4 镜 2×2）
    framing = {
        "1x2": ["Wide establishing shot",
                "Medium shot"],
        "2x2": ["Wide establishing shot",
                "Medium shot",
                "Medium close-up",
                "Extreme close-up"],
    }.get(grid.get("layout") or "")

    pos = {"2x2": ["左上格", "右上格", "左下格", "右下格"],
           "1x2": ["左格", "右格"]}.get(grid.get("layout") or "")
    cells: list[str] = []
    for i, shot in enumerate(grid.get("shots") or []):
        if pos and i < len(pos):
            head = pos[i]
        else:
            head = f"第{i + 1}格"
        title = (shot.get("title") or "").strip()
        body = shot.get("text", "").strip()
        if framing and i < len(framing):
            body = f"{framing[i]}，{body}"
        cells.append(f"{head}{title}：{body}")
    layout_zh = {"2x2": "2×2 四格", "1x2": "左右 1×2 两格"}.get(
        grid.get("layout") or "", "")
    parts = [p.strip("，。 ") for p in (a_text, b_text) if p.strip("，。 ")]
    head = "。".join(parts)
    return (f"{head}。分镜故事板拼图：同一画布内{layout_zh}"
            f"（共{len(cells)}格），格间以清晰细线分隔，各格为独立完整"
            "画面，各格构图严格按其景别指令（宽景/中景/近景/特写），"
            "同一角色外貌、服装与场景风格在所有格中保持完全一致，"
            "整体光影色调统一。按阅读顺序各格内容——"
            + "；".join(cells) + "。")


# 镜位 → 单帧构图指令（方案 A：逐镜生成时代码侧保证景别递进，
# 不依赖底座对「故事板多格」的指令遵循——2026-08-26 实测 FLUX.2
# Klein-4B 忽略拼图指令出单帧，故改逐镜生成 + 代码拼图）。
# 指令为完整构图描述而非裸单词（2026-08-26 竞品对照迭代）：裸
# 「Medium close-up」会被 A+B 段约 350 字场景文本稀释，特写镜
# 被世界观句拉回全身街景；显式画幅/填充/景深指令提升遵循度，
# 且面部特写镜天然不可见全身服装 → 跨镜服装抖动一并消解
_SHOT_FRAMING = {
    "1x2": [
        "Wide establishing shot, full-body visible, character small "
        "in frame, expansive environment",
        "Medium shot, character from knees up, environment visible",
    ],
    "2x2": [
        "Wide establishing shot, full-body visible, character small "
        "in frame, expansive environment",
        "Medium shot, character from waist up, surroundings visible",
        "Medium close-up, head and shoulders framing, upper body "
        "fills the frame",
        # ECU 发型锚（2026-08-27 v18 事故）：末格特写下参考图对
        # 发型的引导被构图指令稀释 → 齐刘海/披肩发漂移（v18 右下
        # 格）。显式声明「发型与面部身份与参考角色一致」——把发型
        # 从 char 锚中段提升到构图指令层（最高遵循权重），与参考
        # 条件图双通道锁定。v20 复发（v21 加 identical/realistic
        # 强化：特写下腮红+娃娃脸方差也一并压制）。v27 剩余缺陷：
        # 写实立绘参考的「多视图感」被复制成 3 肖像拼贴——加
        # single face 占满整框指令
        "Extreme close-up, one single face fills the entire frame, "
        "identical hairstyle and facial identity as the reference "
        "character, realistic facial proportions, natural skin "
        "texture with fine pores, no blush makeup, matte skin "
        "finish, shallow depth of field, blurred background",
    ],
}

# 远景/全景镜判定（2026-08-28 v51-v60 标定）：该类镜人脸在画面中
# 占比极小，DINOv2 face 嵌入不稳定——同镜跨版本 face_sim
# 0.311~0.781 漂移（换 seed 重抽 4 次全更差）而 VLM 恒判同一人
# （90-95）。故 face 门禁对远景镜豁免，由 VLM + scene 门禁负责
# （远景 scene 阈值档 0.50，见 _WIDE_SCENE_SIM_THRESHOLD）；无
# 场景基准时仅 VLM 兜底（与既有「无场景基准仅 VLM」语义一致）。
def _is_wide_shot(framing: list | None, idx: int) -> bool:
    if not framing or idx >= len(framing):
        return False
    f = framing[idx].lower()
    return "wide" in f or "establishing" in f


def _is_char_shot(framing: list | None, idx: int) -> bool:
    """已知 framing 且非远景 = 人物主导镜（中景/中近景/特写）。

    人脸检测失败（画风化/裁切/侧脸）时 scene 门禁对该类镜恒低
    （特写带 0.03~0.09、中景带 0.5~0.68，全图嵌入受景别强混淆——
    v63 shot3 事故：face n/a 落 scene 门禁 -0.006 必假阴性，触发
    无谓重抽）→ 仅 VLM 兜底。framing 未知（单帧行/旧数据）维持
    legacy 行为（face n/a 走 scene 门禁）。
    """
    if not framing or idx >= len(framing):
        return False
    return not _is_wide_shot(framing, idx)


# 颜色词（剥除用：C 段正文中道具名词的颜色修饰与资产设定冲突时，
# 颜色细节交给资产锚+参考条件图决定——竞品行为是资产图优先）
_PROP_COLOR = r"(?:浅|深)?[黑白红蓝绿黄棕灰紫橙粉金银青]色?"


def _strip_prop_colors(body: str, props_prompt: str) -> str:
    """C 段正文中道具名词前的颜色词剥除（与资产颜色冲突消解）。

    v12 事故：C 段正文「白色行李箱」（外贴竞品原文）与资产「黑色
    硬壳拉杆行李箱」冲突，文本胜过参考图。名词集合从 props_prompt
    逗号段尾提取（行李箱/笔记本/封面…），仅剥这些名词前的颜色词
    ——场景色（蓝天绿树）与角色服装色（白衬衫）不在此列。
    """
    if not props_prompt or not body:
        return body
    nouns: set[str] = set()
    for seg in re.split(r"[，；,;]", props_prompt):
        seg = seg.strip()
        for n in (seg[-3:], seg[-2:]):
            if len(n) >= 2 and not re.search(_PROP_COLOR, n):
                nouns.add(n)
    if not nouns:
        return body
    pat = re.compile(
        rf"{_PROP_COLOR}(?=({'|'.join(map(re.escape, sorted(nouns)))}))")
    return pat.sub("", body)


# 角色外貌词（剥除用：C 段正文中的发型/上衣描述与 char 锚职责重叠
# 时会与资产设定拔河——v20/v21 发型来回翻转的文本侧根源）
_CHAR_APPEAR_PAT = re.compile(
    r"(?:绑着|扎着|留着|梳着)?(?:高|低)?马尾(?:辫)?的?"
    r"|(?:浅|深)?[黑白红蓝绿黄棕灰紫橙粉金银青]色?"
    r"(?:衬衫|短袖T恤|短袖|T恤)")


def _strip_char_appearance(body: str, char_prompt: str) -> str:
    """C 段正文中角色外貌词剥除（发型/上衣归 char 锚管辖）。

    v22 落地：v20/v21 发型拔河根因是三层——资产图（披肩发）vs 资产
    prompt（高马尾，导入时误从分镜文本抄写）vs C 段正文（高马尾/
    白衬衫）。DB prompt 已对齐资产图后，C 段正文是最后一处冲突源
    （v12 教训：裁决指令单独不可靠，冲突文本必须移除——v13 行李箱
    即靠 _strip_prop_colors 剥「白色」才稳定）。仅发型/上衣模式，
    裤装/表情/动作不剥（叙事细节）。
    """
    if not char_prompt.strip("，。 ") or not body:
        return body
    return _CHAR_APPEAR_PAT.sub("", body)


# 手部泛红子句（剥除用，2026-08-28 v64 shot2 血手）：C 段「因为
# 攥得太紧，她的指尖泛着潮红」这类局部肤色氛围词被 klein 字面放
# 大渲染（指尖潮红 → 整手血红）。手部自然肤色由参考图/模型先验
# 决定，含泛红的手部子句对生图只有染色风险——整句剥除（同句正
# 向信息「攥紧纸张」已由镜头动作句承载）。泛(?!黄) 排除「泛黄」
# （旧纸张常态词，非肤色）；脸颊泛红不剥（词表仅手部名词）。
_HAND_FLUSH_PAT = re.compile(
    r"[^。]*(?:指尖|手指|手心|掌心|双手|手部|指节)[^。]*泛(?!黄)[^。]*。?")


def _strip_hand_flush(body: str) -> str:
    """C 段正文手部泛红子句剥除（无条件执行，不依赖资产锚）。"""
    if not body:
        return body
    return _HAND_FLUSH_PAT.sub("", body)


# ── 风格强化（2026-08-27 v20 竞品对齐 → 同日并入生图路由引擎）──
# 风格判据与强化词块已迁至 gen_router.STYLE_PACKS（8 风格词典，
# 底座亲和 + 后处理档位联动）；本文件经 detect_style/resolve_route
# 消费，_shot_image_prompt_from_abc 注入风格包词块。


def _shot_image_prompt_from_abc(desc: str, grid: dict,
                                shot_index: int, char_prompt: str = "",
                                scene_prompt: str = "",
                                props_prompt: str = "",
                                style_line: str = "",
                                strip_prop_nouns: str = "",
                                char_count: int = 0,
                                text_priority: bool = False) -> str:
    """A/B/C 描述词 → 单镜首帧提示词（方案 A：逐镜生成）。

    A 段风格 + B 段世界观 + 该镜景别指令 + 该镜画面（剥标题/运镜/
    音效/时间码）。各镜共用同一段 A/B 文本与同一参考条件图 → 跨镜
    一致性由共享风格描述与资产锚点保证。

    style_line（2026-08-28 V49-bis 用户裁定）：A 段风格词替换为
    项目当前风格行（_project_style_line，art_style 空→回退网漫）
    ——活源、用户可控、不读资产描述词（V49 协议保持），规避描述
    词 A 段过期风格残留（V47「3D CG」×网漫资产拔河）。V49 首版
    剥空 A 段后被证伪：default 包英文写实质量词（realistic
    physically-based materials...）成为唯一画风信号，四镜全部
    滑向 3D 超写实（雀斑/西化五官/shot4 人脸检测失败）——画风
    主权归参考图的前提是保留一个与项目风格一致的文本锚（C1 实
    测：网漫词+参考图=0.846 双优；V49 实测：剥空=超写实漂移）。

    char_prompt（2026-08-26 竞品对照迭代）：character 一致性锚。
    V49 协议后为中性指令（角色名 + 与设定图严格一致），不含外貌
    词——外貌/服装/画风主权全归参考条件图，仅保留①「同一角色」
    计数指令（防多人误入格）②C 段外貌冲突词剥除开关。

    scene_prompt / props_prompt（2026-08-27 竞品对齐 v12 事故）：
    v12 用户实报「场景跟场景图不一致、道具跟道具图不一致」——
    行描述词（外贴竞品原文）写「白色行李箱/别墅区」，绑定资产是
    「黑色行李箱/欧式庄园」，冲突时 klein-4B 遵循文本（参考条件
    图被文本压制）。竞品行为是资产图优先。修复：scene/prop 资产
    设定同样以短锚注入（「场景/道具（与设定图严格一致）：…」），
    且注入任一资产锚的镜剥除 B 段环境句——环境职责由锚接管，
    从源头消除「文本白色 vs 资产黑色」对冲（B 段角色语境职责已
    由 char 锚接管，剥离不损外貌一致性）。ECU 镜（末格面部特写）
    只保 char 锚，scene/prop 不可见且会稀释面部构图指令。

    char_count（2026-08-28 P0-3 补口）：绑定角色资产数。>1 时外层
    锚用中性「角色锚」措辞，不叠加单角色唯一性禁令——「始终只有
    这一个角色」与 char_prompt 的「共 N 位」同句矛盾（v26 唯一性
    语义由多角色「除这些角色外」禁令承接）。
    """
    m = re.search(r"(?m)^B[.、．]", desc)
    a_text = desc[:m.start()].strip() if m else desc.strip()
    a_text = re.sub(r"^A[.、．]\s*全局风格[：:]\s*", "", a_text)
    a_text = a_text.replace("全程无字幕、无背景音乐、只有音效。", "")
    # V49-bis 画风锚（见 docstring）：项目当前风格行替换 A 段——
    # 剥空版已被 V49 实测证伪（default 包写实质量词主导 → 超写实
    # 漂移），替换版经 C1 实测验证（网漫词+参考图=0.846）
    if style_line and char_prompt:
        a_text = style_line
    # 风格包识别（生图路由引擎 2026-08-27）：A 段风格词 → 词典
    # 命中（photoreal/cg3d/anime/watercolor/oilpaint/inkwash/
    # cyberpunk；未命中走 DEFAULT_PACK——行为与 v22 单判据一致）
    style_pack = detect_style(a_text)
    # v22 风格/饱和度冲突消解（档位化，stylized 不消解）：①「二
    # 次元」与 3D boost 对冲（Q6_K 动画先验 → 腮红/塑料肤；竞品
    # 同文本渲染 3D CG 是因为它弱化二次元信号——我们显式消解）；
    # ②「高饱和度」被 Q6_K 字面执行 → 糖水色（竞品渲染自然饱和，
    # QUALITY boost 电影调色对冲不够，从源头替换）——anime 等
    # stylized 档高饱和是风格特征，保留
    if style_pack.sid == "cg3d":
        a_text = a_text.replace("二次元", "CG")
    if style_pack.post == "realistic":
        a_text = a_text.replace("高饱和度", "自然通透")
    a_text = a_text.strip("，。 ")
    m = re.search(
        r"(?m)^B[.、．]\s*(?:高密度世界观构建|世界状态快照|世界观构建)?"
        r"[：:]?\s*(.*?)(?=^C[.、．]|^\[?\d[\d.]*s?\s*[-–~]|\Z)",
        desc, flags=re.S)
    b_text = m.group(1).strip() if m else ""

    shots = grid.get("shots") or []
    shot = shots[shot_index] if 0 <= shot_index < len(shots) else {}
    framing = _SHOT_FRAMING.get(grid.get("layout") or "")
    frame_cmd = (framing[shot_index]
                 if framing and shot_index < len(framing) else "")
    cp = char_prompt.strip("，。 ")
    # V35 人脸一致性事故（2026-08-27）：ECU 构图指令的写实强制
    # 词（realistic facial proportions / fine pores / no blush
    # makeup / matte skin）与 char 锚「与设定图严格一致」自相矛
    # 盾——V32 教训（冲突词必须物理剥除）当时只处理了 style_pack
    # 词块，此处构图指令层漏网：特写格被推向写实脸（腮红消失+
    # 毛孔质感+暖黄肤），与立绘资产的粉腮红/光滑肤/冷白皮背离。
    # 有 char 锚时剥除写实强制词，外貌管辖权全交资产锚+参考条
    # 件图；identical 链同时扩展 eye color / skin tone（v18 教
    # 训：发型靠构图指令层锁定，瞳色/肤色同理——V34 特写格琥珀
    # 瞳/暖黄肤根因）。无锚（未绑定资产）保持 v21 写实行为
    if cp:
        if frame_cmd.startswith("Extreme close-up"):
            frame_cmd = frame_cmd.replace(
                "identical hairstyle and facial identity as the "
                "reference character",
                "identical hairstyle, facial identity, eye color and "
                "skin tone as the reference character")
            for _w in ("realistic facial proportions, ",
                       "natural skin texture with fine pores, ",
                       "no blush makeup, ",
                       "matte skin finish, "):
                frame_cmd = frame_cmd.replace(_w, "")
        elif frame_cmd.startswith("Medium close-up"):
            # MCU 中近景（V34 格3 脸颊饱满/腮红淡漂移）：面部身份
            # 锚提升到构图指令层（与 ECU 同机制，最高遵循权重）。
            # v64 补 eye color（2026-08-28 shot3 蓝绿眼）：ECU 分支
            # 早已含 eye color 而 MCU 漏了——特写虹膜无文本锚时
            # klein 默认先验漂向西化浅色瞳（参考图灰褐瞳 vs 生成
            # 灰蓝瞳，PuLID 只管结构身份不锁瞳色）
            frame_cmd += (", identical facial identity, hairstyle, "
                          "eye color and skin tone as the reference "
                          "character")
    body = (shot.get("text") or "").strip()
    # C 段正文道具颜色词剥除（与资产颜色冲突消解，见 _strip_prop_colors）
    # P0 补缺口：剥除名词源 = strip_prop_nouns（绑定道具资产名）——
    # V49 后 props_prompt 恒空（不注入 prop 文本锚），颜色冲突剥除
    # 不能随之失效。text_priority（竞品文本优先模式）整体跳过两处
    # 剥除——竞品样张学说为「描述词文本赢过资产图」
    if not text_priority:
        body = _strip_prop_colors(body, strip_prop_nouns or props_prompt)
        # C 段正文角色外貌词剥除（发型/上衣归 char 锚管辖，见
        # _strip_char_appearance——v22 消除发型拔河的最后一处冲突源）
        body = _strip_char_appearance(body, char_prompt)
    # C 段正文手部泛红子句剥除（v64 shot2 血手，见 _strip_hand_flush）
    # ——渲染缺陷修复而非学说裁定，text_priority 下同样生效
    body = _strip_hand_flush(body)
    # 【…】注记/占位符剥除（2026-08-29 R5 验收事故 v82 shot1）：
    # 正文批注（如「【等待根据参考图填充：如银色短发的少女】」）被
    # klein 字面执行成银发、压过角色资产——注记是元信息不是画面内
    # 容，物理剥除（v12 教训：冲突文本必须移除，裁决指令单独不可
    # 靠；seed 重抽救不了 prompt 级污染，v81→v82 复发实证）
    body = re.sub(r"【[^】]*】", "", body)
    parts = [p for p in (frame_cmd, body) if p]
    shot_desc = "，".join(parts)

    # 资产锚（短文本，双通道之一——另一通道是参考条件图）
    anchors = []
    if cp:
        if char_count > 1:
            # P0-3 多角色行（2026-08-28）：char_prompt 已含逐名列举
            # + 总数限定 +「除这些角色外」禁令，外层禁止再叠加单角色
            # 唯一性措辞——曾写死「始终只有这一个角色」，与「共 N
            # 位」同句矛盾，模型择一执行会回退双人镜缺第二人
            anchors.append(f"角色锚（各镜严格一致）：{cp}。")
        else:
            # 唯一性限定（2026-08-27 v26 事故）：写实单人立绘做参考
            # 条件图后，klein 把参考主体「复制」进画面（格3 双人/
            # 格4 三人/格2 双行李箱）——参考 token 的在场感 +「同一
            # 角色」措辞被理解为多实例。显式声明单人单物场景
            anchors.append(
                f"同一角色（各镜严格一致，画面中始终只有这一个角色，"
                f"除该角色外不得出现任何人物）：{cp}。")
    is_ecu = frame_cmd.startswith("Extreme close-up")
    sp = scene_prompt.strip("，。 ")
    pp = props_prompt.strip("，。 ")
    if not is_ecu:
        if sp:
            anchors.append(f"场景（与设定图严格一致）：{sp}。")
        if pp:
            anchors.append(f"道具（与设定图严格一致，仅此一件不得重复）：{pp}。")
    anchor = "".join(anchors)
    if anchor:
        # 冲突裁决指令：C 段叙事正文可能与资产设定冲突（外贴竞品
        # 原文常态），显式声明优先级——竞品行为是资产图优先
        anchor += "画面细节与上述设定冲突时，一律以设定为准"
    # B 段处理：注入任一资产锚或 ECU 镜时剥离（见 docstring），
    # 无锚镜保留 B 段（未绑定资产的行维持既有世界观语境）
    if anchor or is_ecu:
        head = a_text
    else:
        head = "。".join(p.strip("，。 ")
                         for p in (a_text, b_text) if p.strip("，。 "))
    # 风格强化注入（v20 竞品对齐 → 2026-08-27 路由风格包）：头部
    # 之后、资产锚之前——风格指令紧跟 A 段原文形成双重风格锚，
    # 英文关键词对 Qwen3 编码器权重更稳（不被参考图风格压制）。
    # style_pack 由 detect_style(a_text) 识别：风格词块 + 特化质
    # 量块（cg3d/default 与 v22 单判据行为一致；photoreal 景深词
    # 定向补 klein-9B 景深虚化短板；anime/水彩/油画/水墨等走各
    # 自风格块）
    boost = style_pack.style_block
    quality = style_pack.quality_block
    if cp:
        # V32 人脸一致性事故（2026-08-27）：写实块的 "without makeup
        # or blush"（禁腮红妆容，complexion 后无逗号）与 photoreal
        # 词块 "with visible pores"（毛孔）直接对冲二次元向立绘资产
        # （粉腮红+光滑肤是设定一部分）——与 char 锚「与设定图严格
        # 一致」自相矛盾，模型收到冲突信号后倾向写实词（质量块权重
        # 高）。有 char 锚时剥除这两处外貌强制词，boost/quality 双剥
        # （pores 在 photoreal style_block 中），外貌管辖权全交资产锚
        boost = boost.replace(" with visible pores", "")
        quality = quality.replace(" without makeup or blush", "")
        if style_line:
            # V49 扩展（V32 协议延伸）：画风主权移交参考图时，default
            # 包 quality 的写实人脸词（realistic facial proportions/
            # realistic skin texture ...）对网漫/stylized 资产是同款
            # 对冲——V47 特写镜 3D 卡通脸的推手之一。剥除后外貌与
            # 肤质管辖权全交资产参考图
            for _w in ("realistic facial proportions, ",
                       "realistic skin texture with natural "
                       "bare-skin complexion, "):
                quality = quality.replace(_w, "")
    segs = [s for s in (head, boost, quality,
                        anchor, shot_desc) if s]
    return "。".join(segs) + "。"


def _compose_grid_image(frames: list, layout: str) -> Image.Image | None:
    """逐镜首帧列表 → 网格拼图 PIL 图（方案 A：代码拼图）。

    每格为独立 16:9 首帧，按阅读顺序（镜头时间序）排入网格，格间
    以 8px 黑缝分隔（故事板观感）。layout 1x2 → 横排两格；2x2 →
    田字四格。frames 不足格数时缺格补黑。
    """
    from PIL import Image

    cols, rows = {"1x2": (2, 1), "2x2": (2, 2)}.get(layout, (0, 0))
    if not cols or not frames:
        return None
    # 以首帧尺寸为格基准（逐镜生成尺寸一致）
    cw, ch = frames[0].size
    gap = 8
    W = cw * cols + gap * (cols - 1)
    H = ch * rows + gap * (rows - 1)
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    for i in range(cols * rows):
        r, c = divmod(i, cols)
        x = c * (cw + gap)
        y = r * (ch + gap)
        if i < len(frames):
            canvas.paste(frames[i].resize((cw, ch), Image.LANCZOS), (x, y))
    return canvas


def _enhance_frame(img: Image.Image, profile: str = "realistic") -> Image.Image:
    """FLUX 关键帧帧级画质增强（2026-08-27 v20 竞品对齐）。

    profile 档位（2026-08-27 生图路由引擎）：
      realistic —— 写实/3D/科幻向（v22+v29 全管线，默认，行为不变）
      stylized —— 二次元/水彩/油画/水墨向：跳过红晕抑制与皮肤微
        纹理（腮红/平滑肤是这些风格的特征而非瑕疵——v22 时当目
        标是写实才判定为缺陷），饱和度不再压降（风格色彩特征保留）

    v19 视觉对比定位的后处理层修复（GGUF 量化 + 采样的固有瑕疵）。
    v19 实测噪声形态：天空/皮肤梯度中位 2 但 95 分位 47——重尾
    孤立噪点斑而非均匀噪声。管线顺序「先锐化→平坦区混合清洁版」：
    ①清洁源 = 3×3 中值（孤立斑经典滤波，保边缘）+ 高斯 1.0
      （缓变量噪）——只作平坦区替换素材，不整体应用；
    ②平坦掩码取自清洁源梯度（斑点已除，平坦判定不被噪点欺骗），
      强边缘梯度 >15 加 3×3 保护带（羽化不渗入真结构），小半径
      高斯羽化防混合台阶；
    ③先对原图 UnsharpMask（阈值 6 放过残噪只增强真边缘），锐化
      后的边缘落在保护带内原样保留、天空/皮肤落在平坦掩码内被
      清洁版替换——锐化增益与降噪互不污染（后置 Unsharp 会把
      平坦区降噪成果重新放大，实测 5.3→7.3）；
    ④饱和度微降 0.88（v22：糖水→电影感分级，v21 评审蓝天过艳；
      stylized 档不降——艺术风格色彩特征保留）。
    v22 参数修订（v21 评审）：平坦阈值 6.0→4.0 + 清洁高斯 0.6
    （皮肤纹理退出混合，消塑料感）；Unsharp 50%/r1.2（消发丝
    白边光晕）。
    v29 新增（v28 评审特写格蜡像+红晕）：⑤肤色红晕抑制（置首，
    YCbCr 肤色掩码 ∩ R-G 超额区 R 通道收敛 45%）；⑥皮肤微纹理
    （后置，肤色区 σ1.8 颗粒噪声对抗蜡像光滑）。
    任何一步失败原样返回（不阻断生成主链路；留日志痕）。
    """
    from PIL import Image, ImageEnhance, ImageFilter

    try:
        import numpy as np

        rgb = img.convert("RGB")

        # ── 肤色红晕抑制（v29，置首：锐化会强化红晕边缘）──
        # 特写格剩余问题=红晕偏重：klein 特写先验的腮红渲染。定向
        # 处理——YCbCr 肤色掩码 ∩ R-G 差超额区（真红晕），R 通道向
        # 肤色基线收敛 45%。不动非肤色区（红花/建筑无损）
        def _suppress_flush(arr: np.ndarray) -> np.ndarray:
            r = arr[..., 0]
            g = arr[..., 1]
            b = arr[..., 2]
            cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b
            cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b
            skin = ((cb > 77) & (cb < 127)
                    & (cr > 133) & (cr < 173))
            if skin.sum() < 500:
                return arr  # 近无肤色（空镜/远景）不动
            rg = r - g
            base = float(np.median(rg[skin]))
            flush = skin & (rg > base + 8.0)
            if flush.sum() < 100:
                return arr  # 红晕面积过小不处理
            # 收敛 45%：r_new = g + (base + 0.55*(rg-base))
            target = base + 0.55 * (rg - base)
            r2 = r.copy()
            r2[flush] = (g[flush] + target[flush]).clip(0, 255)
            arr2 = arr.copy()
            arr2[..., 0] = r2
            return arr2

        if profile != "stylized":
            rgb = Image.fromarray(
                _suppress_flush(
                    np.asarray(rgb, dtype=np.float32)).clip(0, 255
                                                            ).astype(np.uint8))

        med = rgb.filter(ImageFilter.MedianFilter(3))
        # v22：高斯 0.8→0.6 + 平坦阈值 6.0→4.0——v21 视觉评审仍判
        # 「皮肤塑料感」：皮肤微纹理梯度 4-8 落在旧平坦掩码内被清洁
        # 版整体替换。阈值 4.0 只罩真平坦（天空缓变中位 ~2），皮肤
        # 纹理区退出混合保持原样；0.6 高斯只平滑剩余缓变噪
        clean = med.filter(ImageFilter.GaussianBlur(0.6))
        # 掩码梯度取自清洁源（噪点斑已除）
        g = np.asarray(med.convert("L"), dtype=np.float32)
        gx = np.abs(np.diff(g, axis=1, prepend=g[:, :1]))
        gy = np.abs(np.diff(g, axis=0, prepend=g[:1, :]))
        grad = gx + gy
        flat = (grad < 4.0)
        edge = (grad > 15.0)

        def _dilate3(mask: np.ndarray) -> np.ndarray:
            d = mask.copy()
            d[1:, :] |= mask[:-1, :]
            d[:-1, :] |= mask[1:, :]
            d[:, 1:] |= mask[:, :-1]
            d[:, :-1] |= mask[:, 1:]
            return d

        blend_mask = _dilate3(flat) & ~_dilate3(edge)
        m_img = Image.fromarray((blend_mask * 255).astype(np.uint8))
        m_img = m_img.filter(ImageFilter.GaussianBlur(1.0))
        m = np.asarray(m_img, dtype=np.float32)[..., None] / 255.0
        # 先锐化（真边缘增益），平坦区随后被清洁版替换。
        # v22：percent 65→50 / radius 1.5→1.2——v21 评审「发丝边缘
        # 过度锐化白边感」（光晕），降低增益半径
        sharp = rgb.filter(
            ImageFilter.UnsharpMask(radius=1.2, percent=50, threshold=6))
        a = np.asarray(sharp, dtype=np.float32)
        b = np.asarray(clean, dtype=np.float32)
        out = a * (1.0 - m) + b * m

        # ── 皮肤微纹理（v29，后置：对抗蜡像光滑；stylized 跳过——
        # 平滑肤是二次元/艺术风格特征）──
        # 特写格剩余问题=蜡像感=皮肤高频缺失。在肤色区（YCbCr 掩码
        # 膨胀）叠加极轻灰度噪声（σ1.8，摄影颗粒感级别）恢复皮肤
        # 颗粒。置于平坦混合之后（否则 σ<2 的纹理被清洁版抹掉），
        # 且只落皮肤区——天空/墙面加噪=噪点回归（v19 教训）
        try:
            if profile != "stylized":
                r = out[..., 0]
                g = out[..., 1]
                bl = out[..., 2]
                cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * bl
                cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * bl
                skin = ((cb > 77) & (cb < 127)
                        & (cr > 133) & (cr < 173))
                if skin.sum() >= 500:
                    skin_d = _dilate3(_dilate3(skin))
                    sm = Image.fromarray((skin_d * 255).astype(np.uint8))
                    sm = sm.filter(ImageFilter.GaussianBlur(2.0))
                    smask = (np.asarray(sm, dtype=np.float32) / 255.0)[..., None]
                    rng = np.random.default_rng(12307)
                    noise = rng.normal(0.0, 1.8, out.shape[:2])[..., None]
                    out = out + noise * smask
        except Exception as exc:  # noqa: BLE001
            log.warning("皮肤微纹理注入跳过: %s", exc, exc_info=True)

        # v22：0.93→0.88——v21 评审「蓝天过艳/绿偏荧光」（A 段
        # 「高饱和度」已源头替换为「自然通透」，此处后处理协同降）；
        # stylized 档保持原饱和（风格色彩特征）
        return ImageEnhance.Color(
            Image.fromarray(out.clip(0, 255).astype(np.uint8))
        ).enhance(1.0 if profile == "stylized" else 0.88)
    except Exception as exc:  # noqa: BLE001
        # 留痕：曾因缺 Image 导入被静默吞掉 → 原图直通 Unsharp
        # 反向放大噪声劣化画质
        log.warning("帧画质增强失败（原图直出）: %s", exc, exc_info=True)
    return img


def _load_row_reference(db: Database, row: dict) -> Image.Image | None:
    """行绑定资产 → 参考条件图（多资产合成拼图，2026-08-26 竞品对齐）。

    竞品（yl.man-tui.com）把角色/场景/道具等多张资产图同时注入采样
    作为视觉参考锚点；FLUX.2 Klein 的 img2img image 参数只接单张
    PIL 图（视觉 token 拼入序列）——合成一张横向拼图后，模型同样
    看到全部资产视觉 token，等价多图注入且零引擎改动。

    拼图规则：按 character → scene → prop 序逐资产收集首图（同类
    多资产都收——竞品协议），等比缩放至统一高度（默认 640px），
    白底间隔 24px 横排。无可用资产图返回 None（纯文生图）。
    """
    from PIL import Image

    from ...config import DATA_DIR as _DATA_DIR

    REF_H = 640      # 拼图统一高度（FLUX 参考条件 ≤1MP 内部会缩放）
    GAP = 24         # 图间白缝
    MAX_IMGS = 6     # 防过多资产把参考条件 token 挤爆（4 视图资产=1 张）

    assets = _fetch_bound_assets(db, row.get("asset_ids") or [])
    by_kind = {"character": [], "scene": [], "prop": []}
    for a in assets:
        if a.get("file_path") and a.get("kind") in by_kind:
            by_kind[a["kind"]].append(a)

    imgs: list = []
    for kind in ("character", "scene", "prop"):
        for a in by_kind[kind]:
            if len(imgs) >= MAX_IMGS:
                break
            p = _DATA_DIR / a["file_path"]
            if not p.is_file():
                continue
            try:
                im = Image.open(p).convert("RGB")
            except Exception as exc:  # noqa: BLE001 - 损坏图跳过下一资产
                log.warning("资产参考图加载失败 %s: %s", p, exc, exc_info=True)
                continue
            # V33 人脸事故（2026-08-27）：character 四视图横排
            #（2560×1440 1×4）缩到 640 高后每格面部 token 占比
            # 极小（~10%），klein 写实先验主导中远景格人脸（眼型
            # 细长/削瘦无腮红/写实肤）。四视图判据命中时只取第 1
            # 格（front 全身立绘——服装发型全身信息完整且面部
            # token 占比 ×4）；特写/近景格另有 _load_face_reference
            # closeup 裁格兜底。单图立绘不命中判据保持整图
            if kind == "character":
                w, h = im.size
                if w >= 2000 and 1.6 <= w / h <= 2.0:
                    im = im.crop((0, 0, w // 4, h))
            w, h = im.size
            nw = max(1, round(w * REF_H / h))
            imgs.append(im.resize((nw, REF_H), Image.LANCZOS))
        if len(imgs) >= MAX_IMGS:
            break

    if not imgs:
        return None
    if len(imgs) == 1:
        return imgs[0]
    total_w = sum(im.size[0] for im in imgs) + GAP * (len(imgs) - 1)
    canvas = Image.new("RGB", (total_w, REF_H), (255, 255, 255))
    x = 0
    for im in imgs:
        canvas.paste(im, (x, 0))
        x += im.size[0] + GAP
    return canvas


def _face_ref_for_asset(a: dict) -> Image.Image | None:
    """单角色资产的面部参考图（V32 优先级链，见 _load_face_reference）。

    P0-3 重构：从 _load_face_reference 内联逻辑抽出——多角色协议
    需要逐角色加载，单角色路径语义不变。
    """
    from PIL import Image

    from ...config import DATA_DIR as _DATA_DIR

    fp = (a.get("file_path") or "").strip()
    if not fp:
        return None
    base = (_DATA_DIR / fp).parent
    for face_p in (base / "character_face.png",
                   base / "portrait_views" / "closeup.png"):
        if not face_p.is_file():
            continue
        try:
            im = Image.open(face_p).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            log.warning("面部参考图加载失败 %s: %s", face_p, exc, exc_info=True)
            im = None
        if im is not None:
            w, h = im.size
            return im.resize((max(1, round(w * 640 / h)), 640),
                             Image.LANCZOS)
    # ③ portrait 四视图横排裁右格（closeup 视图）
    portrait_p = base / "portrait.png"
    if portrait_p.is_file():
        try:
            im = Image.open(portrait_p).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            log.warning("portrait 加载失败 %s: %s", portrait_p, exc, exc_info=True)
            im = None
        if im is not None:
            w, h = im.size
            if w >= 2000 and 1.6 <= w / h <= 2.0:
                crop = im.crop((w * 3 // 4, 0, w, h))
                cw, ch = crop.size
                return crop.resize(
                    (max(1, round(cw * 640 / ch)), 640),
                    Image.LANCZOS)
    return None


def _load_face_reference(db: Database, row: dict) -> Image.Image | None:
    """特写镜专用面部参考图（首位绑定角色的）。

    来源优先级（2026-08-27 V32 裁定）：

    ① character_face.png（写实向工作流 qwen-image 产物——写实资产）
    ② portrait_views/closeup.png（四视图独立落盘——onepass/legacy 资产）
    ③ portrait.png 四视图横排裁右格（上传型资产，2560×1440 1×4，
       右端 closeup 是上半身特写）——资产自产图风格天然一致，
       V32 事故根因即特写格无面部参考回退全身拼图，klein 写实
       先验 + 质量块写实词把脸推向真人化（削瘦+毛孔+无腮红），
       与二次元向立绘资产（饱满苹果肌+粉腮红+光滑肤）背离

    判据 ③：宽 ≥ 2000 且 1.6 ≤ 宽/高 ≤ 2.0（四视图 2560×1440
    标准 16:9 整图；单图立绘 1024²/832×1216 等不命中防误裁）。
    均无返回 None（特写镜回退全身拼图参考）。
    """
    for a in _fetch_bound_assets(db, row.get("asset_ids") or []):
        if a.get("kind") != "character":
            continue
        im = _face_ref_for_asset(a)
        if im is not None:
            return im
    return None


def _load_face_references(db: Database, row: dict) -> list:
    """逐角色面部参考图列表（P0-3 多角色协议）。

    与绑定 character 资产序对齐（含缺失位 None）——多角色的
    ArcFace 身份基准、守卫逐角色匹配均以此为准。
    """
    return [_face_ref_for_asset(a)
            for a in _fetch_bound_assets(db, row.get("asset_ids") or [])
            if a.get("kind") == "character"]


# ── 多资产参考拆分（2026-08-28 P1「参考图拆分加权」）─────────────
# 参考通道从单张 640px 横排拼图升级为逐资产独立图：FLUX.2 Klein
# 原生支持多参考（diffusers image 列表 / ComfyUI ReferenceLatent
# 链式串联，官方上限 8 张），每图独立满幅条件 token——角色正脸
# 不再被拼图稀释（v12 诊断根因：拼图中角色正脸 token 占比 ~1-2%）。
# 8 张上限 = 用户口径 + FLUX.2 多参考官方上限。
_MAX_REF_IMAGES = 8


def _load_asset_references(db: Database, row: dict) -> list:
    """行绑定资产 → 逐资产独立参考图列表（P1 多参考拆分）。

    返回 [{"kind", "name", "image"}]（character → scene → prop 序，
    与 _load_row_reference 拼图序一致；character 沿用 V33 四视图
    裁第 1 格判据）。图片缺失/损坏的资产跳过（诚实降级，不占名额）。
    """
    from PIL import Image

    from ...config import DATA_DIR as _DATA_DIR

    assets = _fetch_bound_assets(db, row.get("asset_ids") or [])
    by_kind = {"character": [], "scene": [], "prop": []}
    for a in assets:
        if a.get("file_path") and a.get("kind") in by_kind:
            by_kind[a["kind"]].append(a)

    refs: list = []
    for kind in ("character", "scene", "prop"):
        for a in by_kind[kind]:
            if len(refs) >= _MAX_REF_IMAGES:
                break
            p = _DATA_DIR / a["file_path"]
            if not p.is_file():
                continue
            try:
                im = Image.open(p).convert("RGB")
            except Exception as exc:  # noqa: BLE001 - 损坏图跳过下一资产
                log.warning("资产参考图加载失败 %s: %s", p, exc, exc_info=True)
                continue
            # V33 人脸事故（2026-08-27）：四视图横排（2560×1440 1×4）
            # 缩幅后每格面部 token 占比极小——命中判据只取第 1 格
            # （front 全身立绘，面部 token 占比 ×4）
            if kind == "character":
                w, h = im.size
                if w >= 2000 and 1.6 <= w / h <= 2.0:
                    im = im.crop((0, 0, w // 4, h))
            refs.append({"kind": kind,
                         "name": (a.get("name") or "").strip(),
                         "image": im})
            # D-1（2026-09-10）：角色全身参考图自动附加——同目录
            # fullbody.png 存在时作为额外 character 参考进
            # ReferenceLatent（portrait 只锚脸，全身图锚身体/服装；
            # PuLID 通道不受影响——继续吃 portrait 的 face 锁）
            if kind == "character":
                fb = p.parent / "fullbody.png"
                if fb.is_file() and len(refs) < _MAX_REF_IMAGES:
                    try:
                        fb_im = Image.open(fb).convert("RGB")
                        refs.append({"kind": "character",
                                     "name": (a.get("name") or "").strip(),
                                     "image": fb_im})
                        log.info("D-1 全身参考附加: %s (%s)",
                                 fb.name, a.get("name", "?"))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("全身参考加载失败 %s: %s", fb, exc, exc_info=True)
                # D-LoRA（2026-09-10）：角色 LoRA 自动附加——同目录
                # lora.safetensors 存在时登记为 "lora" 条目（不进
                # ReferenceLatent），由 _gen_one 挂载为工作流 LoRA
                # 并切 klein-4b 底座（身份权重级硬锁）
                lora_p = p.parent / "lora.safetensors"
                if lora_p.is_file():
                    refs.append({"kind": "lora",
                                 "name": (a.get("name") or "").strip(),
                                 "path": str(lora_p)})
                    log.info("D-LoRA 角色 LoRA 检测: %s (%s)",
                             lora_p.name, a.get("name", "?"))
        if len(refs) >= _MAX_REF_IMAGES:
            break
    return refs


def _select_shot_references(refs: list, shot_text: str,
                            face_imgs: list | None = None,
                            chain_frame: Image.Image | None = None) -> list:
    """逐镜参考选择（P1「结合 ABC 分镜描述词」+ 8 张上限）。

    排序即加权——FLUX.2 多参考无逐图权重语义，位置序 = 重要性序，
    超 _MAX_REF_IMAGES 从尾部淘汰（道具最后保）：
      ① 链式参考帧（v58/v61/v62 标定：仅自过门禁复用帧起链，整帧
         替换语义——链帧在场时独占参考位，资产图不并列混注）
      ② 特写镜面部参考（v29：ECU/MCU 面部 token 主导）
      ③ 资产图 character → scene → prop，同类内「名字出现在本镜
         C 段正文」者优先（ABC 结合：未点名资产靠后，仅超帽时淘汰）

    Returns:
        [{"kind", "name", "image"}]——kind ∈ chain/face/character/
        scene/prop；_gen_one 据此做 PuLID 通道拆分（身份图不进
        ReferenceLatent）。
    """
    text = (shot_text or "").strip()
    picked: list = []
    if chain_frame is not None:
        # 链帧整帧替换（v58/v61/v62 校准语义：链源=过门禁帧单独注入
        # 即全版本最高 0.888；资产图并列属未标定组合，不混注）。
        # D-LoRA：链帧在场时 LoRA 条目仍保留（身份硬锁与链帧正交）。
        lora_meta = [r for r in refs if r.get("kind") == "lora"]
        chain_pick = [{"kind": "chain", "name": "", "image": chain_frame}]
        return chain_pick + lora_meta
    for im in (face_imgs or []):
        if im is not None:
            picked.append({"kind": "face", "name": "", "image": im})
    tiered: dict = {}
    for r in refs:
        tier = 0 if (r.get("name") and r["name"] in text) else 1
        tiered.setdefault((r["kind"], tier), []).append(r)
    for key in (("lora", 0), ("lora", 1),
                ("character", 0), ("character", 1),
                ("scene", 0), ("scene", 1),
                ("prop", 0), ("prop", 1)):
        picked.extend(tiered.get(key, []))
    return picked[:_MAX_REF_IMAGES]


def _char_anchor_name(asset: dict) -> str:
    """角色锚短语：名字（2026-09-02 结构化属性整体移除后仅名字）。

    跨镜一致性由「名字 + 参考图锚」承担——名字作为身份标识引导
    生图模型匹配参考图中该角色的既定外观。
    """
    return (asset.get("name") or "角色").strip()


def _shot_char_protocol(char_assets: list,
                        shot_text: str) -> tuple[str, list]:
    """逐镜角色锚协议（P0-3 × ABC 结合）：只锚「本镜点名」的角色。

    多角色行内某镜正文未点名的角色不进锚也不进参考位次前列（避免
    被主动拉入画面）；全员未点名（代词/省略）回退全员锚（保底身份
    信号）。返回 (char_prompt, 命中角色列表)——单角色保持 v26 唯一
    性措辞语义，多角色逐名列举；外层锚措辞由
    _shot_image_prompt_from_abc 按 char_count 分支。
    """
    text = (shot_text or "").strip()
    named = [a for a in char_assets
             if (a.get("name") or "").strip()
             and a["name"].strip() in text]
    chosen = named or list(char_assets)
    anchors = [_char_anchor_name(a) for a in chosen]
    if not anchors:
        return "", []
    if len(anchors) == 1:
        return f"{anchors[0]}，外貌、服装与角色设定图严格一致", chosen
    return (f"画面中的角色共{len(anchors)}位：{'、'.join(anchors)}，"
            "各自外貌、服装与角色设定图严格一致；"
            "除这些角色外不得出现任何其他人物", chosen)


def _dual_pulid_faces(char_count: int,
                      face_refs: list) -> tuple | None:
    """chars=2 双身份锁方案（UAT 2026-09-10 Step B 转正）。

    双角色面部参考齐备 → 返回 (faceA, faceB)（序=绑定序；face_refs 与
    char_assets 同源同序，P0-3 协议保证映射，错锁防护的依据）。
    任一缺失/数量不符 → None（回落多参考软锁，诚实降级）。
    """
    if (char_count == 2 and len(face_refs) >= 2
            and face_refs[0] is not None and face_refs[1] is not None):
        return (face_refs[0], face_refs[1])
    return None


def _comfy_ref_entries(shot_refs: list, pulid_img: bool,
                       reflat_hint: str,
                       multi_anchor: bool) -> tuple[list, list]:
    """ComfyUI ReferenceLatent 参考装配（UAT 2026-09-10 多角色扩展）。

    返回 (comfy_refs, comfy_mps)。规则：
    - PuLID 激活 + reflat=off：全裸（身份归 PuLID attention，R2 裁定）；
    - PuLID 激活（其余）：face 恒不入 latent（R5 面部复制病理：身份
      归 PuLID 后 face 图再入 latent 致 2 脸复制/无人脸），character/
      scene/prop 保留（角色 1.0MP=一致性锚定主力，D-2 逐图预算）；
    - 无 PuLID + 多角色锚定（chars=2）：face 同样不入（单人 ECU 面部
      图在多人镜头诱发复制病理），双角色立绘锚全保留进 latent 软锁；
    - 无 PuLID 单角色：旧行为全量（face 入 latent 作唯一一致性通道）。
    """
    if pulid_img and reflat_hint == "off":
        return [], []
    drop_face = pulid_img or multi_anchor
    entries = [e for e in shot_refs
               if e.get("kind") != "lora"
               and not (drop_face and e.get("kind") == "face")]
    refs = [e["image"] for e in entries]
    # D-2（2026-09-10）：逐图分辨率预算——角色参考 1.0MP（一致性
    # 锚定主力，0.35MP 时全身图几乎无锚定力）；场景/道具走引擎均匀分档
    mps = [1.0 if e.get("kind") == "character" else None for e in entries]
    return refs, mps


def _resolve_base_seed(db: Database, row_id: str, seed: int | None,
                       force_new_seed: bool) -> tuple[int, str]:
    """V37 seed 解析（方案A 固定兜底）：显式 seed > 沿用最近版本已存
    seed > 随机；force_new_seed 跳过沿用（用户主动重抽 / VLM 低分重试）。
    逐镜派生 base_seed+shot_index 在生成循环内完成。

    Returns:
        (base_seed, source)：source ∈ explicit / reused:vN / random
    """
    if seed is not None and seed >= 0:
        return int(seed), "explicit"
    if not force_new_seed:
        prev = db.query_one(
            "SELECT version, shot_seeds FROM keyframes"
            " WHERE row_id=? AND status='done'"
            " ORDER BY version DESC LIMIT 1", (row_id,))
        try:
            stored = json.loads((prev or {}).get("shot_seeds") or "[]")
        except (ValueError, TypeError):
            stored = []
        if stored:
            return int(stored[0]), \
                f"reused:v{int((prev or {}).get('version') or 0)}"
    return random.SystemRandom().randint(0, 2**31 - 1), "random"


def _generate_keyframe_sync(row_id: str, project_id: str,
                            prompt: str, width: int, height: int,
                            seed: int | None = None,
                            force_new_seed: bool = False,
                            engine_backend: str = "auto",
                            only_shots: list[int] | None = None,
                            src_version: int = 0,
                            text_priority: bool = False,
                            cloud_endpoint: CloudEndpoint | None = None,
                            check_cancel: Callable[[], None] | None = None) -> dict:
    """同步生成一个关键帧版本（竞品协议对齐，2026-08-25）。

    底座 FLUX.2 Klein-9B（2026-08-27 用户裁定切换，quanto float8
    9.7GB 常驻 GPU + Qwen3-8B 编码器 RAM leaf_level，质量优于 4B
    ——方案 C 评估时保留的单帧高质量底座；同族 distilled，guidance
    4.0 语义与 4B 一致）：①Qwen3 中文文本编码器（根治 SDXL 中文
    图文不符——实测中文 A/B/C 喂 SDXL 出中式古亭空镜）②image 参数
    即参考条件（绑定资产图进采样，外貌一致性锚定）。含网格标记的
    A/B/C → 方案 A 逐镜生成 16:9 首帧 + 代码拼图（2026-08-26 实测
    FLUX 忽略「故事板多格」指令出单帧，构图改由代码保证）；否则
    单帧。9B 不可用（缺权重/显存不足）逐级降级 4B → SDXL + 中译英
    （诚实降级）。落盘 → keyframes 表登记。

    V37 seed 固定兜底（2026-08-27 v36 身份漂移事故）：v35/v36 prompt
    与资产完全一致仅 seed 随机不同，结果一个相符一个漂移——抽卡
    方差是身份漂移唯一变量。机制：①逐镜实际 seed 落库 keyframes.
    shot_seeds，未显式指定 seed 且非 force_new_seed 时沿用最近版本
    已存 base_seed 复现；②生成后 VLM 一致性评分，低分镜换 seed
    重抽 1 次（仍低分保留并标注）。seed 解析优先级：请求显式 seed
    > 沿用最近版本 > 随机。

    only_shots（P2-B 2026-08-28 V48 事故——逐镜重抽）：仅重生成指
    定镜号（1-based），其余镜复用 src_version 已落盘首帧（帧文件直
    接拷贝、seed 沿用旧记录）。V48 整版重抽全换 seed 后好镜反而劣
    化（shot3 0.717→0.645），方差引入无增益；网格行破门禁只重抽
    失败镜，好镜零风险保留。单帧行忽略该参数（整图语义）。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法生成关键帧")
    row = db.query_one(
        f"SELECT {_SB_ROW_COLS} FROM storyboard_rows WHERE id=?", (row_id,))
    if row is None:
        raise ApiError(40005, "分镜行不存在", detail={"row_id": row_id})
    if not prompt:
        prompt = (row.get("description") or row.get("original_dialogue")
                  or "").strip()
    if not prompt:
        raise ApiError(40008, "分镜行无画面描述且未提供 prompt",
                       detail={"row_id": row_id})
    # 方案A（2026-09-03 用户裁定）：如实记录画面来源——行无描述词即
    # 原文直出兜底，前端据此打「原文直出」角标（不臆测、旧数据留空）
    used_fallback = not (row.get("description") or "").strip()
    # 直传/外贴描述词归一化（流式→行结构化 + 网格标记注入，幂等）：
    # 归一化结果同时作为关键帧记录 prompt——视频侧以记录为准的网格
    # 判据才能与逐镜落盘产物对齐（协议：标记是唯一判据）
    prompt = _normalize_direct_abc(prompt)
    engine = get_paint_engine()
    # 卡片↔包显式绑定（2026-08-31）：项目所选卡片的风格包随生成传入
    # 路由——大族卡（韩漫/3D/水墨…）确定切换底座/后处理，自定义包
    # （导入 JSON）优先；无绑定回落正则嗅探（行为与既往一致）
    from .comic_asset import _project_style_pack
    style_pack_id, style_pack_def = _project_style_pack(db, project_id)
    # 底座选择（生图路由引擎，2026-08-27）：风格画像 → 底座×风格
    # 包组合自动切换。漫剧关键帧锁 klein 家族（family="flux"——资
    # 产一致性协议闭环验证于 klein 参考条件机制，qwen 写实底座换
    # 族不保跨镜一致；photoreal 风格由风格词块补景深）。路由首选
    # 不可用依 base_chain 降级（cg3d/default: 9B→4B；anime: 4B→
    # 9B——klein 二次元先验即亲和），全链失败走 SDXL+翻译兜底
    # （诚实降级）。路由决策/切换已写系统日志（gen_route_switch）
    route = resolve_route("manga-paint", prompt, family="flux",
                          trace_id=row_id, pack_id=style_pack_id,
                          pack_def=style_pack_def)
    # 后处理档位资产仲裁（2026-08-27 V32 人脸事故）：A 段风格词判
    # 的是画面风格（cg3d），但角色资产可能由 klein-4b（二次元先验）
    # 生成——立绘的粉腮红/光滑肤/饱满苹果肌是设定的一部分，
    # realistic 档后处理（红晕抑制+皮肤微纹理毛孔+饱和度压降）会
    # 反向消解这些设定特征（V32 脸被抹成削瘦写实脸）。仲裁规则：
    # character 资产 meta.model 含 klein-4b → 强制 stylized 档；
    # qwen-image（写实源）→ 保持 route 档；无 meta → route 档
    # 绑定资产一次取齐（P0-3）：路由仲裁 / 角色锚 / PuLID 选路共用
    bound_assets = _fetch_bound_assets(db, row.get("asset_ids") or [])
    char_assets = [a for a in bound_assets if a.get("kind") == "character"]
    face_refs = _load_face_references(db, row)
    # 双身份锁方案（UAT 2026-09-10 Step B 转正）：chars=2 且双 face
    # 齐备 → (faceA, faceB)；否则 None（回落多参考软锁）。face_refs 与
    # char_assets 同源同序（P0-3），映射错位风险已在协议层封死
    dual_faces = _dual_pulid_faces(len(char_assets), face_refs)
    # 后处理档位资产仲裁（2026-08-27 V32 人脸事故）：A 段风格词判
    # 的是画面风格（cg3d），但角色资产可能由 klein-4b（二次元先验）
    # 生成——立绘的粉腮红/光滑肤/饱满苹果肌是设定的一部分，
    # realistic 档后处理（红晕抑制+皮肤微纹理毛孔+饱和度压降）会
    # 反向消解这些设定特征（V32 脸被抹成削瘦写实脸）。仲裁规则：
    # character 资产 meta.model 含 klein-4b → 强制 stylized 档；
    # qwen-image（写实源）→ 保持 route 档；无 meta → route 档
    post_profile = route.style_pack.post
    for a in char_assets:
        gen_model = str((a.get("meta") or {}).get("model") or "")
        if "klein-4b" in gen_model:
            post_profile = "stylized"
            log.info("资产画风仲裁: character 资产由 klein-4b 生成"
                     "（二次元向），后处理档位 %s → stylized",
                     route.style_pack.post)
        break
    # 云端出图（批2 云端API 2026-09-06）：工位绑定云端连接时本地引擎
    # 选路/装载/互斥让位/降级链全部跳过（本地显卡零占用）；推理在
    # _gen_one 云端分支执行，参考图照常加载（云端图生图锚一致性）。
    if cloud_endpoint is None:
        # 推理后端选路（2026-08-27 双链路对比）：comfy=ComfyUI 子进程
        # 工作流（Klein-9B fp8 硬链接权重，与 H3 共用 8189 实例）；
        # diffusers=本地 paint_engine（默认）。显存互斥：两族权重
        # （~10GB 级）不可同驻——切 comfy 前卸载本地管线，切回
        # diffusers 时 /free ComfyUI 驻留权重（进程保活热启动）
        # P0-1（2026-08-28）：PuLID 身份硬锁修复后恢复 auto comfy 选路。
        # 前次剔除（同日早前裁定）的四条缺陷均已根治：①非确定性 →
        # ComfyUI --deterministic + CUBLAS_WORKSPACE_CONFIG（R2 复测）；
        # ②ReferenceLatent 正/负双侧注入稀释负向锚 → reflatent 模式化
        # （PuLID 激活 auto→off，可选 pos/both）；③链式误差复利 → 链
        # 仅自过门禁复用帧起（既有约束保留）；④fp8 质量差 → 身份由
        # PuLID attention 硬锁承载，参考 latent 不再承担身份职责。
        # auto 走 comfy 的条件（缺一回落 diffusers，诚实降级）：
        # ①未被 OMNI_KEYFRAME_PULID_AUTO=0 关闭 ②comfy 管线就绪
        # ③PuLID 就绪 ④绑定 1~2 个角色资产（UAT 2026-09-10 多角色扩展：
        #   chars=2 双 face 齐备走双 PuLID 硬锁（Step B 转正）；单/缺
        #   face 回落多参考软锁；chars≥3 参考稀释风险维持 diffusers）
        # ⑤该角色面部参考可加载（InsightFace 检测稳定的前提）
        use_comfy = engine_backend == "comfy"
        if engine_backend == "auto":
            use_comfy = (os.environ.get("OMNI_KEYFRAME_PULID_AUTO", "1") != "0"
                         and comfy_paint_available() and pulid_available()
                         and 1 <= len(char_assets) <= 2
                         and any(face_refs))
            # B6+（2026-09-14 S3 实弹发现）：行内角色带 D-LoRA 时 comfy
            # 底座切 klein-4b 单文件——ComfyUI diffusion_models 缺该文件
            # （盘上仅有 diffusers 分件版 models/paint/flux2-klein-4b），
            # UNETLoader 校验必炸。带 LoRA 行回落 diffusers（4b 分件在
            # legacy 栈完整可用），缺口补齐（单文件版下载/转换）后此闸
            # 自动放行。
            if use_comfy and any(
                    ((DATA_DIR / str(a.get("file_path") or "")).parent
                     / "lora.safetensors").is_file()
                    for a in char_assets):
                # 角色 LoRA 在场（comfy 将切 klein-4b 单文件底座）——
                # 仅当 ComfyUI diffusion_models 缺该文件才回落 diffusers
                _4b_unet = (DATA_DIR.parent / "tools" /
                            "ComfyUI_windows_portable" / "ComfyUI" /
                            "models" / "diffusion_models" /
                            "flux-2-klein-4b.safetensors")
                if not _4b_unet.is_file():
                    use_comfy = False
                    log.info("角色 LoRA 在场但 ComfyUI 缺 klein-4b 单文件"
                             "权重——回落 diffusers（诚实降级）")
            route_label = ("comfy+双PuLID" if use_comfy and dual_faces
                           else "comfy+PuLID" if use_comfy and len(char_assets) == 1
                           else "comfy+多参考" if use_comfy else "diffusers")
            log.info("引擎自动选路: %s（P0-1 PuLID 身份硬锁条件：%s%s%s%s%s）",
                     route_label,
                     f"env={'on' if os.environ.get('OMNI_KEYFRAME_PULID_AUTO', '1') != '0' else 'off'}",
                     f" comfy={comfy_paint_available()}",
                     f" pulid={pulid_available()}",
                     f" chars={len(char_assets)}",
                     f" face_ref={bool(any(face_refs))}")
        flux = False
        if use_comfy:
            if not comfy_paint_available():
                raise ApiError("PAINT_ENGINE_NOT_READY",
                               "ComfyUI 绘画管线未就绪（便携版或权重缺失）",
                               suggestion="检查 tools/ComfyUI_windows_portable 与权重目录；"
                                          "或在「模型管理 → AI 绘画」改用本地绘画模型后重试")
            # 冷启动可见性（2026-08-31 用户需求「加载要立刻、也要告知」）：
            # ComfyUI 拉起 ~40s + 首图权重装载，期间按钮进度条明示正在加载
            broadcast_gen_progress("keyframe", row_id, percent=1,
                                   label="ComfyUI 绘图引擎启动中（约 40-60s）")
            engine.unload_model()  # 本地管线让位（幂等，未载时快速返回）
            flux = True  # comfy 工作流锁 klein-9b fp8（family 协议一致）
        else:
            comfy = get_comfy_paint_engine()
            if comfy.is_alive():
                comfy.unload()  # 反向互斥：ComfyUI 驻留权重让位
        if not use_comfy:
            # 冷启动可见性：diffusers 权重装载 0.5-2 分钟，进度条明示
            broadcast_gen_progress("keyframe", row_id, percent=1,
                                   label="绘画模型加载中（冷启动约 0.5-2 分钟，完成后自动出图）")
            for mid in route.base_chain:
                if "klein" not in mid:
                    break  # sdxl 兜底位——走既有降级分支
                if engine.ensure_loaded(mid):
                    flux = True
                    break
                log.warning("路由底座 %s 加载失败，依链降级: %s",
                            mid, engine.get_status().get("last_error") or "")
        if not flux:
            # D-LoRA 救援点（2026-09-16 根因定位后重修·断点④真身）：
            # 路由链首位常是 flux2-klein-9b——其 diffusers 目录自 09-10
            # bf16 分片删除后即为幻影候选（transformer/ 只剩骨架索引，
            # _flux_model_dir_ready 不认 transformer-gguf），而风格包偏
            # 好序 klein 族常只有 9b → 白名单交集滤掉 4b → 默认提权要求
            # default∈偏好序（4b 不在）→ 兜底返回不可用原序——四层叠加
            # 使链滑向 SDXL，LoRA 行既无底座也无参考（refs 仅 flux 态
            # 加载）。带 LoRA 行在此显式装载 4b（在盘/白名单内/用户默
            # 认），使 flux 分支与 attach 挂载可达；无 LoRA 行维持原降级。
            _row_has_lora = any(
                ((DATA_DIR / str(a.get("file_path") or "")).parent
                 / "lora.safetensors").is_file()
                for a in char_assets)
            if _row_has_lora:
                if engine.ensure_loaded("flux2-klein-4b"):
                    flux = True
                    log.info("D-LoRA 救援：路由链无可载 klein，行带角色 LoRA "
                             "→ 显式装载 flux2-klein-4b（避开 SDXL 无锚降级）")
                else:
                    log.warning("D-LoRA 救援失败：flux2-klein-4b 装载不成"
                                "（%s），本镜降级 SDXL+翻译（LoRA 失效）",
                                engine.get_status().get("last_error") or "未知")
        if not flux:
            # 降级链：FLUX.2 缺失/加载失败 → SDXL（中文走翻译兜底）
            log.warning("klein 家族加载失败，关键帧降级 SDXL+翻译")
            if not engine.ensure_loaded(None):
                status = engine.get_status()
                raise ApiError(
                    "PAINT_ENGINE_NOT_READY",
                    status.get("last_error") or "绘画模型未就绪",
                    suggestion="已在本次点击时自动尝试加载但失败。可到「模型管理 → "
                               "AI 绘画」查看模型状态并手动加载（如 flux2-klein-4b），"
                               "或稍后重试；确认 models/paint 下模型权重完整")

    # 新版本号 = 该分行当前最大版本 + 1
    vrow = db.query_one(
        "SELECT MAX(version) AS mv FROM keyframes WHERE row_id=?", (row_id,))
    version = int(vrow["mv"] or 0) + 1 if vrow else 1

    # V37 seed 解析（方案A 固定兜底，见 _resolve_base_seed docstring）
    base_seed, seed_src = _resolve_base_seed(db, row_id, seed, force_new_seed)
    log.info("关键帧 seed: row=%s base=%d src=%s", row_id, base_seed, seed_src)

    grid = _parse_abc_shots(prompt)
    is_grid = bool(grid.get("layout")) and len(grid.get("shots") or []) >= 2
    # P1 多参考拆分（2026-08-28）：逐资产独立参考图（含 kind/name
    # 元数据）取代单张拼图作生成参考；_load_row_reference 拼图保留
    # 给一致性评分/VLM 评审上下文（_scoring_context 等）
    asset_refs = _load_asset_references(db, row) \
        if (flux or cloud_endpoint is not None) else []
    # 特写镜面部参考（v29 蜡像感攻关）：存在则 ECU/MCU 镜换用，
    # 面部 token 占比从 ~10% 提到满图——klein 特写先验（蜡像/
    # 红晕）被写实面部引导压制。P0-3：逐角色列表已预载，此处取
    # 首个可用（单角色语义不变）；PuLID 仅单角色行注入（多角色
    # 单 ID 通道会错锁）。云端模式同样取用（进参考图列表作锚）。
    face_ref = (next((im for im in face_refs if im is not None), None)
                if (flux or cloud_endpoint is not None) else None)
    pulid_ok = len(char_assets) <= 1
    out_dir = _KEYFRAME_DIR / row_id
    out_dir.mkdir(parents=True, exist_ok=True)

    def _gen_one(gen_prompt: str, out_w: int, out_h: int,
                 refs: list | None = None,
                 on_step: Callable[[int], None] | None = None,
                 seed: int = -1, reflat_hint: str = "",
                 char_negative: str = "") -> tuple[Image.Image, int]:
        """单张首帧生成（FLUX 原生分辨率直出 / SDXL 半分辨率+上采样）。

        on_step(percent) 采样步级回调 → WS 实时进度条（引擎原生
        progress_cb 直通，采样期间平滑推进而非只在镜间跳变）。
        FLUX 路径 2026-08-27 改原生直出：此前沿用 SDXL/4B 时代的
        「半分辨率生成 + LANCZOS 2x 上采样」速度档，网格格 1280×720
        实际只以 640×360 采样——细节/锐度被插值抹平，是与竞品
        （原生分辨率直出）的主要质量差距。9B transformer int4
        （~4.6GB GPU 常驻 + vLLM 生成期睡眠让渡）激活空间 ~7GB，
        原生采样无换页；超 _IMG_GEN_MAX 的单帧目标等比收缩
        （保画幅）后再放大。SDXL 降级路径维持半分辨率档（显存
        友好）。

        V37（2026-08-27）seed 固定兜底：seed>=0 时固定采样噪声复现
        已验证结果；返回 (image, actual_seed)——FLUX img2img 的
        image 是参考条件 token（latent 纯噪声起步，strength 不参
        与），固定 seed 只复现噪声、不锁死内容，换资产图后沿用
        仍安全。

        refs（P1 多参考拆分）：逐镜参考条目列表（dict：kind/name/
        image，见 _select_shot_references）——comfy 链按 PuLID 激活
        状态做通道拆分，diffusers 链整列表直传管线（原生多参考）。
        char_negative：逐镜角色锚存在时注入的西化瞳色负面锚
        （_CHAR_NEGATIVE_ANCHOR，P1 起随锚逐镜化）。

        云端分支（批2 云端API 2026-09-06）：cloud_endpoint 非 None 时
        提示词/负面/参考图（含面部参考）直送云端适配器出图，尺寸契约
        由适配器保证；seed 不参与云端采样（服务商不受理），原样回传
        仅作台账记录。
        """
        if cloud_endpoint is not None:
            from ...services.inference.cloud_image_client import generate_image as _cloud_generate
            ref_imgs = [e["image"] for e in (refs or [])
                        if isinstance(e, dict)
                        and e.get("image") is not None]
            if face_ref is not None \
                    and not any(isinstance(e, dict)
                                and e.get("kind") == "face"
                                for e in (refs or [])):
                ref_imgs.append(face_ref)
            if on_step:
                on_step(5)
            img = _cloud_generate(
                cloud_endpoint, gen_prompt,
                width=out_w, height=out_h,
                negative=char_negative or "",
                ref_images=ref_imgs,
                check_cancel=check_cancel,
                on_progress=(lambda p: on_step(p)) if on_step else None,
                slot="keyframe.image")
            if on_step:
                on_step(100)
            return img, seed
        step_cb = (lambda p, _s: on_step(p)) if on_step else None
        if flux:
            scale = min(1.0, _IMG_GEN_MAX / out_w, _IMG_GEN_MAX / out_h)
            gw = max(256, int(out_w * scale)) // 16 * 16
            gh = max(256, int(out_h * scale)) // 16 * 16
            # 步数 28→36（v20 质量档）：GGUF 量化噪声收敛更充分；
            # FLUX 帧接画质增强（保边降噪+电影感分级+锐化补偿）
            # min_steps=28（2026-08-28 V77 事故根修）：质量总督深度
            # 降档 36×0.40=14 曾击穿 klein 质量地板（shot3 棕发/shot4
            # 3D 化），28=v20 裁定的最低可接受步数，降至此仍诚实降级
            params = {"prompt": gen_prompt, "steps": 36, "cfg": 4.0,
                      "width": gw, "height": gh, "seed": seed,
                      "min_steps": 28}
            # 西化瞳色负面锚（v65 蓝绿眼，见 char_negative 注释）：
            # comfy 工作流 neg conditioning 原为空——西化瞳色先验
            # 无排除通道，ReferenceLatent 同样注入 neg 侧
            if char_negative:
                params["negative"] = char_negative
            shot_refs = refs or []
            if use_comfy:
                # ComfyUI 工作流：ReferenceLatent 注入参考条件（非像
                # 素初始化 img2img）；步级进度暂不支持（/history 轮
                # 询粒度），进度条按镜跳变
                comfy = get_comfy_paint_engine()
                # PuLID 身份注入（P1）：恒用 face_ref（满图人脸，
                # InsightFace 检测最稳）经 attention 硬锁身份。
                # P0-1 混合 reflatent 策略（R2 实测裁定，2026-08-28）：
                #   特写/中近景（ref=face_ref）→ off——身份走 PuLID，
                #   面部图不再进 ReferenceLatent（R2: off 0.857 > pos
                #   0.807 > both 0.736 > 无PuLID 0.623，且省一半耗时）；
                #   其余镜（ref=资产拼图）→ pos——场景/服装/画风锚保留
                #   正侧，负侧纯文本（瞳色负面锚不被 latent 稀释，
                #   v64/v65 事故根修）。无 PuLID（多角色行/未就绪）→
                #   auto→both 旧行为（参考图是唯一一致性通道）
                pulid_img = (face_ref if face_ref is not None
                             and pulid_available() and pulid_ok else None)
                # 双 PuLID 转正（Step B，UAT 2026-09-10）：chars=2 双
                # face 齐备 → A/B 双身份 attention 硬锁（强度 0.9/0.8
                # = PoC 验证值），reflatent 强制 both（PoC 同配置：双
                # 立绘锚保留在 latent）。单/缺 face 走软锁不变。
                pulid_img_b: Image.Image | None = None
                if dual_faces and pulid_available():
                    pulid_img = dual_faces[0]
                    pulid_img_b = dual_faces[1]
                    params["pulid_strength_b"] = 0.8
                    reflat_hint = "both"
                params["pulid_strength"] = (
                    _PULID_STRENGTH if pulid_img else 0.0)
                if pulid_img and reflat_hint:
                    params["reflatent"] = reflat_hint
                # P1 多参考拆分（2026-08-28）→ R5 验收事故修订
                #（2026-08-29）：PuLID 激活时 face 条目恒不入
                # ReferenceLatent（身份归 PuLID；R2/ECU 专项实测
                # face 图入 latent 致分降低 + 2 脸复制/无人脸病理），
                # 其余条目（character/scene/prop）保留——验收事故
                # 根因即特写镜「off 全裸参考」：背景/道具/画风无锚，
                # 描述词文本成唯一事实源（v82 shot4 商业街背景、
                # R3 shot3 室内走廊两度复现，跨镜场景断裂）。off 仅
                # 作显式回归口（全关）
                comfy_refs, comfy_mps = _comfy_ref_entries(
                    shot_refs, pulid_img=bool(pulid_img),
                    reflat_hint=reflat_hint,
                    multi_anchor=len(char_assets) == 2)
                # D-LoRA（2026-09-10）：角色 LoRA 在场 → 权重级身份硬锁，
                # 底座切 klein-4b + 挂载工作流 LoRA；PuLID 置零（4B 上
                # 未验证且身份已由 LoRA 承担）。LoRA 文件挂入 ComfyUI
                # loras 目录（同卷硬链接优先）。
                lora_entries = [e for e in shot_refs if e.get("kind") == "lora"]
                if lora_entries:
                    import hashlib
                    import os

                    loras_dir = (ROOT_DIR / "tools" / "ComfyUI_windows_portable"
                                 / "ComfyUI" / "models" / "loras")
                    loras_dir.mkdir(parents=True, exist_ok=True)
                    first = lora_entries[0]
                    lp = Path(first["path"])
                    tag = hashlib.md5(str(lp).encode()).hexdigest()[:8]
                    dst = loras_dir / f"char_{tag}.safetensors"
                    if not dst.is_file():
                        try:
                            os.link(lp, dst)
                        except OSError:  # noqa: PERF203 - 跨卷退回复制
                            import shutil
                            shutil.copy(lp, dst)
                    params["lora_name"] = dst.name
                    params["lora_scale"] = 1.0
                    params["lora_base"] = "4b"
                    if pulid_img or pulid_img_b:
                        pulid_img = None
                        pulid_img_b = None
                        params["pulid_strength"] = 0.0
                        params["pulid_strength_b"] = 0.0
                    log.info("D-LoRA 挂载: %s（底座切 klein-4b，PuLID 关）",
                             dst.name)
                if comfy_refs:
                    result = comfy.img2img(params, comfy_refs,
                                           pulid_image=pulid_img,
                                           ref_megapixels=comfy_mps,
                                           pulid_image_b=pulid_img_b)
                else:
                    result = comfy.generate(params,
                                            pulid_image=pulid_img)
            elif shot_refs:
                # D-LoRA 回落挂载（2026-09-15 三连断根修·断点①）：带
                # LoRA 行回落 diffusers 分支时 LoRA 曾被静默丢弃（只剩
                # 参考图软锁）；attach_lora 自 09-10 写完后生产零调用。
                # 配套：无 LoRA 行主动 detach——防止上一镜挂的 LoRA
                # 泄漏到本镜（底座生命周期内 LoRA 常驻，见
                # paint_engine._reset_lora_state 注释）。lora 条目无
                # "image" 键，参考图列表须过滤（断点③ KeyError 拆除）。
                lora_refs = [e for e in shot_refs
                             if e.get("kind") == "lora"]
                img_refs = [e["image"] for e in shot_refs
                            if e.get("image") is not None]
                if lora_refs:
                    # D-LoRA 底座对齐（2026-09-15 E2E 发现的断点④）：角色
                    # LoRA 按 klein base-4b 训练，而路由链首位 flux2-klein-9b
                    # 的 diffusers 分片已删（transformer/ 仅存骨架索引；GGUF
                    # 单文件在盘但 _flux_model_dir_ready 不认，病历已批0-4
                    # 修正入 manifest）→ 回落链滑到 SDXL 时 LoRA 无底座可挂。
                    # LoRA 行显式装载 4b（幂等，已载秒过；失败则 attach 自会
                    # 按软锁降级）。
                    if not engine.ensure_loaded("flux2-klein-4b"):
                        log.warning("D-LoRA 底座 flux2-klein-4b 装载失败"
                                    "（本镜按无 LoRA 软锁继续）")
                    _lp = Path(str(lora_refs[0].get("path") or ""))
                    if _lp.is_file() and engine.attach_lora(_lp, 1.0):
                        log.info("D-LoRA 回落挂载: %s（底座=diffusers "
                                 "klein-4b，身份硬锁）", _lp.name)
                    else:
                        log.warning("D-LoRA 回落挂载失败（按无 LoRA "
                                    "软锁继续）: %s", _lp)
                else:
                    try:
                        if (engine.lora_status() or {}).get("adapter"):
                            engine.detach_lora()
                            log.info("D-LoRA 已卸载（本镜无角色 LoRA）")
                    except Exception:  # noqa: BLE001 - 状态探测失败不阻断
                        log.debug("_gen_one: 降级忽略", exc_info=True)
                result = engine.img2img(
                    params, img_refs,
                    progress_cb=step_cb)
            else:
                result = engine.generate(params, progress_cb=step_cb)
            # 后处理档位：路由风格包 × 资产画风仲裁（V32 人脸事故）
            img = _enhance_frame(
                _upscale_to(result["images"][0], out_w, out_h),
                profile=post_profile)
            return img, int(result.get("seed", seed))
        else:
            from ...services.inference.prompt_translator import translate_prompt_zh2en
            gw, gh = _gen_size_for_target(out_w, out_h)
            # 翻译引擎进场可能把绘画模型腾挪出显存（显存独木桥；
            # 翻译器文档约定「翻译先于 paint 加载」，但降级链在
            # ensure_loaded 之后才走到这里）：翻译后必须重新确认/
            # 装载绘画引擎，否则紧接着的 generate 面对未就绪直接
            # 报错（2026-08-31 实测：加载成功→翻译卸载→生成失败）
            prompt_en = translate_prompt_zh2en(gen_prompt)
            if not engine.is_ready and not engine.ensure_loaded(None):
                raise RuntimeError(
                    engine.get_status().get("last_error") or "绘画模型未就绪")
            # 负向提示按风格包特化（anime 排 photorealistic 等）
            # min_steps=20：SDXL 降级路径质量下限（24×0.40=9.6→20，
            # 关键帧一致性生产任务不走 12 步全局地板，同 FLUX 路径）
            params = {"prompt": prompt_en,
                      "negative": route.style_pack.negative_hint or _STYLE_NEGATIVE,
                      "steps": 24, "cfg": 7.0,
                      "width": gw, "height": gh, "seed": seed,
                      "min_steps": 20}
            result = engine.generate(params, progress_cb=step_cb)
        return _upscale_to(result["images"][0], out_w, out_h), \
            int(result.get("seed", seed))

    # 方案 A：网格协议 → 逐镜生成各格首帧（每格 16:9，逐镜独立采样
    # + 共享参考条件图锚一致性），代码拼图为展示图；各镜首帧单独落盘
    #（视频侧 I2V 直接取用），拼图作为当前关键帧展示。
    if is_grid and _ABC_MARK in prompt:
        shots = grid["shots"]
        # 资产锚协议（2026-08-28 用户裁定 V49）：分镜图只读资产
        # 「图」，不读资产「描述词」——用户可随时替换资产图片，
        # 替换后描述词与图不符（upload 的 VLM 重写靠用户确认，
        # 非硬保证），外貌文本锚会与参考条件图拔河（V47 同型
        # 事故）。落地：①char 锚 = 中性指令（角色名 + 外貌/服装
        # 与设定图严格一致），不含任何外貌词——外貌/服装主权全归
        # 参考条件图；②scene/prop 不注入文本锚（由 row_ref 拼图
        # 参考图与 B/C 段正文承载）；③A 段风格词替换为项目当前
        # 风格行（style_line，见下）——P2-A 的「美术风格：」行
        # 仲裁废弃（数据源是资产描述词）
        # P0-3 多角色协议 → P1 逐镜点名过滤（2026-08-28）：锚必须
        # 覆盖全部绑定角色且不得写死「只有这一个角色」（原缺陷：
        # 只取第一个角色 + 单角色禁令主动禁止第二人入场）。P1 升级
        # 为逐镜裁定——本镜 C 段正文点名的角色才进锚/参考位次前列，
        # 全员未点名（代词/省略）回退全员锚（见 _shot_char_protocol）
        names = [(a.get("name") or "角色").strip() for a in char_assets]
        if names:
            log.info("资产锚协议: chars=%s（逐镜点名过滤）", names)
        # 道具颜色词剥除源（2026-08-28 P0 补缺口）：V49 把 props_prompt
        # 置空后 _strip_prop_colors 失效，C 段「白色行李箱」×黑色道具
        # 资产的文本冲突复活（v12 同型）。剥除名词改从绑定道具资产
        # 「名」提取（元数据非描述词，V49 协议兼容；「cssc行李箱」
        # 尾 3 字即「行李箱」，与原从描述词逗号段尾提取等价）
        prop_names = "，".join(
            (a.get("name") or "").strip() for a in bound_assets
            if a.get("kind") == "prop" and (a.get("name") or "").strip())
        if prop_names:
            log.info("道具颜色剥除源(资产名): %s", prop_names[:60])
        # V49-bis 画风锚（2026-08-28 V49 超写实漂移事故）：A 段风格
        # 词来源 = 项目当前风格行——活源（用户改作品风格即时生效）、
        # 不读资产描述词（V49 协议保持）、规避描述词 A 段过期残留
        # （V47「3D CG」事故）。V49 首版剥空 A 段被证伪：default 包
        # 英文写实质量词成为唯一画风信号，四镜滑向 3D 超写实（雀斑/
        # 西化五官/shot4 人脸检测失败）；C1 实测网漫词+参考图=0.846
        from .comic_asset import _project_style_line
        if not project_id:
            sb = db.query_one(
                "SELECT project_id FROM storyboards WHERE id=?",
                (row.get("storyboard_id"),))
            project_id = ((sb or {}).get("project_id") or "")
        style_line = _project_style_line(db, project_id)
        if style_pack_id:
            log.info("画风锚(A段)=项目风格行: %s（pack=%s）",
                     style_line[:40], style_pack_id)
        else:
            # 2026-09-02 可解释路由：嗅探路径带上裁决凭据（命中词/
            # 遮蔽关系），排障不再依赖脑内推演词序
            _pk, _why = explain_style(prompt)
            log.info("画风锚(A段)=项目风格行: %s（嗅探→%s，%s）",
                     style_line[:40], _pk.sid, _why)
        # 西化瞳色负面锚 → 模块常量 _CHAR_NEGATIVE_ANCHOR（2026-08-28
        # P1 逐镜化：随逐镜角色锚存在性注入，标定史 v65/v67/v69 见
        # 常量处注释）
        # 每格首帧尺寸：网格总宽按 width 摊，每格 16:9
        cols = 2
        cell_w = max(640, (width // cols) // 8 * 8)
        cell_h = max(360, (cell_w * 9 // 16) // 8 * 8)
        frames = []
        actual_seeds: list[int] = []
        # P2-B 逐镜重抽复用表：only_shots 指定重抽镜，其余镜从
        # src_version 落盘首帧加载（帧/seed 双沿用，见 docstring）
        reuse_frames: dict[int, Any] = {}
        old_seeds: list[int] = []
        if only_shots and src_version > 0:
            from PIL import Image as _PILImage
            for si in range(len(shots)):
                if (si + 1) in only_shots:
                    continue
                p_old = out_dir / f"v{src_version}_shot{si + 1}.png"
                if p_old.is_file():
                    reuse_frames[si] = _PILImage.open(p_old).convert("RGB")
            prev_kf = db.query_one(
                "SELECT shot_seeds FROM keyframes "
                "WHERE row_id=? AND version=?", (row_id, src_version))
            try:
                old_seeds = json.loads(
                    (prev_kf or {}).get("shot_seeds") or "[]")
            except Exception:  # noqa: BLE001 - 损坏数据兜底
                old_seeds = []
            log.info("逐镜重抽: row=%s 重抽镜=%s 复用镜=%s（源v%d）",
                     row_id, only_shots,
                     sorted(si + 1 for si in reuse_frames), src_version)
        # 逐镜参考图选择（P1 多参考拆分，见 _select_shot_references）：
        # ECU/MCU 镜命中角色的面部图前置（v29：面部 token 主导——
        # 红晕/蜡像主因是面部参考占比，中近景一并受益）；其余镜按
        # 角色点名 → 场景 → 道具序注入独立资产图（取代单张拼图）
        framing = _SHOT_FRAMING.get(grid.get("layout") or "")
        n_shots = len(shots)
        # 逐镜进度广播（镜开始跳变 + 采样步级平滑推进）：
        # 全局 percent = (镜序 + 镜内步% ) / 总镜数
        broadcast_gen_progress("keyframe", row_id, current=0, total=n_shots,
                               percent=0, label=f"准备生成 {n_shots} 镜")
        for si in range(n_shots):
            if si in reuse_frames:
                frames.append(reuse_frames[si])
                actual_seeds.append(
                    int(old_seeds[si])
                    if si < len(old_seeds) else base_seed + si)
                continue
            broadcast_gen_progress("keyframe", row_id, current=si + 1,
                                   total=n_shots,
                                   percent=int(si * 100 / n_shots),
                                   label=f"第 {si + 1}/{n_shots} 镜")

            def _map_step(p: int, _si: int = si) -> None:
                broadcast_gen_progress(
                    "keyframe", row_id, current=_si + 1, total=n_shots,
                    percent=int((_si + max(0, min(100, p)) / 100.0)
                                * 100 / n_shots),
                    label=f"第 {_si + 1}/{n_shots} 镜")

            shot_text = (shots[si].get("text") or "").strip()
            char_prompt, chosen = _shot_char_protocol(char_assets, shot_text)
            chosen_idx = [i for i, a in enumerate(char_assets)
                          if any(a is c for c in chosen)]
            sp = _shot_image_prompt_from_abc(prompt, grid, si, char_prompt,
                                             "", "", style_line,
                                             strip_prop_nouns=prop_names,
                                             char_count=len(chosen),
                                             text_priority=text_priority)
            is_close = bool(
                framing and si < len(framing)
                and framing[si].startswith(("Extreme close-up",
                                            "Medium close-up")))
            # ArcReel 链式参考（用户指令② 2026-08-28）：组内（分镜
            # 行内连续剧情段）串行——ReferenceLatent 通道改用上一镜
            # 已定型帧（身份/画风/服装/光环境 latent 直传）；PuLID
            # face_ref 身份通道恒定不变；首镜维持资产参考锚。
            # 修正（v61/v62 事故）：仅从复用帧起链——复用帧来自过
            # 门禁/择优版本（已校验），链源可靠（v58 实证：shot4 锚
            # 复用 shot3 → face 0.888 全版本最高）；整版重抽时上一镜
            # 是未校验新帧，逐镜链传会误差复利（v61/v62 shot4 face
            # 0.470→0.386 塌方），回退资产参考直连（v55 基线行为）。
            # _CHAIN_REF=False 一行关闭全链（回归口）
            chain_frame = (frames[-1]
                           if (_CHAIN_REF and use_comfy and frames
                               and (si - 1) in reuse_frames) else None)
            # 特写镜面部参考（v29 蜡像感攻关）：命中角色的面部图前
            # 置（面部 token 主导压制 klein 特写写实先验）；其余镜
            # 不注入（全身立绘已含面部信号）
            face_imgs = ([face_refs[i] for i in chosen_idx
                          if i < len(face_refs) and face_refs[i]]
                         if is_close else [])
            shot_refs = _select_shot_references(
                asset_refs, shot_text, face_imgs=face_imgs,
                chain_frame=chain_frame)
            # 逐镜派生 seed（base+镜序）：跨镜共享分量保一致性基底，
            # 镜间差异保画面多样性；重生成沿用同组 seed 即复现。
            # reflat_hint（P0-1 混合策略 → R5 验收修订 2026-08-29）：
            # 恒 pos——特写镜 face 条目在 _gen_one 内被剥除（身份走
            # PuLID），scene/prop 条目保留注入正侧（v82 验收事故：
            # 特写 off 全裸参考致跨镜场景断裂）；_gen_one 内仅在
            # PuLID 实际生效时应用（多角色行无 PuLID → auto→both）
            img, actual = _gen_one(sp, cell_w, cell_h, refs=shot_refs,
                                   on_step=_map_step, seed=base_seed + si,
                                   reflat_hint="pos",
                                   char_negative=(_CHAR_NEGATIVE_ANCHOR
                                                  if char_prompt else ""))
            frames.append(img)
            actual_seeds.append(actual)
        # 各镜首帧落盘（视频侧 I2V 首帧来源）
        shot_paths = []
        for si, fr in enumerate(frames):
            sp = out_dir / f"v{version}_shot{si + 1}.png"
            fr.save(sp, "PNG")
            shot_paths.append(str(sp.relative_to(DATA_DIR))
                              .replace("\\", "/"))
        image = _compose_grid_image(frames, grid["layout"])
        if image is None:
            image = frames[0]
        log.info("方案A逐镜生成: row=%s %d镜 %s 各格%dx%d",
                 row_id, len(frames), grid["layout"], cell_w, cell_h)
    else:
        if _ABC_MARK in prompt:
            gen_prompt = _image_prompt_from_abc(prompt)
        else:
            # 直文本兜底画风治理（2026-09-02 P6d 实测修复）：此前缀
            # _STYLE_PHOTO（08-14 写真人设参考图时代的写实助推遗留），
            # 网漫项目走此路径（未生成描述词、剧本原文兜底）被推成照
            # 片风、角色锚外观被文本压漂（实测白背心→黑背心、短寸→
            # 长发）。改与 ABC 主路径同规则：前置项目风格行（_project_
            # style_line 空风格回退网漫，非空）。
            from .comic_asset import _project_style_line
            pid = project_id
            if not pid:
                sb = db.query_one(
                    "SELECT project_id FROM storyboards WHERE id=?",
                    (row.get("storyboard_id"),))
                pid = ((sb or {}).get("project_id") or "")
            style_line = _project_style_line(db, pid)
            gen_prompt = f"{style_line}，{prompt}" if style_line else prompt
        broadcast_gen_progress("keyframe", row_id, percent=2,
                               label="正在生成")
        image, _actual = _gen_one(
            gen_prompt, width, height, refs=asset_refs,
            seed=base_seed, reflat_hint="pos",
            on_step=lambda p: (broadcast_gen_progress(
                "keyframe", row_id, percent=p, label="正在生成")))
        actual_seeds = [_actual]
    broadcast_gen_progress("keyframe", row_id, percent=96,
                           label="落盘登记")

    out_path = out_dir / f"v{version}.png"
    image.save(out_path, "PNG")
    rel_path = str(out_path.relative_to(DATA_DIR)).replace("\\", "/")
    kf_id = uuid.uuid4().hex
    _ensure_source_mode_column(db)
    db.update("keyframes", {"is_current": 0}, "row_id=?", (row_id,))
    db.insert("keyframes", {
        "id": kf_id, "row_id": row_id, "project_id": project_id,
        "version": version, "file_path": rel_path, "prompt": prompt,
        "status": "done", "error": "", "is_current": 1,
        "created_at": _now(),
        "shot_seeds": json.dumps(actual_seeds), "consistency": "",
        "source_mode": "fallback" if used_fallback else "describe"})
    db.update("storyboard_rows", {"generation_status": "done"},
              "id=?", (row_id,))
    broadcast_gen_progress("keyframe", row_id, percent=100, status="done",
                           label="生成完成")
    log.info("关键帧落库: row=%s v%d seeds=%s", row_id, version, actual_seeds)
    if use_comfy:
        # 生成毕即卸 ComfyUI 驻留权重（进程保活）——后续一致性守卫
        # 需唤醒 vLLM 评分，两族 ~10GB 级权重不可同驻（2026-08-27
        # V40 事故：comfy 驻留致 vLLM KV cache 仅 0.7GiB 启动失败）
        get_comfy_paint_engine().unload()
    return {"keyframe_id": kf_id, "row_id": row_id, "version": version,
            "file_path": rel_path, "prompt": prompt, "status": "done",
            "is_current": True}


# ═══════════════════════════════════════════════════════════════════
#  V37 方案B：VLM 一致性评分 + 低分自动重抽（2026-08-27 v36 事故）
# ═══════════════════════════════════════════════════════════════════
#
# 机制：生成完成后后台任务执行（不阻塞 API 返回）——
#   ①等 vLLM 唤醒就绪（生成期睡眠让渡显存，Windows fallback 冷启动
#     ~157s，轮询兜底 300s）
#   ②逐镜评分：参考图（资产设定）+ 资产 prompt 文本双基准（用户裁定
#     「对比+资产prompt文本」），Qwen3-VL 输出 0-100 分
#   ③任一镜 < 阈值 → 换 seed 自动重抽 1 次（force_new_seed=True，
#     限 1 次——用户裁定，质量/耗时平衡）
#   ④重抽版本再评分，择优置当前版本；仍低分保留较优并在
#     keyframes.consistency 列 + 系统日志标注
#   ⑤画风通道（2026-08-29）：角色绑定行逐镜加评「渲染技法/美术
#     风格」一致性（独立 VLM 调用，锚=角色资产图）——face_sim 只
#     管骨相身份、结构分模板明确「不评价画风」，写实化/厚涂化漂移
#     曾零拦截（Consistency LoRA 写实化事故）；<75 计入破门禁维度
# 显存编排：评分需 vLLM 醒、重抽需 vLLM 睡——顺序为 醒(评分) →
# 睡(重抽) → 醒(复评)，复用既有 sleep/wake 协商原语。

_CONSISTENCY_THRESHOLD = 75          # 面部身份结构分阈值（P0 收紧，
                                     # 低于此分触发重抽；原 70 属性分
                                     # 放行走廊过宽——V41 结构漂移 95 分
                                     # 假阳性事故）
_CONSISTENCY_WAIT_S = 300.0          # 等 vLLM 就绪上限（Windows 冷启 ~157s）
_CONSISTENCY_MAX_TOKENS = 160

# 人脸嵌入硬门禁（DINOv2 余弦，2026-08-27 P0 标定落地）：
# VLM 评审对面骨结构身份无区分力（V41 结构漂移稳定 95 分假阳性），
# 嵌入余弦硬指标实测可分离——标定：V38 人眼验收 min=0.779；
# V41 用户判漂移 shot3=0.570（拦截）；阈值取 0.70（V38 min 与
# V41 shot3 之间中位偏上）。无脸检测帧（侧背/远景）返回 None
# 跳过该维度，不按 0 分判负。
# 2026-08-27 V46 收紧 0.70→0.75：V46 人脸门禁全过（0.72~0.84）
# 但用户肉眼判漂移——0.70 只拦「换人」，拦不住「微妙变脸」的
# 灰色地带；收紧至 0.75（V38 验收 min 0.779 之上留余量，
# V44 灰色样本 0.736/0.740 将触发重抽择优）。
_FACE_SIM_THRESHOLD = 0.75

# PuLID 身份注入强度（P1，2026-08-28）：官方推荐 1.3~1.4，但节点
# issue#15 争议（增益或来自 ReferenceLatent）——生产默认 1.0 起，
# A/B 标定后再调；灰色地带（face_sim 0.75~0.84）由 PuLID 硬锁
_PULID_STRENGTH = 1.0

# 西化瞳色负面锚（2026-08-28 v65 蓝绿眼事故）：klein-9b 在瞳色信号
# 弱时系统性漂向西化浅色瞳（V49「蓝绿眼/西化五官」同源）。ECU/MCU
# 构图指令的 eye color 正向锚在 comfy 链路被 ReferenceLatent/PuLID
# 双 latent 通道稀释（v64/v65 shot3/shot4 实测仍漂），CFG negative
# 通道补位排除。非外貌锚定（不指定瞳色值，只排西化漂移方向；棕/
# 黑/琥珀不在排除列——亚洲角色资产主流）；仅绑角色资产行生效；
# 蓝瞳角色资产会误伤（资产级瞳色协议为后续方向，先记边界）。
# v67 实测：ECU（满框脸）负面锚已修复 shot4（浅绿→浅棕一致）；
# MCU（半身）眼睛占比低信号衰减 shot3 仍浅蓝——扩 grey eyes 覆盖
# 灰蓝漂移带。
# v69 实测：shot3 跨 3 seed（1476896851×2/1964321414）稳定浅蓝——
# 眼睛为画面表现主体时（C 段「眼神来回游移」）klein「高表现力大
# 眼」先验压过负面信号；措辞强化补 iris 变体，仍无效则需眼部局部
# 重绘方案（另立项）。P1 逐镜化（2026-08-28）：随逐镜角色锚存在性
# 注入（_gen_one char_negative 参数）。
_CHAR_NEGATIVE_ANCHOR = ("blue eyes, green eyes, grey eyes, blue iris, "
                         "green iris, grey iris, heterochromia, "
                         "light-colored eyes")

# ArcReel 链式参考开关（用户指令② 2026-08-28）：True=仅从复用帧
# （过门禁校验）起链——部分重抽时上一镜复用帧作 ReferenceLatent
# （v58 实证 face 0.888）；整版重抽不进链（v61/v62 误差复利事故，
# 见逐镜循环注释）。False=完全关闭（回归口）
_CHAIN_REF = True

# 场景嵌入门禁（DINOv2 全图余弦，2026-08-27 V46 场景无门禁事故）：
# 此前只查人脸，场景漂移无任何硬指标。标定（场景资产图 vs 全图）：
# 远景 0.6~0.88 / 中景 0.5~0.68 / 特写 0.03~0.09（景别强混淆）——
# 仅对无人脸镜头（face_sim=None 的远景/空镜，场景主导帧）作硬门禁，
# 有人脸镜头由人脸门禁负责；v46 用户判负 shot2=0.511 < 阈值。
# 无场景绑定/基准构建失败返回 None 跳过，不按 0 判负。
_SCENE_SIM_THRESHOLD = 0.60

# 远景镜 scene 门禁阈值（2026-08-28 v55-v60 标定）：好远景镜
# scene_sim 实测带宽 0.521~0.696（五版 VLM 全 90-95、人物判定无
# 争议），DINOv2 全图嵌入对同场景远景也有 ±0.17 方差（v58/v59
# 同 seed 分别 0.594/0.521）——0.60 卡带中位会误判约半数好远景
# 镜触发无谓重抽（v59/v60 shot1 连续 0.521/0.568 被重抽均更差，
# 择优回滚 v59）。降 0.50 容忍嵌入方差；错场景（特写带 0.03~
# 0.09、错地点）仍远低于此线必被拦截。
_WIDE_SCENE_SIM_THRESHOLD = 0.50

# 按镜位 face 门禁档（2026-08-28 R2/R3 修复后管线标定回填）：
# 单一 0.75 阈值错标两档——R1-b 304 帧历史分布 + R3/ECU 专项共
# 21 帧修复后实测带宽定档。远景镜不在此列（face 门禁豁免，走
# scene 门禁）。
#   medium —— 0.58：R3 pos 档合法 3/4 侧角帧 dino 0.615（目检
#     画风/场景/叙事全对，角度方差压分），0.75 会冤枉此类合法帧
#     触发无谓重抽（v66→v80 一日 14 版的推手之一）；
#   MCU    —— 0.82：R3 off 档带宽 0.881~0.913（3/3 收敛，PuLID
#     硬锁质变带；历史 diffusers 带中位仅 0.695），p05-0.06 定档；
#   ECU    —— 0.75：off 档带双峰（好帧 ≥0.767 目检优，漂移帧
#     ≤0.744 目检写实化），0.75 恰卡实测边界；漂移侧由重抽+择优
#     兜底。特写发型漂移（齐刘海先验，pos/off 均复现）是遗留项，
#     门禁拦不住同风格发型漂移——P1 资产级外貌协议治理。
_FACE_SIM_MEDIUM = 0.58             # Medium shot（中景，pos 档）
_FACE_SIM_MCU = 0.82                # Medium close-up（中近景，off 档）
_FACE_SIM_ECU = 0.75                # Extreme close-up（特写，off 档）


def _face_threshold_for(framing: list | None, idx: int) -> float:
    """镜位 → face 门禁阈值档（未知 framing 维持默认档）。"""
    if not framing or idx >= len(framing):
        return _FACE_SIM_THRESHOLD
    f = framing[idx].lower()
    if f.startswith("medium close-up"):
        return _FACE_SIM_MCU
    if f.startswith("extreme close-up"):
        return _FACE_SIM_ECU
    if f.startswith("medium shot"):
        return _FACE_SIM_MEDIUM
    return _FACE_SIM_THRESHOLD

_CONSISTENCY_PROMPT_CHAR = (
    "你是人脸身份一致性评审。前 {n_ref} 张是角色设定参考图，"
    "最后一张是 AI 生成的分镜首帧。\n"
    "任务：判断首帧中的核心人物与参考图人物是否为同一人。逐维度"
    "对比面部结构（不评价画风/构图/画质）：①脸型轮廓与下颌线 "
    "②眼型/眼距/眼睑形态 ③鼻型/鼻梁高度 ④唇形/嘴宽 ⑤发型/"
    "发际线/刘海位置 ⑥肤色肤质。侧脸/低头/遮挡按可见维度评估。\n"
    "关键判据：发型服装相同但面部结构不同 = 不同人，必须低分；"
    "仅渲染风格差异不扣分。\n"
    "输出格式（严格遵守两行）：\n评分：NN\n理由：一句话\n"
    "NN 为 0-100 整数（≥90=确定同一人；75-89=大致相似但局部结构"
    "偏差；<75=面部结构明显不同）。")
_CONSISTENCY_PROMPT_GENERIC = (
    "你是漫剧关键帧一致性评审。前 {n_ref} 张是场景/美术设定参考图，"
    "最后一张是 AI 生成的分镜首帧。\n"
    "只对比「内容与设定的一致性」：场景要素、关键道具、整体氛围。"
    "不评价画风、构图、画质。\n"
    "输出格式（严格遵守两行）：\n评分：NN\n理由：一句话\n"
    "NN 为 0-100 整数，100 = 与设定完全一致。")


def _parse_consistency_score(text: str) -> int | None:
    """从 VLM 输出解析 0-100 评分（宽容解析，模型偶发格式漂移）。"""
    m = re.search(r"评分[：:]\s*(\d{1,3})", text)
    if not m:
        m = re.search(r"(\d{1,3})\s*分", text)
    if not m:
        return None
    return max(0, min(100, int(m.group(1))))


def _score_shot_sync(shot_path: Path, ref_b64: list[str],
                     char_anchor: bool) -> tuple[int | None, str]:
    """单镜一致性评分（阻塞，经 run_blocking 调用）。

    char_anchor：有角色资产绑定（参考图为角色设定图）→ 人脸身份
    评审 CHAR 模板；否则场景评审 GENERIC 模板（V49 修复：路由按
    资产类型，不依赖已废除的描述词文本通道）。

    Returns:
        (score, reason)：score None = 评分失败/解析失败（不计入阈值
        判定）；reason 为 VLM 理由或错误信息（截断 200 字）。
    """
    try:
        from PIL import Image
        img = Image.open(shot_path).convert("RGB")
        img.thumbnail((960, 960), Image.LANCZOS)  # 省 token / 加速理解
        tpl = (_CONSISTENCY_PROMPT_CHAR if char_anchor
               else _CONSISTENCY_PROMPT_GENERIC)
        prompt_text = tpl.format(n_ref=len(ref_b64))
        svc = get_vllm_service()
        chunks: list[str] = []
        for ch in svc.chat_stream(
                [{"role": "user", "content": prompt_text}],
                images_b64=ref_b64 + pil_images_to_b64([img]),
                temperature=0.0, max_tokens=_CONSISTENCY_MAX_TOKENS):
            chunks.append(ch)
        text = "".join(chunks).strip()
        return _parse_consistency_score(text), text[:200]
    except Exception as exc:  # noqa: BLE001
        log.warning("一致性评分失败 %s: %s", shot_path, exc, exc_info=True)
        return None, str(exc)[:200]


# VLM 画风判定通道（2026-08-29，用户指令）：face_sim（DINOv2 人脸
# 嵌入）对发色/画风/年龄漂移不敏感，VLM 结构分模板又明确「不评价
# 画风/仅渲染风格差异不扣分」（防干扰身份判定）——写实化/厚涂化
# 等画风体系漂移曾零拦截（Consistency LoRA 写实化事故教训）。本
# 通道独立评审「渲染技法与美术风格」维度，与身份维度互补。
# 仅角色资产绑定行启用：风格锚=角色立绘图（场景资产常见照片参考，
# 作画风锚会恒判漂移误伤）；锚加载失败仅关闭本通道不阻塞其余门禁。
# 阈值与结构分同档 75：画风是 VLM 强项（对比身份骨相更可分，V41
# 假阳性教训不适用）；PuLID 链路量化带宽待标定，首版只拦显著漂移。
_STYLE_SIM_THRESHOLD = 75

_STYLE_PROMPT = (
    "你是画风一致性评审。第 1 张是角色资产设定图（画风基准），"
    "第 2 张是 AI 生成的分镜首帧。\n"
    "任务：只对比两图「美术风格/渲染技法」是否同体系，逐维度："
    "①画风类型（赛璐璐2D/厚涂插画/3D CG 渲染/写实照片感）"
    "②线条与上色（描边线稿有无、上色 flat 还是厚重）"
    "③光影质感（软阴影/硬光影/照片级光影）④肤色与材质渲染写实度。"
    "不评价人物身份、构图、画质、内容。\n"
    "关键判据：参考立绘是二次元风，首帧却渲染成写实/照片感 = 显著"
    "画风漂移，必须低分；仅光影氛围或色调节差异不扣分。\n"
    "输出格式（严格遵守两行）：\n评分：NN\n理由：一句话\n"
    "NN 为 0-100 整数（≥90=画风同体系；75-89=同体系但有写实化/"
    "厚涂化倾向；<75=画风体系明显不同）。")


def _score_style_sync(shot_path: Path, style_ref_b64: str) -> tuple[int | None, str]:
    """单镜画风评分（阻塞，经 run_blocking 调用）：角色资产图 vs 首帧。

    Returns:
        (score, reason)：score None = 评分失败/解析失败（不计入
        阈值判定）；reason 为 VLM 理由或错误信息（截断 200 字）。
    """
    try:
        from PIL import Image
        img = Image.open(shot_path).convert("RGB")
        img.thumbnail((960, 960), Image.LANCZOS)  # 省 token / 加速理解
        svc = get_vllm_service()
        chunks: list[str] = []
        for ch in svc.chat_stream(
                [{"role": "user", "content": _STYLE_PROMPT}],
                images_b64=[style_ref_b64] + pil_images_to_b64([img]),
                temperature=0.0, max_tokens=_CONSISTENCY_MAX_TOKENS):
            chunks.append(ch)
        text = "".join(chunks).strip()
        return _parse_consistency_score(text), text[:200]
    except Exception as exc:  # noqa: BLE001
        log.warning("画风评分失败 %s: %s", shot_path, exc, exc_info=True)
        return None, str(exc)[:200]


def _scoring_context(db: Database, row: dict) -> tuple[list[str], bool]:
    """评分基准（P0 修正 2026-08-27：仅图像锚，文本通道已废除）：

    参考图 b64 列表（行级参考 + 面部参考，≤2 张）；返回值第二项
    为「有角色资产绑定」信号，用于 VLM 模板路由（角色图 → 人脸
    身份评审 CHAR 模板；无角色 → 场景评审 GENERIC 模板）。
    V49 修复（2026-08-28）：原返回 char_prompt 字符串（V49 后恒
    空）→ 评分恒走 GENERIC 场景模板 → 特写镜（纯人脸无场景要素
    可评）恒 0 分。模板路由改按资产类型判定，不依赖文本。
    fail-closed：绑定了角色资产但其图像文件缺失（路径漂移/被清理
    ——参考图静默跳过后无身份锚出高分的假阳性事故）→ 返回空
    refs，调用方标记 score_unavailable，禁止无锚出分。
    """
    from ...config import DATA_DIR as _DATA_DIR
    char_bound = False
    char_missing = False
    for a in _fetch_bound_assets(db, row.get("asset_ids") or []):
        if a.get("kind") != "character":
            continue
        char_bound = True
        fp = (a.get("file_path") or "").strip()
        if not fp or not (_DATA_DIR / fp).is_file():
            char_missing = True
    if char_bound and char_missing:
        log.warning("角色资产图像缺失，一致性评分关闭（fail-closed）: "
                    "row=%s", row.get("id"))
        return [], False
    refs: list[str] = []
    row_ref = _load_row_reference(db, row)
    if row_ref is not None:
        refs.extend(pil_images_to_b64([row_ref]))
    face_ref = _load_face_reference(db, row)
    if face_ref is not None and len(refs) < 2:
        refs.extend(pil_images_to_b64([face_ref]))
    return refs, char_bound


def _shot_files_for_version(row_id: str, version: int) -> list:
    """该版本参与评分的图像文件：网格行取逐镜首帧，单帧取展示图。"""
    out_dir = _KEYFRAME_DIR / row_id
    shots = sorted(out_dir.glob(f"v{version}_shot*.png"))
    if shots:
        return shots
    single = out_dir / f"v{version}.png"
    return [single] if single.is_file() else []


async def _wait_vllm_healthy(timeout_s: float = _CONSISTENCY_WAIT_S) -> bool:
    """等 vLLM 就绪：未运行则后台冷启动（start_async 幂等），轮询兜底。

    生成期 vLLM 处于「睡眠让渡」或「本就未启动」两种状态都要覆盖
    ——wake_from_paint 只处理前者（_stopped_for_paint 置位才重启），
    后者由本函数点火（2026-08-27 e2e 实测缺口：vLLM 未运行时纯
    轮询 300s 空等，一致性校验被跳过）。
    """
    svc = get_vllm_service()
    if not svc.runtime_ready():
        return False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    ignited = False
    while loop.time() < deadline:
        try:
            if await run_blocking(svc.is_healthy):
                return True
            if not ignited:
                ignited = True
                svc.start_async()  # 幂等：已在运行/启动中则复用
        except Exception:  # noqa: BLE001
            log.debug("_wait_vllm_healthy: 降级忽略", exc_info=True)
        await asyncio.sleep(5.0)
    return False


def _annotate_consistency(db: Database, kf_id: str, payload: dict) -> None:
    """评分结果落 keyframes.consistency（JSON，前端详情可查）。"""
    try:
        db.update("keyframes", {"consistency": json.dumps(
            payload, ensure_ascii=False)}, "id=?", (kf_id,))
    except Exception as exc:  # noqa: BLE001
        log.warning("一致性标注落库失败 %s: %s", kf_id, exc, exc_info=True)


async def _consistency_guard(row_id: str, project_id: str, kf_id: str,
                             version: int, width: int, height: int,
                             engine_backend: str = "diffusers",
                             vllm_was_active: bool = True,
                             text_priority: bool = False) -> None:
    """生成后一致性守卫（后台任务，全 best-effort 不抛错）。

    vllm_was_active：端点在生成前采样的 vLLM 活跃态（运行/冷启动
    中）。False 表示评分所需 vLLM 系本守卫点火（_wait_vllm_healthy
    冷启动），S7 收尾——评分毕停止实例回收显存（实测：守卫点火
    后 qwen3-vl-8b-awq 常驻 ~13GB，整卡 93% 越用户 90% 约束线）；
    True 表示生成前即为用户对话态，保留实例。
    """
    try:
        await _consistency_guard_inner(
            row_id, project_id, kf_id, version, width, height,
            engine_backend, text_priority=text_priority)
    except Exception as exc:  # noqa: BLE001
        log.warning("一致性守卫异常（不影响已生成结果）row=%s: %s",
                    row_id, exc)
    finally:
        if not vllm_was_active:
            # 用户操作最高权限：评分期间用户开始对话（dialog 锁持有
            # 中）→ 让位不停；其余情况停止守卫点火的实例。用户随后
            # 主动对话时由 dialog_engine.ensure_loaded 按需重启。
            try:
                from ...middleware.feature_lock import get_feature_lock
                if get_feature_lock().active_feature == "dialog":
                    log.info("一致性守卫收尾让位（用户对话中）: row=%s",
                             row_id)
                else:
                    await run_blocking(get_vllm_service().stop)
                    log.info("一致性守卫收尾：评分点火的 vLLM 已停止"
                             "回收显存（row=%s）", row_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("一致性守卫收尾停止 vLLM 失败（不阻断）: %s",
                            exc)


async def _consistency_guard_inner(row_id: str, project_id: str,
                                   kf_id: str, version: int,
                                   width: int, height: int,
                                   engine_backend: str = "diffusers",
                                   text_priority: bool = False
                                   ) -> None:
    db = get_db_safe()
    if db is None:
        return
    broadcast_gen_progress("keyframe", row_id, percent=100,
                           label="AI 一致性校验中")
    if not await _wait_vllm_healthy():
        log.info("一致性校验跳过（对话引擎未就绪）: row=%s", row_id)
        _annotate_consistency(db, kf_id, {
            "skipped": "vllm_not_ready", "ts": _now()})
        broadcast_gen_progress("keyframe", row_id, percent=100,
                               status="done", label="生成完成")
        return
    # 用户操作最高权限：该校验版本已不是当前版本（用户又手动重生
    # 成了）→ 让位，不做任何评分/重抽
    kf = await run_blocking(lambda: db.query_one(f"SELECT {_KF_COLS} FROM keyframes WHERE id=?",
                      (kf_id,)))
    if not kf or not int(kf.get("is_current", 0)):
        log.info("一致性校验让位（已有更新版本）: row=%s v%d", row_id, version)
        return
    row = await run_blocking(lambda: db.query_one(
        f"SELECT {_SB_ROW_COLS} FROM storyboard_rows WHERE id=?", (row_id,)))
    if row is None:
        return
    refs, char_anchor = _scoring_context(db, row)
    shot_files = _shot_files_for_version(row_id, version)
    if not shot_files:
        return
    # 远景镜识别（v51-v58 标定，见 _is_wide_shot docstring）：face
    # 门禁豁免的判定上下文——layout 来自该版本 keyframe 自身的
    # prompt（与生成时同源，1x2/2x2 首镜恒为远景 establishing）
    framing_ctx = _SHOT_FRAMING.get(
        (_parse_abc_shots(str(kf.get("prompt") or ""))
         .get("layout")) or "")
    if not refs:
        # fail-closed：无任何图像锚（角色图缺失/行无绑定资产）→
        # 禁止出分（无锚评分必然失真——V41 假阳性事故教训）
        _annotate_consistency(db, kf_id, {
            "skipped": "score_unavailable", "note": "no_image_anchor",
            "ts": _now()})
        broadcast_gen_progress("keyframe", row_id, percent=100,
                               status="done", label="生成完成")
        return

    # 人脸嵌入基准（角色绑定时构建；构建失败仅降级跳过该门禁）。
    # V49 修复（2026-08-28）：原条件 `char_prompt and ...` 在 V49 协议
    # （评分不读资产描述词）下恒 False → face 基准永不构建 → 人物
    # 镜全部误走 scene 维度门禁（scene 资产全图嵌入对中景/特写恒
    # 低分）→ 守卫恒重抽。门禁基准按图像锚存在性判定（无角色绑定
    # 时加载器返回 None，ref_vecs 自然为空，行为不变）
    ref_vecs: list = []
    if face_sim.face_sim_available():
        try:
            # 参考图加载含 DB 查询+PIL 解码，同卸载约定（审计 09-10 P2-3）
            for rim in (await run_blocking(_load_face_reference, db, row),
                        await run_blocking(_load_row_reference, db, row)):
                if rim is None:
                    continue
                v = await run_blocking(face_sim.embed_face, rim)
                if v is not None:
                    ref_vecs.append(v)
        except Exception as exc:  # noqa: BLE001
            log.warning("人脸嵌入基准构建失败（嵌入门禁降级关闭）: %s", exc, exc_info=True)
            ref_vecs = []

    # 场景嵌入基准（2026-08-27 V46 场景无门禁事故）：绑定 scene
    # 资产首图全图嵌入；无人脸镜头（远景/空镜）的硬门禁锚点
    scene_vec = None
    if face_sim.face_sim_available():
        try:
            from ...config import DATA_DIR as _DATA_DIR
            for a in _fetch_bound_assets(db, row.get("asset_ids") or []):
                if a.get("kind") != "scene" or not a.get("file_path"):
                    continue
                sp = _DATA_DIR / a["file_path"]
                if not sp.is_file():
                    continue
                from PIL import Image as _PILImage
                scene_vec = await run_blocking(
                    face_sim.embed_scene,
                    _PILImage.open(sp).convert("RGB"))
                break
        except Exception as exc:  # noqa: BLE001
            log.warning("场景嵌入基准构建失败（场景门禁降级关闭）: %s", exc, exc_info=True)
            scene_vec = None

    # 画风判定锚（2026-08-29 画风通道）：角色绑定行取首张角色资产图
    # 作画风基准（角色 > 场景 > prop 的加载优先序沿用参考条件图约定
    # ——画风漂移的主受害对象是角色立绘）；加载失败仅关闭该通道，
    # 不阻塞其余门禁维度
    style_ref_b64: str | None = None
    try:
        from PIL import Image as _PILImage

        from ...config import DATA_DIR as _DATA_DIR
        for a in _fetch_bound_assets(db, row.get("asset_ids") or []):
            if a.get("kind") != "character" or not a.get("file_path"):
                continue
            cp = _DATA_DIR / a["file_path"]
            if not cp.is_file():
                continue
            rim = _PILImage.open(cp).convert("RGB")
            rim.thumbnail((960, 960), _PILImage.LANCZOS)
            style_ref_b64 = pil_images_to_b64([rim])[0]
            break
    except Exception as exc:  # noqa: BLE001
        log.warning("画风锚加载失败（画风通道降级关闭）: %s", exc, exc_info=True)
        style_ref_b64 = None

    # 逐角色 ArcFace 基准（2026-08-28 P0-2）：R1 标定（428 帧实测）
    # 证伪 ArcFace 对风格化内容的门禁能力——同角色 median 0.32 vs
    # 跨角色 0.21（分离度为负），连特写镜也只有 0.43；DINOv2 同数
    # 据同样重叠（pos med 0.68 / neg med 0.61）但方向性弱优。故
    # face 门禁主度量维持 DINOv2，ArcFace 作为证据通道落
    # consistency 字段（写实资产的 ArcFace 门禁待写实标定数据，
    # 另行启用），同时产出 face_count 多实例告警信号（v26「参考
    # 主体被复制成多人」事故的可见度）
    arc_ref_vecs: list = []
    face_refs_imgs = _load_face_references(db, row)
    if face_sim.arc_available() and any(face_refs_imgs):
        for rim in face_refs_imgs:
            if rim is None:
                arc_ref_vecs.append(None)
                continue
            try:
                arc_ref_vecs.append(
                    await run_blocking(face_sim.embed_face_arc, rim))
            except Exception as exc:  # noqa: BLE001
                log.warning("ArcFace 基准构建失败（证据通道关闭）: %s", exc, exc_info=True)
                arc_ref_vecs.append(None)

    scores: list[int | None] = []
    reasons: list[str] = []
    face_sims: list[float | None] = []
    scene_sims: list[float | None] = []
    style_scores: list[int | None] = []
    arc_sims: list[list] = []
    face_counts: list[int] = []
    has_arc = any(v is not None for v in arc_ref_vecs)
    for i, fp in enumerate(shot_files):
        broadcast_gen_progress(
            "keyframe", row_id, percent=100,
            label=f"一致性评分 {i + 1}/{len(shot_files)}")
        s, reason = await run_blocking(_score_shot_sync, fp, refs,
                                       char_anchor)
        style_s = (await run_blocking(_score_style_sync, fp, style_ref_b64)
                   if style_ref_b64 else None)
        sim = (await run_blocking(face_sim.face_similarity, fp, ref_vecs)
               if ref_vecs else None)
        ssim = (await run_blocking(face_sim.scene_similarity, fp, scene_vec)
                if scene_vec is not None else None)
        arc_row, n_faces = (await run_blocking(face_sim.arcface_match,
                                               fp, arc_ref_vecs)
                            if has_arc else ([], 0))
        scores.append(s)
        reasons.append(reason)
        face_sims.append(sim)
        scene_sims.append(ssim)
        style_scores.append(style_s[0] if style_s else None)
        arc_sims.append(arc_row)
        face_counts.append(n_faces)
        log.info("一致性评分: row=%s v%d shot%d score=%s face_sim=%s "
                 "scene_sim=%s style=%s arc=%s faces=%d reason=%s",
                 row_id, version, i + 1, s,
                 f"{sim:.3f}" if sim is not None else "n/a",
                 f"{ssim:.3f}" if ssim is not None else "n/a",
                 str(style_s[0]) if style_s else "n/a",
                 ([f"{x:.3f}" if x is not None else "n/a" for x in arc_row]
                  if arc_row else "-"),
                 n_faces, reason[:80])
    valid = [s for s in scores if s is not None]
    sim_valid = [x for x in face_sims if x is not None]
    if not valid and not sim_valid:
        _annotate_consistency(db, kf_id, {
            "skipped": "score_unavailable", "reasons": reasons, "ts": _now()})
        broadcast_gen_progress("keyframe", row_id, percent=100,
                               status="done", label="生成完成")
        return

    def _failed(vlm_s: int | None, sim: float | None,
                scene_sim: float | None,
                is_wide: bool = False,
                is_char: bool = False,
                face_thr: float = _FACE_SIM_THRESHOLD,
                style_s: int | None = None) -> bool:
        if vlm_s is not None and vlm_s < _CONSISTENCY_THRESHOLD:
            return True
        # 画风通道（2026-08-29）：仅角色锚行启用（style_ref 存在时
        # 才有分）——写实化/厚涂化体系漂移独立拦截，与身份维度互补
        if style_s is not None and style_s < _STYLE_SIM_THRESHOLD:
            return True
        if is_wide:
            # 远景镜 face 门禁豁免（v51-v58 标定，见 _is_wide_shot）：
            # 小脸嵌入方差大（0.311~0.781 漂移而 VLM 恒判同一人），
            # 由 VLM + scene 门禁负责；scene 阈值用远景标定档
            # 0.50（好远景带 0.521~0.696，见 _WIDE_SCENE_SIM_THRESHOLD）；
            # 无场景基准时仅 VLM 兜底
            return (scene_sim is not None
                    and scene_sim < _WIDE_SCENE_SIM_THRESHOLD)
        if sim is not None:
            # 有人脸镜头：人脸门禁按镜位档负责（R1 标定：单一阈值
            # 对中景错标，见 _face_threshold_for）
            return sim < face_thr
        if is_char:
            # 人物镜人脸检测失败（画风化/裁切/侧脸，v63 shot3 事故）：
            # scene 门禁对人物镜恒低（景别混淆），仅 VLM 兜底
            return False
        # 无人脸镜头（framing 未知的远景/空镜）：场景门禁（0.60）
        # 负责；无场景基准时本镜头仅 VLM 兜底
        return (scene_sim is not None
                and scene_sim < _SCENE_SIM_THRESHOLD)

    failed_shots = [
        i + 1 for i, (s, sim, ssim, st) in enumerate(
            zip(scores, face_sims, scene_sims, style_scores, strict=False))
        if _failed(s, sim, ssim, _is_wide_shot(framing_ctx, i),
                   _is_char_shot(framing_ctx, i),
                   _face_threshold_for(framing_ctx, i), st)]
    overall = min(valid) if valid else None
    if not failed_shots:
        _annotate_consistency(db, kf_id, {
            "shots": scores, "face_sims": face_sims,
            "scene_sims": scene_sims, "style_scores": style_scores,
            "arc_sims": arc_sims,
            "face_count": face_counts, "min": overall,
            "threshold": _CONSISTENCY_THRESHOLD,
            "face_threshold": _FACE_SIM_THRESHOLD,
            "scene_threshold": _SCENE_SIM_THRESHOLD,
            "style_threshold": _STYLE_SIM_THRESHOLD, "retried": False,
            "picked": version, "ts": _now()})
        log_event("manga", "keyframe_consistency_pass",
                  f"分镜关键帧一致性校验通过（VLM {overall} 分）",
                  detail=f"row={row_id} v{version} shots={scores} "
                         f"face_sims={face_sims} scene_sims={scene_sims} "
                         f"style_scores={style_scores}",
                  trace_id=row_id)
        broadcast_gen_progress("keyframe", row_id, percent=100,
                               status="done",
                               label=f"一致性校验通过（{overall} 分）")
        return

    # ── 低分：换 seed 自动重抽 1 次（用户裁定限 1 次）─────────────
    log_event("manga", "keyframe_consistency_retry",
              f"第 {'、'.join(map(str, failed_shots))} 镜与角色设定一致性"
              f"偏低（VLM {overall} 分），正在自动重抽一次",
              level="warning",
              detail=f"row={row_id} v{version} shots={scores} "
                     f"face_sims={face_sims} scene_sims={scene_sims}",
              trace_id=row_id)
    broadcast_gen_progress("keyframe", row_id, percent=100,
                           label=f"一致性 {overall if overall is not None else '—'} "
                                 f"分偏低，自动重抽一次")
    try:
        lock = await acquire_or_raise("paint",
                                      task_id=f"consistency:{row_id}")
    except ApiError:
        # 用户正在跑别的生成——用户操作最高权限，让位不重抽
        log.info("一致性重抽让位（paint 锁被占用）: row=%s", row_id)
        _annotate_consistency(db, kf_id, {
            "shots": scores, "face_sims": face_sims,
            "scene_sims": scene_sims, "style_scores": style_scores,
            "min": overall,
            "threshold": _CONSISTENCY_THRESHOLD,
            "face_threshold": _FACE_SIM_THRESHOLD,
            "scene_threshold": _SCENE_SIM_THRESHOLD,
            "style_threshold": _STYLE_SIM_THRESHOLD, "retried": False,
            "note": "retry_skipped_lock", "ts": _now()})
        broadcast_gen_progress("keyframe", row_id, percent=100,
                               status="done", label="生成完成")
        return
    new_data: dict | None = None
    await _sleep_vllm_for_vram()
    try:
        # P2-B 逐镜重抽（V48 事故）：网格行只重生成破门禁镜，好镜
        # 帧文件+seed 双沿用——整版重抽全换 seed 会给已达标镜引入
        # 新方差（V48 shot3 0.717→0.645 劣化，仲裁被迫回退 V47）。
        # 单帧行 only_shots 在管线内被忽略（整图语义不变）
        new_data = await run_blocking(
            _generate_keyframe_sync, row_id, project_id,
            str(kf.get("prompt") or ""), width, height, None, True,
            engine_backend, failed_shots, version,
            text_priority=text_priority)
    except Exception as exc:  # noqa: BLE001
        log.warning("一致性重抽生成失败（保留原版本）: %s", exc, exc_info=True)
        log_event("manga", "keyframe_consistency_retry",
                  "自动重抽失败了，已保留原关键帧", level="warning",
                  detail=str(exc)[:200], trace_id=row_id)
    finally:
        # S7 生成毕即卸：重抽毕卸管线再唤醒 vLLM 复评（见
        # keyframe_generate 注释，两族权重不可同驻）
        await _unload_paint_after_gen()
        await lock.release("paint")
        # 批3（2026-09-10）：释锁后立即唤醒 → 去抖唤醒——立即唤醒
        # 会被紧邻的下一任务（绘画/视频队列）功能锁门禁拒绝且无人
        # 重试（2026-09-08 实测撞锁形态）；经协调器到点仍空闲才唤醒
        _schedule_debounced_wake(f"keyframe:{row_id}")
    if new_data is None:
        _annotate_consistency(db, kf_id, {
            "shots": scores, "face_sims": face_sims,
            "scene_sims": scene_sims, "style_scores": style_scores,
            "min": overall,
            "threshold": _CONSISTENCY_THRESHOLD,
            "face_threshold": _FACE_SIM_THRESHOLD,
            "scene_threshold": _SCENE_SIM_THRESHOLD,
            "style_threshold": _STYLE_SIM_THRESHOLD, "retried": True,
            "note": "retry_failed", "ts": _now()})
        broadcast_gen_progress("keyframe", row_id, percent=100,
                               status="done", label="生成完成")
        return

    # ── 重抽版本复评（VLM + 人脸/场景嵌入门禁），择优置当前 ────────
    new_version = int(new_data["version"])
    new_kf_id = str(new_data["keyframe_id"])
    new_scores: list[int | None] = []
    new_face_sims: list[float | None] = []
    new_scene_sims: list[float | None] = []
    new_style_scores: list[int | None] = []
    new_arc_sims: list[list] = []
    new_face_counts: list[int] = []
    if await _wait_vllm_healthy():
        new_files = _shot_files_for_version(row_id, new_version)
        for fp in new_files:
            # 逐镜容错（2026-08-29 R5 验收事故补强）：单镜指标异常
            # 记 None 继续，不丢整版标注/择优依据
            try:
                s, _r = await run_blocking(_score_shot_sync, fp, refs,
                                           char_anchor)
                st2 = (await run_blocking(_score_style_sync, fp,
                                          style_ref_b64)
                       if style_ref_b64 else None)
                sim2 = (await run_blocking(face_sim.face_similarity, fp,
                                           ref_vecs)
                        if ref_vecs else None)
                ssim2 = (await run_blocking(
                            face_sim.scene_similarity, fp, scene_vec)
                         if scene_vec is not None else None)
                n_arc, n_faces2 = (await run_blocking(
                    face_sim.arcface_match, fp, arc_ref_vecs)
                    if has_arc else ([], 0))
            except Exception as exc:  # noqa: BLE001
                log.warning("复评单镜失败（记 None 继续）%s: %s", fp, exc, exc_info=True)
                s, sim2, ssim2 = None, None, None
                st2, n_arc, n_faces2 = None, [], 0
            new_scores.append(s)
            new_face_sims.append(sim2)
            new_scene_sims.append(ssim2)
            new_style_scores.append(st2[0] if st2 else None)
            new_arc_sims.append(n_arc)
            new_face_counts.append(n_faces2)
            log.info("一致性复评: row=%s v%d score=%s face_sim=%s "
                     "scene_sim=%s style=%s arc=%s faces=%d",
                     row_id, new_version, s,
                     f"{sim2:.3f}" if sim2 is not None else "n/a",
                     f"{ssim2:.3f}" if ssim2 is not None else "n/a",
                     str(st2[0]) if st2 else "n/a",
                     ([f"{x:.3f}" if x is not None else "n/a" for x in n_arc]
                      if n_arc else "-"),
                     n_faces2)

    def _combined(vlms: list[int | None],
                  sims: list[float | None],
                  scene_s: list[float | None],
                  styles: list[int | None] | None = None) -> int | None:
        """逐镜配对取门禁维度（有人脸用 face、无人脸用 scene；
        远景镜 face 嵌入方差大 → 取标定过的 scene 维度；人物镜
        face n/a → 仅 VLM 不采 scene 景别混淆值），避免 scene
        全图相似度被特写镜景别混淆拉爆择优。画风通道（2026-08-29）：
        角色锚行逐镜计入（同 0-100 量纲，min 汇聚自动参与择优）。"""
        vals = [v for v in vlms if v is not None]
        styles = styles or []
        for i, (f, sc) in enumerate(zip(sims, scene_s, strict=False)):
            if i < len(styles) and styles[i] is not None:
                vals.append(styles[i])
            if _is_wide_shot(framing_ctx, i):
                if sc is not None:
                    vals.append(round(sc * 100))
                elif f is not None:
                    vals.append(round(f * 100))
            elif f is not None:
                vals.append(round(f * 100))
            elif _is_char_shot(framing_ctx, i):
                pass  # 人物镜 face n/a：仅 VLM（scene 景别混淆不采）
            elif sc is not None:
                vals.append(round(sc * 100))
        return min(vals) if vals else None

    old_combined = _combined(scores, face_sims, scene_sims, style_scores)
    new_combined = _combined(new_scores, new_face_sims, new_scene_sims,
                             new_style_scores)
    new_valid = [s for s in new_scores if s is not None]
    new_overall = min(new_valid) if new_valid else None

    picked = new_version
    note = "retry_picked_new"
    if (new_combined is not None and old_combined is not None
            and new_combined < old_combined):
        # 重抽更差 → 回置原版本为当前（用户操作最高权限：期间若
        # 用户又手动生成了更新版本，不动 is_current）
        mx = await run_blocking(lambda: db.query_one(
            "SELECT MAX(version) AS mv FROM keyframes WHERE row_id=?",
            (row_id,)))
        if int((mx or {}).get("mv") or 0) <= new_version:
            await run_blocking(lambda: db.update("keyframes", {"is_current": 0}, "row_id=?", (row_id,)))
            await run_blocking(lambda: db.update("keyframes", {"is_current": 1}, "id=?", (kf_id,)))
            picked = version
            note = "retry_worse_rollback"
    elif any(_failed(s, sim, ssim, _is_wide_shot(framing_ctx, i),
                     _is_char_shot(framing_ctx, i),
                     _face_threshold_for(framing_ctx, i), st)
             for i, (s, sim, ssim, st) in
             enumerate(zip(new_scores, new_face_sims, new_scene_sims,
                             new_style_scores, strict=False))):
        note = "retry_still_low"
    _annotate_consistency(db, kf_id, {
        "shots": scores, "face_sims": face_sims,
        "scene_sims": scene_sims, "style_scores": style_scores,
        "arc_sims": arc_sims,
        "face_count": face_counts, "min": overall,
        "threshold": _CONSISTENCY_THRESHOLD,
        "face_threshold": _FACE_SIM_THRESHOLD,
        "scene_threshold": _SCENE_SIM_THRESHOLD,
        "style_threshold": _STYLE_SIM_THRESHOLD, "retried": True,
        "picked": picked, "ts": _now()})
    _annotate_consistency(db, new_kf_id, {
        "shots": new_scores, "face_sims": new_face_sims,
        "scene_sims": new_scene_sims, "style_scores": new_style_scores,
        "arc_sims": new_arc_sims,
        "face_count": new_face_counts, "min": new_overall,
        "threshold": _CONSISTENCY_THRESHOLD,
        "face_threshold": _FACE_SIM_THRESHOLD,
        "scene_threshold": _SCENE_SIM_THRESHOLD,
        "style_threshold": _STYLE_SIM_THRESHOLD, "retried": True,
        "picked": picked, "ts": _now()})
    if note == "retry_worse_rollback":
        friendly = (f"自动重抽后一致性反而更低（{new_overall} 分），"
                    f"已保留原来的版本")
        level = "warning"
    elif note == "retry_still_low":
        friendly = (f"自动重抽后一致性为 {new_overall} 分，仍低于"
                    f"{_CONSISTENCY_THRESHOLD} 分，已保留较优版本。"
                    f"可检查资产设定图或调整描述词后手动重试")
        level = "warning"
    else:
        friendly = f"自动重抽完成，一致性 {new_overall} 分"
        level = "info"
    log_event("manga", "keyframe_consistency_retried", friendly,
              level=level,
              detail=(f"row={row_id} v{version}→v{new_version} "
                      f"scores={scores}→{new_scores} picked=v{picked}"),
              trace_id=row_id)
    broadcast_gen_progress("keyframe", row_id, percent=100,
                           status="done", label=friendly)


@router.post("/manga/keyframe/generate")
async def keyframe_generate(req: KeyframeGenerateRequest) -> dict[str, Any]:
    """生成关键帧（COMIC-121）：分镜行描述 → SDXL 文生图 → 新版本登记。

    2026-09-02 图像队列改造：入统一图像队列（services/image_queue.py）
    顺序消费——并发多行生图不再「同名锁双双放行后引擎盲等」，而是
    FIFO 排队 + 排队中位次广播；paint 功能锁由队列排空循环持有
    （锁持有中守卫/调度器深层回收/force_unload 跳过活动功能类别的
    保护语义不变，2026-08-26 e2e 事故修复的等效实现）；vLLM 睡眠/
    绘画管线卸载/唤醒由队列统一编排（连续生图接力免换载 churn）。
    """
    # S7 收尾状态捕获（2026-08-28）：记录生成前 vLLM 活跃态（运行中
    # 或冷启动中均视为用户对话态）。守卫评分若系自身点火（生成前
    # 未运行），评分毕须停实例回收显存；生成前已活跃则保留。
    # 须在队列准入 vLLM 睡眠之前采样——睡眠会抹掉运行态。
    _svc = get_vllm_service()
    vllm_was_active = _svc.is_running() or _svc.is_booting()
    # P2-C（2026-08-28）默认路由升级 comfy+PuLID：画风仲裁消除拔河
    # 后 comfy+PuLID 实测全面占优（face_sim 0.846 最高 + 视觉画风
    # 归位最佳，ReferenceLatent 的 latent 级画风传递强于 diffusers
    # 中文文本编码），代价 ~110s/镜 vs diffusers ~30-60s/镜（质量
    # 优先）。auto 分支：PuLID 就绪 + 有角色资产 → comfy+PuLID，
    # 否则 diffusers（PuLID 缺件/无角色资产行自动回落）；显式
    # "comfy"/"diffusers" 恒尊重（回归口）
    engine_backend = req.engine or "auto"
    # P2-B 逐镜重抽端点暴露（2026-08-28）：src_version=该行当前版本
    #（复用帧/seed 来源）；无当前版本时 src_ver=0，管线内
    # only_shots 生效条件不满足→自动退化整版生成
    src_ver = 0
    if req.only_shots:
        _db = get_db_safe()
        _cur = (await run_blocking(lambda: _db.query_one(
            "SELECT version FROM keyframes WHERE row_id=? AND is_current=1",
            (req.row_id,))) if _db else None)
        src_ver = int((_cur or {}).get("version") or 0)
        log.info("逐镜重抽请求: row=%s shots=%s src=v%d",
                 req.row_id, req.only_shots, src_ver)

    # 云端路由（批2 云端API 2026-09-06）：关键帧工位绑定云端连接时，
    # 任务走云端道（队列跳过本地锁/热保护），生成核心换云端适配器。
    # 绑定解析失败按本地（不阻断生成）。
    try:
        from ...services.cloud_provider_service import get_image_endpoint
        _cloud_ep = get_image_endpoint("keyframe.image")
    except Exception as exc:  # noqa: BLE001
        log.warning("关键帧云端路由解析失败（按本地引擎）: %s", exc, exc_info=True)
        _cloud_ep = None

    def _kf_runner(task: dict, check_cancel: Callable[[], None]) -> dict:  # noqa: ARG001
        return _generate_keyframe_sync(
            req.row_id, req.project_id or "",
            (req.prompt or "").strip(), req.width, req.height,
            req.seed, req.force_new_seed, engine_backend,
            req.only_shots, src_ver, req.text_priority,
            cloud_endpoint=_cloud_ep, check_cancel=check_cancel)

    def _wait_cb(_task_id: str, pos: int) -> None:
        broadcast_gen_progress(
            "keyframe", req.row_id, percent=0, status="running",
            label=f"排队中（前 {pos - 1} 个）" if pos > 1 else "排队中")

    try:
        data = await get_image_queue().submit_and_wait({
            "task_id": f"kf:{req.row_id[:12]}:{time.time_ns():x}",
            "kind": "keyframe", "runner": _kf_runner,
            "loop": asyncio.get_running_loop(),
            "cloud": _cloud_ep is not None,
            "wait_progress_cb": _wait_cb})
    except ApiError as exc:
        broadcast_gen_progress("keyframe", req.row_id, percent=0,
                               status="error", label="生成失败",
                               error=exc.message)
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("关键帧生成失败: %s", exc)
        broadcast_gen_progress("keyframe", req.row_id, percent=0,
                               status="error", label="生成失败",
                               error=str(exc)[:200])
        raise ApiError("PAINT_GENERATION_FAILED", str(exc)[:300]) from exc
    # V37 方案B：生成后后台一致性校验（逐镜 VLM 评分 → 低分换 seed
    # 自动重抽 1 次 → 择优置当前）。不阻塞 API 返回；vLLM 唤醒（上方
    # finally 已触发）就绪后才开始评分，全程系统日志可追踪
    asyncio.create_task(_consistency_guard(
        req.row_id, req.project_id or "", str(data["keyframe_id"]),
        int(data["version"]), req.width, req.height, engine_backend,
        vllm_was_active=vllm_was_active,
        text_priority=req.text_priority))
    return ok(data)


@router.post("/manga/keyframe/batch")
async def keyframe_batch(req: KeyframeBatchRequest) -> dict[str, Any]:
    """批量关键帧生成（COMIC-122）：逐行串行生成，聚合成功/失败明细。

    2026-09-02 图像队列：整批=一个队列任务（循环期间队列持 paint 锁，
    守卫不将在用管线当空闲模型卸载；批次结束由队列排空统一收尾协商）。
    """
    if not req.row_ids:
        raise ApiError(40008, "缺少 row_ids 数组")

    # 云端路由（批2）：整批共用一次绑定解析
    try:
        from ...services.cloud_provider_service import get_image_endpoint
        _cloud_ep = get_image_endpoint("keyframe.image")
    except Exception as exc:  # noqa: BLE001
        log.warning("关键帧云端路由解析失败（按本地引擎）: %s", exc, exc_info=True)
        _cloud_ep = None

    def _batch_runner(task: dict, check_cancel: Callable[[], None]) -> dict:  # noqa: ARG001
        results, failed = [], []
        for row_id in req.row_ids:
            try:
                data = _generate_keyframe_sync(
                    str(row_id), req.project_id or "",
                    "", IMG_TARGET_W, IMG_TARGET_H,
                    cloud_endpoint=_cloud_ep, check_cancel=check_cancel)
                results.append(data)
            except ApiError as exc:
                failed.append({"row_id": str(row_id), "code": exc.code,
                               "message": exc.message})
            except Exception as exc:  # noqa: BLE001
                failed.append({"row_id": str(row_id),
                               "code": "PAINT_GENERATION_FAILED",
                               "message": str(exc)[:300]})
        return {"succeeded": results, "failed": failed,
                "total": len(req.row_ids), "success_count": len(results)}

    data = await get_image_queue().submit_and_wait({
        "task_id": f"kfbatch:{req.project_id[:12]}:{time.time_ns():x}",
        "kind": "keyframe_batch", "runner": _batch_runner,
        "loop": asyncio.get_running_loop(),
        "cloud": _cloud_ep is not None})
    return ok(data)


@router.post("/manga/keyframe/regenerate")
async def keyframe_regenerate(req: KeyframeGenerateRequest) -> dict[str, Any]:
    """重新生成关键帧（COMIC-123）：产出 v{n+1} 新版本，旧版保留可回退。"""
    return await keyframe_generate(req)


@router.post("/manga/keyframe/rollback")
def keyframe_rollback(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """关键帧回退（COMIC-124）：把指定版本置为当前版本。"""
    keyframe_id = str(body.get("keyframe_id") or "").strip()
    if not keyframe_id:
        raise ApiError(40008, "缺少 keyframe_id")
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法回退关键帧")
    kf = db.query_one(f"SELECT {_KF_COLS} FROM keyframes WHERE id=?",
                      (keyframe_id,))
    if kf is None:
        raise ApiError(40005, "关键帧不存在", detail={"keyframe_id": keyframe_id})
    db.update("keyframes", {"is_current": 0}, "row_id=?", (kf["row_id"],))
    db.update("keyframes", {"is_current": 1}, "id=?", (keyframe_id,))
    return ok({"row_id": kf["row_id"], "keyframe_id": keyframe_id,
               "version": int(kf.get("version", 1))})


@router.delete("/manga/keyframe/{keyframe_id}")
def keyframe_delete(keyframe_id: str) -> dict[str, Any]:
    """删除关键帧版本（COMIC-124）：删记录与文件；当前版本删除后自动回退到上一版本。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法删除关键帧")
    kf = db.query_one(f"SELECT {_KF_COLS} FROM keyframes WHERE id=?",
                      (keyframe_id,))
    if kf is None:
        raise ApiError(40005, "关键帧不存在", detail={"keyframe_id": keyframe_id})
    fp = DATA_DIR / (kf.get("file_path") or "")
    if kf.get("file_path") and fp.is_file():
        try:
            fp.unlink()
        except OSError as exc:
            log.warning("关键帧文件删除失败: %s", exc, exc_info=True)
    db.delete("keyframes", "id=?", (keyframe_id,))
    if kf.get("is_current"):
        prev = db.query_one(
            f"SELECT {_KF_COLS} FROM keyframes WHERE row_id=?"
            " ORDER BY version DESC LIMIT 1", (kf["row_id"],))
        if prev is not None:
            db.update("keyframes", {"is_current": 1}, "id=?", (prev["id"],))
    return ok({"keyframe_id": keyframe_id, "deleted": True})


@router.get("/manga/keyframe/list")
def keyframe_list(row_id: str = Query(...),
                  limit: int = Query(100, ge=1, le=500,
                                     description="返回条数上限"),
                  offset: int = Query(0, ge=0, description="分页偏移")) -> dict[str, Any]:
    """分镜行关键帧版本列表（版本倒序）。

    审计 R3-P3：增加 limit/offset 分页（默认 100、上限 500），
    total 维持「该分镜行版本总数」语义，向后兼容。
    """
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法查询关键帧")
    _ensure_source_mode_column(db)
    total_row = db.query_one(
        "SELECT COUNT(*) AS c FROM keyframes WHERE row_id=?", (row_id,))
    total = int(total_row["c"]) if total_row else 0
    rows = db.query(
        f"SELECT {_KF_COLS} FROM keyframes WHERE row_id=?"
        " ORDER BY version DESC LIMIT ? OFFSET ?", (row_id, limit, offset))
    items = [_kf_row_to_dict(r) for r in rows]
    return ok({"row_id": row_id, "items": items, "total": total})


# ═══════════════════════════════════════════════════════════════════
#  批 1.7 其余端点（COMIC-070/090/105/131/135/136/139）
# ═══════════════════════════════════════════════════════════════════

# 情绪规则词典（对话引擎未就绪时的本地分类回退）
_EMOTION_KEYWORDS = {
    "喜悦": ("笑", "高兴", "开心", "快乐", "喜", "哈哈", "棒", "好耶"),
    "愤怒": ("怒", "生气", "愤怒", "可恶", "混蛋", "气死", "滚"),
    "悲伤": ("哭", "伤心", "难过", "悲", "泪", "痛苦", "失去"),
    "惊讶": ("惊", "竟然", "居然", "什么", "怎么", "不会吧", "天啊"),
    "恐惧": ("怕", "恐怖", "害怕", "吓", "危", "救命"),
    "温柔": ("温柔", "轻", "柔", "抱", "安慰", "乖"),
}


@router.post("/manga/story/keyframe")
async def story_keyframe_generate(req: StoryKeyframeRequest) -> dict[str, Any]:
    """故事生图（解说漫剧第 4 步）：跨分镜一致性风格图。

    以故事线为单位生图：第 1 张用文生图，后续分镜以第 1 张为参考（img2img
    或统一 seed+风格前缀 保证一致性）。无 IP-Adapter 时降级为 seed 一致性。
    """
    _, _, rows = _load_project_rows(req.project_id)
    if not rows:
        raise ApiError(40008, "项目无分镜行")

    target_ids = set(req.row_ids) if req.row_ids else {r["id"] for r in rows}
    if req.scope == "missing":
        targets = [r for r in rows if r["id"] in target_ids
                   and (r.get("description") or "").strip()]
    else:
        targets = [r for r in rows if r["id"] in target_ids
                   and (r.get("description") or "").strip()]
    if not targets:
        return ok({"succeeded": [], "failed": [], "success_count": 0,
                   "total": 0, "degraded": False})

    # 解析分辨率（默认 2560×1440，出图统一规格）
    res_map = {"2560x1440": (IMG_TARGET_W, IMG_TARGET_H),
               "1024x1024": (1024, 1024), "1024x576": (1024, 576),
               "576x1024": (576, 1024)}
    w, h = res_map.get(req.resolution, (IMG_TARGET_W, IMG_TARGET_H))

    # 逐行生成（复用 keyframe_generate 核心逻辑）；2026-09-02 图像队列：
    # 整批=一个队列任务，paint 锁/收尾协商由队列统一编排
    def _story_runner(task: dict, check_cancel: Callable[[], None]) -> dict:  # noqa: ARG001
        succeeded: list[dict] = []
        failed: list[dict] = []
        for r in targets:
            try:
                data = _generate_keyframe_sync(
                    str(r["id"]), req.project_id, "", w, h)
                succeeded.append(data)
            except ApiError as exc:
                failed.append({"row_id": str(r["id"]), "code": exc.code,
                               "message": exc.message})
            except Exception as exc:  # noqa: BLE001
                failed.append({"row_id": str(r["id"]),
                               "code": "PAINT_GENERATION_FAILED",
                               "message": str(exc)[:300]})
        return {"succeeded": succeeded, "failed": failed,
                "success_count": len(succeeded), "total": len(targets),
                "degraded": False, "degrade_reason": ""}

    return ok(await get_image_queue().submit_and_wait({
        "task_id": f"kfstory:{req.project_id[:12]}:{time.time_ns():x}",
        "kind": "keyframe_batch", "runner": _story_runner,
        "loop": asyncio.get_running_loop()}))
