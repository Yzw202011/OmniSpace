"""H3 链式引擎：漫剧分镜行 → V10 全能多图参考 Chain → 单条成片（2026-08-30）。

把漫剧工作台的"一镜"(分镜行)接入内置 ComfyUI 的全能参考链式工作流：
  分镜行(描述词+人物/场景/道具绑定) → 六段式计划 JSON → Chain API 图
  → ComfyUI /prompt → 轮询 → 成片回搬 → 交给调用方入库。

设计要点：
  - API 图模板 = 一次成功运行的历史提取（h3_chain_workflow_api.json，
    34 节点，与前端排队提交完全一致），运行时仅替换 plan_json /
    run_name / 宽高 / LoadImage 参考路径 / 成片文件名；
  - 复用 H3Engine 的进程托管/提交/轮询/显存闸门（与导演台同锁）;
  - 参考图来自 comic_assets 真源，按"角色→场景→道具"排序映射
    <Picture 1..N>（≤7，与模板槽位一致；槽 8/9 预留）;
  - 质量档位：480p(864×480, 每镜≤15s) / 720p(1344×768, 每镜≤8s)
    ——16G 显存实测矩阵（2026-08-30）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from ...config import DATA_DIR, get_config
from ...middleware.error_handler import ApiError
from .comfy_proc import COMFY_INPUT_DIR as _COMFY_INPUT
from .h3_engine import _COMFY_OUTPUT, align_h3_frames, get_h3_engine

log = logging.getLogger("omnispace.inference.h3_chain")

# 2026-09-02 修复：comfy_proc 启动参数已把 input/output 重定向到
# data/comfyui/（08-31 生命周期治理），h3_engine 已同步改 import，
# 本引擎漏改仍写死旧安装目录 tools/.../ComfyUI/input——参考图拷入
# 旧目录而运行中的 ComfyUI 读 data/comfyui/input，链式提交必报
# 「Invalid image file」（E2E 双镜实测揪出）。统一从 comfy_proc 取真源。

_TEMPLATE = Path(__file__).parent / "h3_chain_workflow_api.json"

# B6 步2（2026-09-14）：链式管线权重名单源。渲染时注入 workflow json
# 的 5 处 Loader 节点——json 内同名值自此降级为占位默认，改名只动这里
# （消「代码与模板双写、改名必炸其一」的克隆漂移）。注意与 h3_engine
# ._H3_FILES 的关系：那是普通单镜管线的名单（nvfp4 代差），两套管线
# 权重代差不同、各自单源，非双写关系。
_CHAIN_H3_FILES = {
    "unet": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    "clip": "qwen3vl_32b_minimax_h3_int4_convrot.safetensors",
    "video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    "turbo_lora": "minimax_h3_turbo_v4_step600_ema.safetensors",
}


def _load_template_text() -> str:
    """API 图模板文本：优先解密金库（发行包内），开发环境回退明文。

    （P6 锁4：发行包内模板以 assets_enc/h3_chain_workflow_api.json.enc
    存在，明文由 make_dist 在出包时移除；金库密钥由发行公钥派生。）
    """
    from ...asset_vault import read_asset
    blob = read_asset("h3_chain_workflow_api.json.enc")
    if blob is not None:
        return blob.decode("utf-8")
    return _TEMPLATE.read_text(encoding="utf-8")
_QUALITY = {"480p": (864, 480), "720p": (1344, 768),
            "960p": (1728, 960), "1440p": (2688, 1536)}
# 双采加速产线（2026-10-24 用户拍板，参考二开大神 Deferred Upscale +
# HF 讨论#21 @ANe5s turbo 双采配方）：新档位 = 半分辨率 turbo 粗采
# （4/6/8 步）→ LatentUpscaler3D ×2 放大 → 低σ精修（模型换 0.7 低强度
# LoRA——>0.7 涂抹、<0.69 无效）。480p 档出 ~960p、720p 档出 ~1440p，
# 采样价不变。1440p pass2/解码 16G 卡偏险，先 960p 主验。
_TURBO2P_BASE = {"960p": (864, 480), "1440p": (1344, 768)}
_MAX_REFS = 7  # 与模板槽位一致（引擎上限 9,槽 8/9 预留）


def _split_names(value: Any) -> list[str]:
    """分镜行绑定字段 → 名单（容忍 JSON 数组/顿号/逗号/换行分隔）。"""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed if str(v).strip()]
    except Exception:  # noqa: BLE001 - 非 JSON 按分隔符切
        log.debug("_split_names: 降级忽略", exc_info=True)
    return [p.strip() for p in re.split(r"[、,，;；\n]+", text) if p.strip()]


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度(绑定名 vs 资产名的模糊匹配打分)。"""
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best


def _parse_id_list(v: Any) -> list[str]:
    """asset_ids 兼容解析：list / JSON 字符串 / 逗号分隔（对齐 keyframe
    的 _fetch_bound_assets 三形态兼容）。"""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:  # noqa: BLE001 - 非合法 JSON 按逗号分隔兜底
            log.debug("_parse_id_list: 降级忽略", exc_info=True)
        return [s.strip() for s in v.split(",") if s.strip()]
    return []


def _collect_refs(row: dict, row_id: str) -> list[dict]:
    """按 角色→场景→道具 收集参考（comic_assets 真源，≤7）。

    P0 修复（2026-08-31）：绑定真源 = storyboard_rows.asset_ids（资产
    id 列表，关键帧链路 _fetch_bound_assets 同源）；此前只读
    characters/scene/props 名称冗余列——ai-describe 重建/写入的行该
    三列为空（实测 row_1788130850860：asset_ids 4 项全带图而三列全
    空）→ 「均未绑定带图资产」误拒，视频生成即失败。现改为 asset_ids
    批查优先（带图、kind 优先级、同项目优先），名称列 LCS 模糊匹配
    (≥2 字)保留为旧数据兜底。
    """
    from ...data.database import get_db_safe

    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用(参考图收集)")
    refs: list[dict] = []
    used: set[str] = set()
    # 分镜行 → 所属项目(同项目资产优先,防跨项目风格错位)
    proj = db.query_one(
        "SELECT s.project_id AS pid FROM storyboard_rows r "
        "JOIN storyboards s ON r.storyboard_id=s.id WHERE r.id=?",
        (row_id if isinstance(row_id, str) else str(row_id),))
    proj_id = (proj or {}).get("pid")

    # ① asset_ids 真源：id 直查带图资产，character→scene→prop 优先级、
    #    同项目优先排序后截取
    ids = _parse_id_list(row.get("asset_ids"))
    if ids:
        placeholders = ",".join("?" * len(ids))
        try:
            picked = list(db.query(
                f"SELECT name, kind, file_path, project_id FROM comic_assets "
                f"WHERE id IN ({placeholders}) AND file_path!=''", ids))
        except Exception:  # noqa: BLE001 - 查询失败回落名称列兜底
            picked = []
        kind_pri = {"character": 0, "scene": 1, "prop": 2}
        picked.sort(key=lambda a: (kind_pri.get(a["kind"], 9),
                                   0 if proj_id and a["project_id"] == proj_id else 1))
        for a in picked:
            if len(refs) >= _MAX_REFS:
                break
            if a["name"] in used:
                continue
            used.add(a["name"])
            refs.append({"kind": a["kind"], "name": a["name"],
                         "src": DATA_DIR / a["file_path"]})

    # ② 名称列兜底（旧数据/手工绑定行）：最长公共子串模糊匹配(≥2 字),
    #    行里写"行李箱"能命中资产"黑色行李箱"
    for kind, bindings in (("character", _split_names(row.get("characters"))),
                           ("scene", _split_names(row.get("scene"))),
                           ("prop", _split_names(row.get("props")))):
        if len(refs) >= _MAX_REFS:
            break
        assets = [(r["name"], r["file_path"],
                   1 if proj_id and r["project_id"] == proj_id else 0)
                  for r in db.query(
            "SELECT name, file_path, project_id FROM comic_assets "
            "WHERE kind=? AND file_path!=''", (kind,))]
        for name in bindings:
            if len(refs) >= _MAX_REFS:
                break
            best, best_key = None, (0, 0)
            for aname, apath, same_proj in assets:
                if aname in used:
                    continue
                key = (_lcs_len(name, aname), same_proj)
                if key > best_key:
                    best, best_key = (aname, apath), key
            if best and best_key[0] >= 2:
                used.add(best[0])
                refs.append({"kind": kind, "name": best[0],
                             "src": DATA_DIR / best[1]})
    return refs


def _split_abc(description: str) -> dict[str, str]:
    """按段首标记「A.」「B.」「C.」切分分镜描述词。

    标记位置容忍两种（2026-08-31 镜3 实测修复）：行首，或紧跟句读
    （。！？；;）之后——AI 生成的描述词常把「。B.高密度世界观构建：」
    写在同一行，只认行首会令 B 段解析为空、世界观整段塌进 A 段并携带
    原始标记。标记字母必须前置合法（行首/句读后），词中字母（如
    "3D CG"）不会误命中。返回 {"a","b","c"}；无标记的纯文本描述三段
    均为空（调用方兜底原文）。
    """
    import re
    text = description.strip()
    marks = []
    for m in re.finditer(r"[ABC]\s*[.、．]", text):
        prefix = text[:m.start()].rstrip()
        if prefix and prefix[-1] not in "。！？；;\n":
            continue
        marks.append((m.group(0)[0].lower(), m.start(), m.end()))
    marks.sort(key=lambda t: t[1])
    # 段边界 = 下一个**任意**标记（含重复键——三轮挖掘④：双 B 段场景
    # 旧版把第二个 B 的标记与内容整块并入首 B 段）；重复键的段内容丢弃
    out: dict[str, str] = {"a": "", "b": "", "c": ""}
    for i, (key, _s, content_start) in enumerate(marks):
        seg_end = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        if out[key]:
            continue  # 同键取首段，重复段（复制粘贴残留）丢弃
        out[key] = text[content_start:seg_end].strip()
    return out


def _timeline_window(c_text: str, seconds: float) -> str:
    """C 段时间轴按时长取窗（2026-08-31 用户反馈「视频与描述词不符」根因）。

    描述词的 C 段是整行完整时间轴（如 0.1-10s 三镜），而单次链式生成
    只有 N 秒——整段塞入会稀释镜头语言。按镜头块**起点**判窗：起点早于
    生成时长的块保留（跨窗块演绎其前半），起点在时长之后的块丢弃；
    无标记/解析失败/全部超窗时返回整段（诚实兜底，宁可多演不可漏演）。
    """
    import re
    if not c_text:
        return ""
    blocks = re.split(r"(?=\[\d+(?:\.\d+)?s\s*[-~]\s*\d+(?:\.\d+)?s\])", c_text)
    header, timed = blocks[0], blocks[1:]
    if not timed:
        return c_text
    kept, dropped = [], 0
    for blk in timed:
        m = re.match(r"\[(\d+(?:\.\d+)?)s\s*[-~]\s*(\d+(?:\.\d+)?)s\]", blk.strip())
        if m and float(m.group(1)) >= seconds:
            dropped += 1
            continue
        kept.append(blk.strip())
    if not kept:
        return c_text
    note = (f"\n（本镜仅演绎前 {seconds:.0f} 秒内的镜头，其后续镜头另行生成）"
            if dropped else "")
    head = header.strip()
    return (head + "\n" if head else "") + "\n".join(kept) + note


def _parse_c_shots(c_text: str, seconds: float) -> tuple[list[dict], str]:
    """C 段结构化解析（2026-10-02 三轮挖掘：判定/渲染统一单源）。

    _six_section（渲染）与 _chain_audio_spec（静音判定）此前两套逻辑
    ——旧正则判定会被首帧保持段的「音效：无」污染（主拍未标注也被误
    判强制静音）。现统一：返回 (shots, header_note)。

    - shots 每项 {start,end,text,cam,dlg,sfx,spans}；首帧保持段
      （start≤0.01 且 end≤0.5）跳过；spans=画面语义超出生成时长
      （组装方须注「仅演绎前半」）。
    - header_note = 首个时间标记前的实义正文（纯「分镜时间轴」标签
      行剥除；旧版整块丢弃——「本镜为夜戏」类前置说明会静默丢失）。
    """
    c_win = _timeline_window(c_text, seconds)
    blocks = re.split(
        r"(?=\[\d+(?:\.\d+)?s\s*[-~]\s*\d+(?:\.\d+)?s\])", c_win)
    header_lines = [ln for ln in blocks[0].strip().splitlines()
                    if ln.strip() and "分镜时间轴" not in ln]
    header_note = "\n".join(header_lines).strip()
    shots: list[dict] = []
    for blk in blocks[1:]:
        m = re.match(
            r"\[(\d+(?:\.\d+)?)s\s*[-~]\s*(\d+(?:\.\d+)?)s\]", blk.strip())
        if not m:
            continue
        _st, _en = float(m.group(1)), float(m.group(2))
        if _st <= 0.01 and _en <= 0.5:
            continue  # 首帧保持段（协议 artifact）
        text = cam = sfx = ""
        dlg: list[str] = []  # 四轮挖掘②：同拍多台词行收集（旧版单值覆盖丢前句）
        last_field = ""
        for part in re.split(r"[|\n]", blk[m.end():]):
            p = part.strip()
            if not p:
                continue
            if p.startswith("画面"):
                text = re.sub(r"^画面[：:]\s*", "", p)
                last_field = "text"
            elif p.startswith("运镜"):
                cam = re.sub(r"^运镜[：:]\s*", "", p)
                last_field = "cam"
            elif p.startswith("台词"):
                _d = re.sub(r"^台词[：:]\s*", "", p).strip()
                if _d:
                    dlg.append(_d)
                last_field = "dlg"
            elif p.startswith("音效"):
                sfx = re.sub(r"^音效[：:]\s*", "", p)
                last_field = "sfx"
            elif not text:
                text = p
                last_field = "text"
            elif last_field == "dlg":
                # 台词续行（2026-10-11 新格式变体实锤）：台词： 独立成行
                # （空内容）+对白在下一行（无字段前缀的裸说话人行）——
                # 原逻辑落到 not text 已被占/直接丢弃，台词整体丢失
                dlg.append(p)
                last_field = "dlg"
        # 四轮挖掘③：转场拍（画面「衔接上一镜头…」承上启下空拍）——
        # 每镜独立剧本看不到上一镜，引用即幻觉诱因；跳过（与 first_shot
        # 同口径）。全为转场拍时该拍保留文本剥离引用词（诚实兜底）
        if text.startswith("衔接"):
            text = ""
        if text or dlg:
            shots.append({"start": _st, "end": _en, "text": text,
                          "cam": cam, "dlg": dlg, "sfx": sfx,
                          "spans": _en > seconds + 0.01})
    if not shots:
        # 编号格式分支（描述词 v2 无时间码变体：「1. 画面：…」逐行）。
        # 此前落整段单拍兜底，「1. 画面：衔接…」连转场带实拍糊成一条，
        # 转场剥离/摘要首拍跳过双双失效（存量红测试
        # test_six_section_summary_skips_transition_shot 实锤，2026-10-12）。
        # 门卫：段数过半带字段前缀才按拍列表切（单段编号也放行——
        # 「1. 画面：…」+台词/音效双无的单镜合法；散文列举 0 前缀仍拦）
        num_segs = [s for s in re.split(r"(?:^|\n)\s*(?=\d+[\.、])",
                                        c_win.strip()) if s.strip()]
        _FIELD_PREFIXES = ("画面", "运镜", "台词", "音效")
        stripped = [re.sub(r"^\d+[\.、]\s*", "", s.strip()) for s in num_segs]
        if stripped and sum(
                s.startswith(_FIELD_PREFIXES) for s in stripped) * 2 >= len(stripped):
            for seg in stripped:
                text = cam = sfx = ""
                dlg_n: list[str] = []
                for part in re.split(r"[|\n]", seg):
                    p = part.strip()
                    if not p:
                        continue
                    if p.startswith("画面"):
                        text = re.sub(r"^画面[：:]\s*", "", p)
                    elif p.startswith("运镜"):
                        cam = re.sub(r"^运镜[：:]\s*", "", p)
                    elif p.startswith("台词"):
                        _d = re.sub(r"^台词[：:]\s*", "", p).strip()
                        if _d:
                            dlg_n.append(_d)
                    elif p.startswith("音效"):
                        sfx = re.sub(r"^音效[：:]\s*", "", p)
                    elif not text:
                        text = p
                if text.startswith("衔接"):
                    text = ""  # 转场拍：与时间码路径同口径剥离
                if text or dlg_n:
                    shots.append({"start": 0.0, "end": seconds, "text": text,
                                  "cam": cam, "dlg": dlg_n, "sfx": sfx,
                                  "spans": False})
    if not shots:  # 无有效拍块兜底：整段当单拍（诚实降级）
        shots = [{"start": 0.0, "end": seconds, "text": c_win, "cam": "",
                  "dlg": [], "sfx": "", "spans": False}]
    return shots, header_note


def _six_section(description: str, refs: list[dict], total_seconds: float) -> str:
    subj = []
    retain = []
    for i, r in enumerate(refs, 1):
        kind_zh = {"character": "角色形象", "scene": "场景", "prop": "道具"}[r["kind"]]
        subj.append(f"<Picture {i}> 是{kind_zh}`{r['name']}`的唯一视觉参考。")
        retain.append(f"<Picture {i}> {r['name']}: fully_preserved。")
    # 描述词结构化增强（2026-08-31）：此前 summary 仅截前 120 字（几乎
    # 只剩 A 段风格句），detailed_description 塞整行全量时间轴——单镜
    # N 秒生成被稀释成「画风对了、内容不符」。现按 A/B/C 切分并按时长
    # 窗口取 C 段镜头，世界观/镜头语言按结构分位注入。
    abc = _split_abc(description)
    # B 段标题行剥离（2026-10-25 边角一）：手写描述词 B 段常带栏目标题
    # 首行（如「高密度世界观构建」，短行无句读）——按句号截摘要时
    # 标题会混进 summary 与世界状态段；短无标点首行按标题剥除
    _b_text = abc["b"].strip()
    _b_lines = _b_text.splitlines()
    if len(_b_lines) > 1:
        _head = _b_lines[0].strip()
        if _head and len(_head) <= 16 and not re.search(r"[。，,；;：:]", _head):
            _b_text = "\n".join(_b_lines[1:]).strip()
    # B 段静默吞除（2026-10-26 用户拍板）：世界观散文是模型朗读稿源
    # （Whisper ASR 实锤逐句念 B 段），可拍要素 C 段已全覆盖——B 段
    # 不再进合同（summary 只剩首拍、[世界与人物状态] 整段省略）；
    # config manga.h3_keep_b_section=true 回退旧行为
    _keep_b = False
    try:
        _keep_b = bool((get_config().get("manga") or {})
                       .get("h3_keep_b_section", False))
    except Exception:  # noqa: BLE001 - 配置异常按吞除
        _keep_b = False
    if not _keep_b:
        _b_text = ""
    # 四轮挖掘④：纯文本（无 A/B/C 标记）——abc 全空时结构化路径产出
    # 空时间轴=原文整段丢失；直接原文兜底（教学头+原文，官方六段壳）
    _plain = not (abc["a"] or abc["b"] or abc["c"])
    # C 段结构化解析（三轮挖掘统一单源 _parse_c_shots）：保持段跳过/
    # 协议标签官方化/跨窗拍 spans 标注/标记前正文（header_note）保留
    shots_p, header_note = _parse_c_shots(abc["c"], total_seconds)
    # 摘要首拍：跳过转场拍（解析层已剥离「衔接」引用，双保险口径一致）
    first_shot = next((s["text"][:120] for s in shots_p
                       if s["text"] and not s["text"].startswith("衔接")), "")
    brief = "；".join(x for x in [
        _b_text.split("。", 1)[0][:120] if _b_text else "",
        first_shot,
    ] if x)
    # 台词/音效并集（结构化取值；dlg 为列表——同拍多台词全收）
    dlg_vals = [d for s in shots_p for d in s["dlg"]]
    sfx_vals = [s["sfx"] for s in shots_p if s["sfx"]]
    has_dialogue = any(v not in {"无", "无台词", "N/A", "-"} for v in dlg_vals)
    sfx_named = [v for v in sfx_vals
                 if v not in {"无", "无音效", "N/A", "-"}]
    # 台词官方标记化（2026-10-02 四报根因）：H3 靠 <d>[Chinese] …</d>
    # 标记识别「要说的话」，裸文本致台词/旁白混淆（B 段散文被念三报）。
    # 四轮挖掘①：S 编号绑定角色（refs 中 character 序号）——官方要求
    # speaker ID 跨镜稳定，旧版按镜内发声序=多角色场景跨镜漂移；
    # 非绑定角色按镜内出现序接在角色编号之后。
    _char_names = [r["name"] for r in refs if r.get("kind") == "character"]
    _extra_ids: dict[str, str] = {}
    _spk_seen: set[str] = set()

    def _mark_dialogue_str(raw: str) -> str:
        raw = (raw or "").strip()
        if raw in {"无", "无台词", "N/A", "-"}:
            return ""
        spk, manner, line = "", "", raw
        mm = re.match(r"^(.{1,12}?)（(.{1,12}?)）[：:]\s*(.+)$", raw)
        if mm:
            spk, manner, line = (mm.group(1).strip(),
                                 mm.group(2).strip(), mm.group(3).strip())
        else:
            mm = re.match(r"^(.{1,12}?)[：:]\s*(.+)$", raw)
            if mm:
                spk, line = mm.group(1).strip(), mm.group(2).strip()
            else:
                # 无说话人前缀的语气括号（2026-10-25 边角二）：如
                # 「（内心 OS）我只是路过啊…」——语气括号摘出归 manner；
                # 单主角绑定时台词归主角色（多主角无法归属维持匿名）
                mm = re.match(r"^（([^（）]{1,16}?)）\s*(.+)$", raw)
                if mm:
                    manner = mm.group(1).strip()
                    line = mm.group(2).strip()
                    if len(_char_names) == 1:
                        spk = _char_names[0]
        if not line:
            return ""
        if spk and spk in _char_names:
            sid = f"S{_char_names.index(spk) + 1}"
        elif spk:
            # 名称变体匹配（2026-10-25 边角二延伸）：描述词称「夏沐沐」、
            # 资产名「夏沐沐白裙」——精确匹配失败会把同一人拆成两个
            # 说话人编号（实测 S1/S2 双人假象）；互为包含且唯一时按
            # 同一角色绑定并归一到资产名
            _hits = [i for i, n in enumerate(_char_names)
                     if (spk in n) or (n in spk)]
            if len(_hits) == 1:
                sid = f"S{_hits[0] + 1}"
                spk = _char_names[_hits[0]]
            else:
                if spk not in _extra_ids:
                    _extra_ids[spk] = f"S{len(_char_names) + len(_extra_ids) + 1}"
                sid = _extra_ids[spk]
        else:
            sid = "S1"
        _spk_seen.add(sid)
        head = f"{spk} ({sid})" if spk else f"({sid})"
        if manner and re.search(r"OS|内心|独白|心声", manner, re.I):
            verb = "内心独白式地说"  # OS 类语气直译「OS地说」不通顺
        else:
            verb = f"{manner}地说" if manner else "说"
        return f"{head} {verb}: <d>[Chinese] {line}</d>"

    # 官方时间轴组装（[Shot n] + At 分:秒.毫秒 切镜戳；运镜独立指令行
    # 严格锚定；音效不进时间轴——由 overall_soundscape 统一表达）
    _t0 = shots_p[0]["start"]
    _tl_lines: list[str] = []
    for _i, _s in enumerate(shots_p):
        _seg = _s["text"].strip().rstrip("。")
        parts = [f"[Shot {_i + 1}] {_seg}"] if _i == 0 else []
        if _i > 0:
            _cut = max(0.0, _s["start"] - _t0)
            _mm, _rem = divmod(int(round(_cut * 1000)), 60000)
            parts.append(f"[Shot {_i + 1}] At {_mm:02d}:{_rem / 1000.0:06.3f}, "
                         f"the camera cuts to {_seg}")
        if _s["cam"]:
            parts.append(f"镜头运动：{_s['cam'].strip()}（严格执行，"
                         f"不得自行更改或增加镜头运动）")
        for _d_line in _s["dlg"]:  # 同拍多台词逐条标记（四轮挖掘②）
            _dlg_m = _mark_dialogue_str(_d_line)
            if _dlg_m:
                parts.append(_dlg_m)
        if _s.get("spans"):
            # 跨窗拍（三轮挖掘②：旧版尾注在结构化时丢失）——画面语义
            # 超出生成时长，明确告知只演窗内前半
            parts.append(f"（本拍仅演绎前 {max(0.0, total_seconds - _s['start']):.1f} 秒内内容）")
        _tl_lines.append("。".join(p for p in parts if p.rstrip("。")) + "。"
                         if parts else "")
    c_marked = "\n".join(x for x in _tl_lines if x)
    if header_note:
        c_marked = f"{header_note}\n{c_marked}"
    has_dialogue = bool(_spk_seen) or has_dialogue
    # A 段矛盾句中和（三轮挖掘③：精确串匹配漏变体）——任何「保留/含/
    # 包含 … 台词/配音/人声/旁白」句式的句子都可能与 C 段「无」打架
    _a_text = abc["a"][:400] if abc["a"] else ""
    if _a_text:
        _a_text = re.sub(
            r"[^。；;\n]*(?:保留|含有|包含)[^。；;\n]*(?:台词|配音|人声|旁白)[^。；;\n]*[。；;]?",
            "各镜音画以时间轴标注为准。", _a_text)
    detailed = "\n\n".join(x for x in [
        "严格按下方时间轴逐拍演绎；场景、光影与人物动作以描述词为准，"
        "参考图仅用于锁定人物与道具的外观既定形态。全局风格、世界与"
        "人物状态、画面描述均为视觉信息——绝不可作为旁白朗读、不可"
        "作为字幕、不可出任何人声念读；只有 <d>[Chinese] …</d> 标记"
        "内的话是被说出的台词。",
        f"[全局风格] {_a_text}" if _a_text else "",
        f"[世界与人物状态] {_b_text[:500]}" if _b_text else "",
        # 四轮挖掘④：纯文本描述词原文直入（结构化路径产出空时间轴=
        # 原文整段丢失）
        (description.strip()[:1500] if _plain else
         (f"[本镜时间轴（共 {total_seconds:.0f} 秒）]\n{c_marked[:1500]}"
          if c_marked else "")),
        ("[运镜铁律] 本镜固定机位：全程镜头无位移、无推拉摇移变焦，"
         "取景与构图保持不变。"
         if any(s["cam"] for s in shots_p)
         and all("固定" in s["cam"] for s in shots_p if s["cam"])
         else ""),
        ("[音频铁律] 本镜全程无角色台词、无任何人声说话、无字幕；"
         "音效为无（接近无声，仅极低房间底噪）。"
         if (sfx_vals and not sfx_named and not has_dialogue) else ""),
    ] if x)
    if sfx_vals and not sfx_named:
        soundscape = ("用户明确要求：本镜无音效、接近无声，"
                      "仅可能存在极低的房间底噪。")
    elif sfx_named:
        soundscape = "；".join(sfx_named) + "。"
    else:
        soundscape = "Quiet ambient atmosphere only; no prominent sound events."
    if not has_dialogue:
        soundscape += " 全程无角色台词、无任何人声说话、无字幕。"
    else:
        soundscape += " 角色台词已在时间轴中给出，按时间轴演绎。"
    return "\n".join([
        "subject_definitions:\n" + "\n".join(subj),
        "summary:\n[reference generation] " + (brief or description.strip()[:160]),
        "retention_analysis:\n" + "\n".join(retain) +
        "\n<Subject 1>: fully_preserved - 全程保持参考图既定外观与服饰。",
        "detailed_description:\n" + (detailed or description.strip()),
        f"overall_soundscape:\n{soundscape} 单镜时长约 {total_seconds:.0f} 秒。",
        "non_diegetic_music:\n无配乐。",
    ])


def h3_sharpen_enabled() -> bool:
    """全局锐化 pass 开关（config comfyui.h3_sharpen.enabled，2026-10-12）。"""
    try:
        from ...config import get_config
        return bool(((get_config().get("comfyui") or {})
                     .get("h3_sharpen") or {}).get("enabled", True))
    except Exception:  # noqa: BLE001 - 配置异常按开（锐化零阻断）
        return True


def _sharpen_strength() -> float:
    """锐化强度（CAS 0~1；A/B 定档 0.6——0.5 保守/0.7 进取，0.9 过锐）。"""
    try:
        from ...config import get_config
        return max(0.0, min(1.0, float(
            ((get_config().get("comfyui") or {})
             .get("h3_sharpen") or {}).get("strength", 0.6))))
    except Exception:  # noqa: BLE001
        return 0.6


def _sharpen_pass(video: Path, strength: float = 0.6,
                  progress_cb: Callable[[str], None] | None = None) -> Path:
    """全局 CAS 锐化 + 时序降噪（对比度自适应，光晕少于 unsharp）：整帧轻推

    边缘反差 + hqdn3d 时序降噪（帧间闪烁实测 MAD 8.2→7.3~7.7，量化
    2026-10-13）——修正原生直出在全屏播放下的感知软化与噪点。秒级
    ffmpeg 滤镜重编码（crf16 保码率），音轨 stream copy；失败零阻断
    回退原片。降噪+锐化合并为同一次重编码（零额外成本）。
    """
    try:
        from ..encoder_service import get_encoder_service  # type: ignore[import-untyped]
        enc = get_encoder_service()
        if not enc.available:
            log.warning("[h3_chain] 全局锐化跳过（FFmpeg 不可用），保留原片")
            return video
        if progress_cb is not None:
            progress_cb("CAS 锐化中")
        # 降噪前置（hqdn3d 时序+空间）→ CAS 锐化收尾：先清噪再提边缘，
        # 反序会把噪声一起放大
        vf = f"cas=strength={strength:.2f}"
        try:
            from ...config import get_config
            if ((get_config().get("comfyui") or {})
                    .get("h3_sharpen") or {}).get("denoise", True):
                vf = "hqdn3d=1.5:1.5:6:6," + vf
        except Exception:  # noqa: BLE001 - 配置异常按默认（含降噪）
            vf = "hqdn3d=1.5:1.5:6:6," + vf
        tmp = video.with_name(f"{video.stem}_sh.mp4")
        flags = (subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        r = subprocess.run(
            [enc.ffmpeg_path, "-y", "-i", str(video),
             "-vf", vf,
             "-c:v", "libx264", "-crf", "16", "-preset", "medium",
             "-c:a", "copy", str(tmp)],
            capture_output=True, text=True, timeout=600,
            creationflags=flags)
        if r.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 1000:
            shutil.move(str(tmp), str(video))
            log.info("[h3_chain] 全局 CAS 锐化完成 (strength=%.2f, vf=%s): %s",
                     strength, vf, video.name)
        else:
            log.warning("[h3_chain] 全局锐化失败（保留原片）: %s",
                        (r.stderr or "")[-200:])
    except Exception as exc:  # noqa: BLE001 - 锐化失败不阻断交付
        log.warning("[h3_chain] 全局锐化异常（保留原片）: %s", exc)
    return video


def _chain_audio_spec(descriptions: list[str], seconds: float) -> tuple[bool, bool, bool]:
    """多行链式的台词/音效并集判定 → (有台词, 有具名音效, 显式双无)。

    显式双无 = 至少一行 C 段的**实体拍**（首帧保持段已由解析层排除）
    同时写出台词：无 与 音效：无。任何一行具名台词/具名音效则不触发
    静音强制。三轮挖掘①：旧版对窗口原文跑正则——保持段的「音效：无」
    会污染判定（主拍未标注也被误判强制静音）；现走 _parse_c_shots
    与渲染同源。
    """
    _none = {"无", "无台词", "无音效", "N/A", "-"}
    has_dlg = False
    has_sfx = False
    explicit_mute = False
    for d in descriptions:
        abc = _split_abc(d)
        shots, _ = _parse_c_shots(abc["c"], seconds)
        dlg = [d for s in shots for d in s["dlg"]]
        sfx = [s["sfx"] for s in shots if s["sfx"]]
        dlg_named = any(v not in _none for v in dlg)
        sfx_named = any(v not in _none for v in sfx)
        has_dlg = has_dlg or dlg_named
        has_sfx = has_sfx or sfx_named
        if dlg and sfx and not dlg_named and not sfx_named:
            explicit_mute = True
    return has_dlg, has_sfx, explicit_mute


def _mute_audio_track(video: Path) -> Path:
    """音轨替换为静音（台词：无+音效：无 的文件级强制，2026-10-02）。

    提示词级禁令被模型三次无视（B 段散文被当旁白朗读，响度实测
    -14dB）——C 段显式双无时直接换 anullsrc 静音轨，100% 执行用户
    标注。视频流零拷贝（-c:v copy）；失败回退原片零阻断。
    """
    try:
        from ..encoder_service import get_encoder_service  # type: ignore[import-untyped]
        enc = get_encoder_service()
        if not enc.available:
            log.warning("[h3_chain] 静音替换跳过（FFmpeg 不可用），保留原音轨")
            return video
        tmp = video.with_name(f"{video.stem}_mute.mp4")
        flags = (subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        r = subprocess.run(
            [enc.ffmpeg_path, "-y", "-i", str(video),
             "-f", "lavfi",
             "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
             "-shortest", "-map", "0:v:0", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", str(tmp)],
            capture_output=True, text=True, timeout=120,
            creationflags=flags)
        if r.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 1000:
            shutil.move(str(tmp), str(video))
            log.info("[h3_chain] 音轨已替换为静音（台词：无+音效：无 文件级强制）")
        else:
            log.warning("[h3_chain] 静音替换失败（保留原音轨）: %s",
                        (r.stderr or "")[-200:])
    except Exception as exc:  # noqa: BLE001 - 静音失败不阻断交付
        log.warning("[h3_chain] 静音替换异常（保留原音轨）: %s", exc)
    return video


def _prepare_refs(task_id: str, refs: list[dict]) -> list[dict]:
    """参考图拷入 ComfyUI input 子目录,返回带 image 路径的引用。"""
    sub = Path(_COMFY_INPUT) / f"h3chain_{task_id}"
    sub.mkdir(parents=True, exist_ok=True)
    out = []
    for i, r in enumerate(refs, 1):
        dst = sub / f"p{i}.png"
        shutil.copy2(r["src"], dst)
        out.append({**r, "image": f"h3chain_{task_id}/p{i}.png"})
    return out


# （dual_clock 函数族已删——批5 A/B 收口 2026-09-18，A/B 测伪入库 a9e50ed）
# 引擎按编号注入参数的节点（缺号=注入静默落空→错图）。模板 JSON 被改动
# 致编号漂移时由 _render_graph 入口卡死（问题总账 #30 防呆，2026-09-19 Q6）。
_REQUIRED_NODES = frozenset({"1", "2", "3", "4", "110", "1700", "1701", "1706", "1961"})


def _render_graph(template: dict, plan_json: str, run_name: str,
                  width: int, height: int, refs: list[dict], task_id: str,
                  start_clip: int = 1, turbo: bool = True,
                  two_stage: bool = False,
                  carry_image: str | None = None,
                  two_pass: bool = False,
                  base_dims: tuple[int, int] | None = None,
                  same_res_refine: bool = False) -> dict:
    graph = json.loads(json.dumps(template))
    required = (set(_REQUIRED_NODES) if turbo
                else set(_REQUIRED_NODES) - {"1961"} | {"122"})
    missing = required - graph.keys()
    if missing:
        raise ApiError(
            "H3_WORKFLOW_TEMPLATE_BROKEN",
            f"H3 工作流模板缺节点 {sorted(missing)}——模板 JSON 被改动致节点"
            "编号漂移（引擎按编号注入参数，缺号=静默错图，宁可失败不出错片）",
            suggestion="对照 git 还原 h3_chain_workflow_api.json；若有意改模板，"
                       "须同步更新引擎 _REQUIRED_NODES 与注入代码")
    # 两阶段（2026-10-13 用户令）：720p 档用 480p 采样 + 神经 latent 上采样
    # + 低σ精修回 720p——时间砍半、画质同级（V1 生产实弹验证 h3v1_latent2stage）
    # 双采加速产线（2026-10-24 拍板）：960p/1440p 半分辨率粗采，画布由
    # base_dims 显式给定（480p/720p），输出=目标档（×2 放大）
    if base_dims is not None:
        base_w, base_h = base_dims
    else:
        base_w, base_h = (864, 480) if (two_stage and width >= 1280) \
            else (width, height)
    # B6 步2：权重名从 _CHAIN_H3_FILES 单源注入（json 内同名值=占位默认）
    graph["1"]["inputs"]["unet_name"] = _CHAIN_H3_FILES["unet"]
    graph["2"]["inputs"]["clip_name"] = _CHAIN_H3_FILES["clip"]
    graph["3"]["inputs"]["vae_name"] = _CHAIN_H3_FILES["video_vae"]
    graph["4"]["inputs"]["vae_name"] = _CHAIN_H3_FILES["audio_vae"]
    if turbo:
        graph["1961"]["inputs"]["lora_name"] = _CHAIN_H3_FILES["turbo_lora"]
    else:
        # 官方非 turbo 基线（对照官方 1、For循环版 20 步模板 / V5·V6 双采
        # 12,3 显式位移模板）：摘蒸馏 LoRA（1961）与 Turbo 采样器（1962），
        # 采样器换模板内休眠的 KSamplerSelect(122, res_multistep)——
        # shift 12/3 位移（节点 5）保留，与官方双采模板同构
        graph["5"]["inputs"]["model"] = graph["1961"]["inputs"]["model"]
        graph["124"]["inputs"]["sampler"] = ["122", 0]
        graph.pop("1961", None)
        graph.pop("1962", None)
    # （dual_clock 调用段已删——批5 A/B 收口 2026-09-18）
    p = graph["1700"]["inputs"]
    p["plan_json"] = plan_json
    p["run_name"] = run_name
    p["width"], p["height"] = base_w, base_h
    graph["1701"]["inputs"]["start_clip"] = start_clip
    graph["1706"]["inputs"]["filename"] = f"{run_name}_%date:yyyy-MM-dd%"
    # 参考图:动态识别模板里的参考 LoadImage 节点及其在 110 上的槽位键
    # (自动生长槽位的编号由前端决定,不能硬编码顺序)
    slot_map = []  # [(loadimage_nid, input_key)]
    for key, val in graph["110"]["inputs"].items():
        if key.startswith("ref_images.ref_image_") and isinstance(val, list):
            nid = str(val[0])
            if nid in graph and graph[nid]["class_type"] == "LoadImage":
                slot_map.append((nid, key))
    slot_map.sort(key=lambda x: int(x[0]))
    for k, (nid, key) in enumerate(slot_map):
        if k < len(refs):
            graph[nid]["inputs"]["image"] = refs[k]["image"]
        else:
            graph.pop(nid, None)
            graph["110"]["inputs"].pop(key, None)
    if carry_image:
        # 行级承接：尾帧 LoadImage → AddGuide 硬锚 frame_idx=0，
        # ChainContext(1703) 的 conditioning 输入改接 AddGuide 输出。
        # 运行时注入节点 8100/8101（模板零改动，不入 _REQUIRED_NODES
        # 防呆范围——无 carry_image 时渲染结果与模板逐字节等价，
        # 单测锁定）。AddGuide 按目标 latent 尺寸重缩放锚定图。
        graph["8100"] = {"class_type": "LoadImage",
                         "inputs": {"image": carry_image}}
        graph["8101"] = {"class_type": "MiniMaxH3AddGuide",
                         "inputs": {"positive": ["110", 0],
                                    "latent": ["110", 1],
                                    "vae": ["3", 0],
                                    "image": ["8100", 0],
                                    "frame_idx": 0}}
        graph["1703"]["inputs"]["conditioning"] = ["8101", 0]
    if two_stage:
        _insert_latent_refine(graph, base_w, base_h, width, height)
        if turbo:
            # turbo 双采版（2026-10-24 拍板，同图换配方）：粗采已是
            # turbo 步数（4/6/8），精修抄大神 Deferred Upscale 原方——
            # 仅 2 步 @ denoise 0.24、T8 尾段旁路（13 步精修会把 turbo
            # 省的时间吃回去）。图拓扑与 VRAM 包络不变（16G 实测安全）
            graph["V1sigmas"]["inputs"]["steps"] = 2
            graph["V1sigmas"]["inputs"]["denoise"] = 0.24
            graph["V1refine"]["inputs"]["sigmas"] = ["V1sigmas", 0]
    if same_res_refine:
        # 480p 同分辨率 σ 精修（turbo 双采 480p 档）：粗采后 2 步低σ
        # 精修、无放大（target=粗采画布）；VRAM 包络与 720p 两阶段同域
        _insert_latent_refine(graph, width, height, width, height)
        graph["V1sigmas"]["inputs"]["steps"] = 2
        graph["V1sigmas"]["inputs"]["denoise"] = 0.24
        graph["V1refine"]["inputs"]["sigmas"] = ["V1sigmas", 0]
    if two_pass:
        # 双采加速产线：粗采强制 turbo（pass1 走 1961@1.0 + 1962 采样器，
        # 步数 4/6/8）；精修抄大神 Deferred Upscale 原方——仅 2 步 @
        # denoise 0.24、T8 尾段节点旁路（sigmas 直连；13 步精修会把
        # turbo 省的时间全吃回去）。
        # （首跑实测修正：turbo 节点 bypass 不支持自身叠加——1963@0.7
        # 二次挂载直接 NoneType 崩，精修改用全强度 turbo 模型；0.7 低
        # 强度版留待核心 LoraLoader 路线验证）
        _insert_latent_refine(graph, base_w, base_h, width, height)
        graph["V1sigmas"]["inputs"]["steps"] = 2
        graph["V1sigmas"]["inputs"]["denoise"] = 0.24
        graph["V1refine"]["inputs"]["sigmas"] = ["V1sigmas", 0]
        # ×2 放大后放大器权重即卸（16G 卡实测：960p 精修窗口常驻=
        # ComfyUI 整进程崩；卸载后给精修采样/解码让路）
        graph["V1refine"]["inputs"]["offload_after_upscale"] = True
    return graph


def _insert_latent_refine(graph: dict, base_w: int, base_h: int,
                          target_w: int, target_h: int) -> None:
    """两阶段后处理：在采样器(124)输出与 VAEDecode(130/131) 之间插入

    MinimaxH3LatentUpscaler3DRefineHandoff——learned latent 上采样 +
    低σ H3 精修，输出直接可解码 AV latent（目标分辨率）。

    2026-10-17 用户拍板「双采+sig强化甜点」唯一产链：精修参数从
    dn0.3 无尾段升级为 dn0.45 + 尾段+3（贪心扫荡定谳的唯一有效
    旋钮组合，详见记忆 h3-quality-techlines-2026-10-16）。
    """
    graph["V1noise"] = {"class_type": "RandomNoise", "inputs": {
        "noise_seed": 42}}
    graph["V1sampler"] = {"class_type": "KSamplerSelect", "inputs": {
        "sampler_name": "res_multistep"}}
    graph["V1sigmas"] = {"class_type": "BasicScheduler", "inputs": {
        "scheduler": "simple", "steps": 10, "denoise": 0.45,
        "model": ["5", 0]}}
    # 尾段细化（+3 步渐降，低σ细节成形期加密）：SIGMAS 级纯调度无参考
    # 门槛；profile 必须 custom_strict（默认 turbo_standard8 按 8 步校验
    # 会拒绝本调度）
    graph["V1tail2"] = {
        "class_type": "MiniMaxH3AVTailDetailScheduleT8Advanced",
        "inputs": {
            "sigmas": ["V1sigmas", 0],
            "extra_tail_steps": 3,
            "spacing": "video_sigma_linear",
            "shift_video": 12.0,
            "shift_audio": 3.0,
            "profile": "custom_strict",
        }}
    graph["V1refine"] = {
        "class_type": "MinimaxH3LatentUpscaler3DRefineHandoff",
        "inputs": {
            "latent": ["124", 0],
            "noise": ["V1noise", 0],
            "sampler": ["V1sampler", 0],
            "sigmas": ["V1tail2", 0],
            "model": ["5", 0],
            "positive": ["1703", 0],
            "model_name": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
            "mode": "target dimensions",
            "scale": 2.0,
            "width": target_w,
            "height": target_h,
            "megapixels": 1.0,
            "align": 32,
            "keep_proportion": True,
            "lock_audio": True,
            "cfg": 1.0,
            "device": "cuda",
            "precision": "fp16",
            "offload_after_upscale": False,
        }}
    graph["130"]["inputs"]["samples"] = ["V1refine", 0]
    graph["131"]["inputs"]["samples"] = ["V1refine", 0]


# ═══════════════════════════════════════════════════════════════════
#  行级承接（2026-10-24 用户拍板）：行描述词 C 段带「承接上一镜头」类
#  标记 → 单镜运行时取上一行最新成功视频的尾帧，AddGuide 硬锚
#  frame_idx=0 为本镜起始帧。识别只扫 C 段、词表收紧（动词+上一镜/
#  前一镜宾语），防 B 段世界观散文同形词误触发；上一镜取不到/画幅比
#  不一致/FFmpeg 缺失一律回退独立生成（用户拍板），绝不阻断出片。
#  注入点：ChainContext(1703) 的 conditioning 输入改接 AddGuide——
#  ChainContext 对 scene 1 透传 conditioning，硬锚精确作用于首镜；
#  scene 2+ 会继承 keyframe，故仅 len(rows)==1 时注入（连播运行的
#  镜间连贯由链式上下文内建机制负责）。
# ═══════════════════════════════════════════════════════════════════
_CARRY_MARKER_RE = re.compile(
    r"(?:承接|紧接|衔接|接续|延续)(?:自|于)?[上前]一?[个]?\s*镜[头]?")


def detect_carry_marker(description: str) -> bool:
    """行描述词 C 段带「承接上一镜头」类标记 → True（视频侧承接触发）。"""
    c_text = _split_abc(description or "")["c"]
    return bool(_CARRY_MARKER_RE.search(c_text))


def _carry_config_enabled() -> bool:
    """承接总开关（config manga.h3_carry_enabled，默认开）。"""
    try:
        return bool((get_config().get("manga") or {})
                    .get("h3_carry_enabled", True))
    except Exception:  # noqa: BLE001 - 配置异常按默认开
        return True


def _find_prev_row_video(db: Any, row_id: str) -> str:
    """同分镜表内按 sort_index 紧邻上一行的最新成功视频；取不到 ""。"""
    if db is None:
        return ""
    try:
        cur = db.query_one(
            "SELECT storyboard_id, sort_index FROM storyboard_rows "
            "WHERE id=?", (row_id,))
        if not cur or not (cur.get("storyboard_id") or "").strip():
            return ""
        prev = db.query_one(
            "SELECT id FROM storyboard_rows WHERE storyboard_id=? "
            "AND sort_index<? ORDER BY sort_index DESC LIMIT 1",
            (cur["storyboard_id"], cur.get("sort_index") or 0))
        if not prev:
            return ""
        vt = db.query_one(
            "SELECT file_path FROM video_tasks WHERE storyboard_row_id=? "
            "AND status='done' AND file_path IS NOT NULL AND file_path!='' "
            "ORDER BY created_at DESC LIMIT 1", (prev["id"],))
        p = Path(str((vt or {}).get("file_path") or ""))
        if not p.is_absolute():
            p = DATA_DIR / p
        return str(p) if p.is_file() else ""
    except Exception:  # noqa: BLE001 - 查询失败按无上行回退
        log.warning("[h3_chain] 承接: 上一行视频查询失败", exc_info=True)
        return ""


def _extract_carry_frame(video_path: str, dst: Path,
                         width: int, height: int) -> bool:
    """ffmpeg 抽上一镜尾帧 PNG；横竖屏跨切（画幅比偏差 >5%）→ False。

    分辨率不预对齐——AddGuide 按目标 latent 尺寸 _resize 锚定图，
    只把画幅比不一致的（硬锚黑边/裁切无意义）挡在门外（用户拍板回退）。
    """
    from PIL import Image

    from ..encoder_service import get_encoder_service
    try:
        enc = get_encoder_service()
        if not enc.available:
            return False
        flags = (subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        r = subprocess.run(
            [enc.ffmpeg_path, "-y", "-sseof", "-0.1", "-i", video_path,
             "-frames:v", "1", "-update", "1", "-q:v", "1", str(dst)],
            capture_output=True, text=True, timeout=60,
            creationflags=flags)
        if r.returncode != 0 or not dst.is_file() \
                or dst.stat().st_size == 0:
            return False
        with Image.open(dst) as img:
            vw, vh = img.size
        if vh <= 0:
            return False
        # 容差 5%（2026-10-24 段2 实弹修正）：脸修复链会把 1344 宽垫到
        # 1376（32 对齐，偏差 2.38%）——2% 护栏误杀真承接；护栏本意只挡
        # 横竖屏跨切（竖屏 0.56 vs 横屏 1.75 偏差 68%），5% 足够分辨
        target_ratio = width / height
        return abs((vw / vh) - target_ratio) <= 0.05 * target_ratio
    except Exception:  # noqa: BLE001 - 提取失败按回退处理
        log.warning("[h3_chain] 承接: 尾帧提取失败 %s", video_path,
                    exc_info=True)
        return False


def run_h3_chain_task(task_id: str, row_ids: list[str], seconds: float,
                      quality: str, start_clip: int = 1, run_name: str = "",
                      loop: asyncio.AbstractEventLoop | None = None,
                      progress_cb: Callable[[float, str], None] | None = None,
                      aspect: str = "16:9",
                      check_cancel: Callable[[], None] | None = None,
                      keep_loaded: bool = False,
                      steps_override: int | None = None,
                      turbo_override: bool | None = None,
                      refine_pass: bool | None = None) -> dict:
    """链式生成主流程（阻塞,供视频队列 worker 调用）。

    row_ids: 多个分镜行 → 计划多镜(参考取各行绑定并集,≤7);
    start_clip>1: 断点续跑(沿用同 run_name 的已完成镜检查点);
    progress_cb(frac, stage) 用于回写 video_tasks.progress;
    aspect: "16:9"|"9:16"（2026-08-31 模型配置接线：默认画幅此前从不
    进请求，宽高按画幅翻转）;
    check_cancel: 取消检查点（2026-09-02 视频队列接线）——提交前与
    采样轮询（3s 一查）调用，命中抛调用方异常并 best-effort POST
    ComfyUI /interrupt 止损（此前排队/采样中取消完全无效，空跑整镜）;
    keep_loaded: 队列还有后继 H3 任务时跳过收尾卸载，镜间接力免整轮
    权重重载（此前前端逐镜接力每镜都重载 H3 权重）。
    返回 {"file_path": 绝对路径, "resolution": "WxH", "duration_seconds": s}。
    """
    from ...data.database import get_db_safe

    def _cancel_check() -> None:
        if check_cancel is not None:
            check_cancel()

    def _cancel_interrupt(engine_ref: Any) -> None:
        """取消时 best-effort 中断 ComfyUI 当前 prompt（止损 GPU）。"""
        try:
            engine_ref._api("POST", "/interrupt", timeout=5.0)
        except Exception:  # noqa: BLE001 - 中断失败不影响取消主流程
            log.debug("_cancel_interrupt: 降级忽略", exc_info=True)

    if quality not in _QUALITY:
        raise ApiError("VIDEO_GENERATION_FAILED", f"未知画质档位 {quality}")
    width, height = _QUALITY[quality]
    # 双采加速产线：半分辨率粗采画布（竖屏同翻转）
    _base_dims = _TURBO2P_BASE.get(quality)
    if aspect == "9:16":
        width, height = height, width  # 模型配置·默认画幅接线（竖屏）
        if _base_dims:
            _base_dims = (_base_dims[1], _base_dims[0])
    engine = get_h3_engine()
    run_name = run_name or f"h3chain_{task_id}"

    def report(frac: float, stage: str) -> None:
        if progress_cb:
            try:
                progress_cb(frac, stage)
            except Exception:  # noqa: BLE001
                log.debug("report: 降级忽略", exc_info=True)

    # 1. 分镜行 + 参考收集(多镜取并集,同项目优先去重;库瞬时锁重试)
    db = get_db_safe()
    rows: list[dict] = []
    for _attempt in range(3):
        try:
            if db is None:
                db = get_db_safe()
            if db is None:
                time.sleep(1.0)
                continue
            for rid in row_ids:
                r = db.query_one(
                    "SELECT description, characters, scene, props, asset_ids "
                    "FROM storyboard_rows WHERE id=?", (rid,))
                if r and (r.get("description") or "").strip():
                    rows.append({**r, "id": rid})
            break
        except Exception:  # noqa: BLE001 - sqlite busy 瞬时错误重试
            time.sleep(1.0)
    if not rows:
        raise ApiError("SYSTEM_RESOURCE_NOT_FOUND", f"分镜行不存在或描述词为空: {row_ids}")
    seen: set[tuple] = set()
    refs: list[dict] = []
    for r in rows:
        for ref in _collect_refs(r, r["id"]):
            key = (ref["kind"], ref["name"])
            if key not in seen and len(refs) < _MAX_REFS:
                seen.add(key)
                refs.append(ref)
    if not refs:
        raise ApiError("VIDEO_GENERATION_FAILED", "所选分镜行均未绑定带图资产(人物/场景/道具)")

    # 2. 参考图落盘 + 计划(每镜一 shot,长度=17k+5)
    report(0.02, "准备参考图")
    refs = _prepare_refs(task_id, refs)
    # 角色参考图读入内存（脸修复身份锚）——必须在采样段 finally 的
    # 参考图目录 rmtree 之前读（上一版插在收片后=图已被删=恒跳过）
    ref_imgs: list = []
    for r in refs:
        if r.get("kind") != "character":
            continue
        fp = Path(_COMFY_INPUT) / str(r.get("image") or "")
        if fp.is_file():
            try:
                from PIL import Image as _Img
                ref_imgs.append(_Img.open(fp).convert("RGB"))
            except Exception:  # noqa: BLE001 - 单图失败不拦参考收集
                pass
        if len(ref_imgs) >= 2:
            break
    # 行级承接解析（单镜运行 + 标记 + 总开关开）——须在 _prepare_refs
    # 建好输入目录之后：尾帧 PNG 落同目录，随 finally 统一回收
    carry_image: str | None = None
    if _carry_config_enabled():
        if len(rows) == 1 and detect_carry_marker(rows[0]["description"]):
            _carry_note = ""
            try:
                _prev_video = _find_prev_row_video(db, rows[0]["id"])
                _carry_dir = Path(_COMFY_INPUT) / f"h3chain_{task_id}"
                _carry_dir.mkdir(parents=True, exist_ok=True)
                _carry_png = _carry_dir / "carry_tail.png"
                if not _prev_video:
                    _carry_note = "上一镜无已完成视频"
                elif _extract_carry_frame(_prev_video, _carry_png,
                                          width, height):
                    carry_image = f"h3chain_{task_id}/carry_tail.png"
                    report(0.03, "已锚定上一镜尾帧为起始帧")
                else:
                    _carry_note = "尾帧提取失败或画幅不一致"
            except Exception as exc:  # noqa: BLE001 - 承接失败不阻断出片
                _carry_note = f"承接解析异常: {exc}"
            if _carry_note:
                log.warning("[h3_chain] 承接未生效(回退独立生成): row=%s %s",
                            rows[0]["id"], _carry_note)
        elif len(rows) > 1:
            _marked = [r["id"] for r in rows
                       if detect_carry_marker(r["description"])]
            if _marked:
                log.info(
                    "[h3_chain] 连播运行 %d 行带承接标记，镜间连贯由链式"
                    "上下文内建机制负责（跨运行尾帧锚定仅单行模式生效）: %s",
                    len(rows), _marked)
    shots: list[dict] = []
    for k, r in enumerate(rows, 1):
        frames = align_h3_frames(seconds)
        seed = str(int(hashlib.md5(f"{r['id']}|{seconds}".encode()).hexdigest()[:8], 16) % 1_000_000)
        shots.append({"id": f"shot_{k:02d}",
                      "prompt": [_six_section(r["description"], refs, seconds)],
                      "length": frames, "seed": seed})
    # 采样步数档位（批1b 2026-09-12）：config manga.h3_steps，标准 6 /
    # 快速 4（Turbo LoRA 有效区间 4~8，钳制防呆）。h3_turbo=false 走
    # 官方非 turbo 基线（摘蒸馏 LoRA，官方目录步数=20；2026-10-11 挡位
    # 战役放开 8~30 供 A/B，缺省仍 turbo，生产行为不变）
    # 双采加速产线（2026-10-24 拍板）：
    # - 720p + turbo = 同图换配方双采（粗采 turbo 4/6/8 步 + 精修 2 步
    #   dn0.24 大神原方），图拓扑与 VRAM 包络同现行两阶段（16G 实测安全）
    # - 960p/1440p = ×2 放大档：16G 卡精修窗口两次实锤整进程崩
    #   （12:09/12:41 同节点同进度），加闸默认关——大显存机可开
    _turbo_two_pass = quality in _TURBO2P_BASE
    try:
        _manga = (get_config().get("manga") or {})
        _steps = int(_manga.get("h3_steps", 6) or 6)
        _turbo = bool(_manga.get("h3_turbo", True))
        _x2_gate = bool(_manga.get("h3_two_pass_x2", False))
    except Exception:  # noqa: BLE001 - 配置异常按标准档
        _steps, _turbo, _x2_gate = 6, True, False
    if _turbo_two_pass and not _x2_gate:
        raise ApiError(
            "VIDEO_GENERATION_FAILED",
            f"{quality} ×2 放大档在 16G 机型会崩 ComfyUI（两次实锤：精修"
            "窗口整进程死亡），已在 config manga.h3_two_pass_x2 加闸默认关"
            "——大显存机型可开，或改用 720p+turbo 双采（h3_turbo=true）",
            detail={"quality": quality})
    if _turbo_two_pass:
        _turbo = True
    if steps_override:
        # 调用方点名步数（A/B 与未来 UI 档位用），仍走同款钳制防呆
        _steps = int(steps_override)
    if turbo_override is not None:
        _turbo = bool(turbo_override)
    _steps = (max(4, min(8, _steps)) if _turbo
              else max(8, min(30, _steps)))
    # 两阶段旗量：720p 恒两阶段（10-17 拍板唯一产链）；turbo 时粗采换
    # turbo 步数、精修换大神 2 步配方（渲染层按 turbo 分叉）
    _two_stage = (quality == "720p")
    # 480p 同分辨率 σ 精修（2026-10-24 turbo 双采 480p 档）：粗采后
    # 追加 2 步低σ精修（无放大）；720p 已有 two_stage 不叠加
    _same_res_refine = (bool(refine_pass) and quality == "480p"
                        and not _two_stage)
    plan = {"defaults": {"duration_seconds": seconds,
                         "steps": _steps},
            "shots": shots}
    plan_json = json.dumps(plan, ensure_ascii=False, indent=1)

    # 3. 组图提交（与导演台同锁,互斥 GUI/其他 H3 任务）;无论成败,结束必卸载权重
    _cancel_check()
    report(0.04, "提交 H3 链式工作流")
    engine._ensure_running(lambda f, s: report(0.02 + f * 0.02, s))
    # 忙碌标记（2026-08-31 事故）：链式走自有提交路径不经 h3_engine.generate
    # 的 mark 配对，单 prompt 运行 >300s 被 comfy_proc 空闲关杀（链式中断）。
    from .comfy_proc import get_comfy_proc as _get_comfy_proc
    _get_comfy_proc().mark_busy()
    try:
        with engine._gen_lock:
            graph = _render_graph(json.loads(_load_template_text()),
                                  plan_json, run_name, width, height,
                                  refs, task_id, start_clip, turbo=_turbo,
                                  two_stage=_two_stage,
                                  carry_image=carry_image,
                                  two_pass=_turbo_two_pass,
                                  base_dims=_base_dims,
                                  same_res_refine=_same_res_refine)
            import urllib.error
            try:
                resp = engine._api("POST", "/prompt",
                                   body={"prompt": graph,
                                         "client_id": f"omnispace-{task_id}"},
                                   timeout=15.0)
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "ignore")[:500]
                except Exception:  # noqa: BLE001
                    log.debug("run_h3_chain_task: 降级忽略", exc_info=True)
                raise ApiError("COMFY_WORKFLOW_INVALID",
                               f"链式工作流被拒绝(HTTP {exc.code}): {body}") from exc
            if resp is None:
                raise ApiError("COMFY_UNREACHABLE", "ComfyUI 不可达（链式提交失败）")
            if resp.get("error") or resp.get("node_errors"):
                import json as _j
                raise ApiError("COMFY_WORKFLOW_INVALID", "链式工作流校验失败: " + _j.dumps(
                    resp.get("node_errors") or resp.get("error"),
                    ensure_ascii=False)[:400])
            prompt_id = str(resp["prompt_id"])
            report(0.06, "链式采样中")

            # 4. 轮询（复用导演台的帧数 ETA + 活性检测；3s 一查取消）；
            # 采样进度映射 0~0.68（2026-10-02 进度重置修复：给后处理双
            # pass 留 0.70~0.99 全局区间，避免 pass 局部进度倒退假象）
            video_rel = _poll_chain_history(
                engine, prompt_id, seconds, frames,
                lambda f, s: report(min(f, 1.0) * 0.68, s),
                check_cancel=check_cancel,
                on_cancel=_cancel_interrupt)
    finally:
        _get_comfy_proc().mark_idle()
        # 跑完必卸载,但仅在队列空闲时执行——若用户/其他任务已排队,
        # 贸然 /free 会把运行中任务的模型抽走(2026-08-30 排查修正);
        # keep_loaded: 后端视频队列还有后继 H3 任务(2026-09-02),接力
        # 镜免整轮权重重载
        try:
            if keep_loaded:
                log_note = "队列后继为 H3,跳过卸载(接力免重载)"
                log.info(f"[h3_chain] {log_note}")
            else:
                q = engine._api("GET", "/queue", timeout=5.0) or {}
                busy = bool((q.get("queue_running") or [])
                            or (q.get("queue_pending") or []))
                if busy:
                    log_note = "队列非空,跳过卸载(由后续任务接管显存)"
                    log.info(f"[h3_chain] {log_note}")
                else:
                    engine.unload()
        except Exception:  # noqa: BLE001 - 卸载探测失败按原样尝试卸载
            if not keep_loaded:
                engine.unload()
        finally:
            shutil.rmtree(Path(_COMFY_INPUT) / f"h3chain_{task_id}",
                          ignore_errors=True)

    # 5. 收片：final MP4 → 项目视频目录
    src = (_COMFY_OUTPUT / video_rel)
    if not src.is_file():
        raise ApiError("COMFY_NO_OUTPUT", f"链式成片缺失: {video_rel}")
    dest_dir = DATA_DIR / "videos" / str(row_ids[0])[:16]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{task_id}.mp4"
    # 中间帧工作目录定位（move 前取，src 移走后路径仍在但文件不在）：
    # 只回收本任务 h3chain_<task_id> 目录，绝不触碰 h3_chains 根
    run_dir = next((a for a in src.parents
                    if a.name == f"h3chain_{task_id}"), None)
    shutil.move(str(src), dest)
    shutil.rmtree(Path(_COMFY_INPUT) / f"h3chain_{task_id}",
                  ignore_errors=True)  # 参考图副本回收(防磁盘泄漏)
    # 中间帧工作目录回收：成片已剪切走，目录里只剩链式过程帧，留存会
    # 无限累积（2026-08-31 实测 output/h3_chains 积压 5.4G）
    if run_dir is not None and run_dir != _COMFY_OUTPUT \
            and _COMFY_OUTPUT in run_dir.parents:
        shutil.rmtree(run_dir, ignore_errors=True)
    # 6. 后处理双 pass（2026-10-01 补链式引擎缺口——分镜行"生成视频"
    # 主力链；脸修复在前、逐帧放大殿后；全失败路径回退原片零阻断，
    # config comfyui.h3_face_refine / h3_frame_upscale 各自开关）
    # RAM 减压（2026-10-02 两次后端崩溃复盘 00:42/01:21：三道工序连跑
    # 时 H3 权重+对话引擎叠加把 RAM 顶到 99% → 进程死亡 → 孤儿回收判
    # error）：①工序前卸对话引擎（后端进程 ~9GB 大户）②脸修复毕立即
    # 卸 H3 权重（放大只吃 ESRGAN ~1GB）
    from .h3_face_refine import face_refine_enabled, refine_video
    from .h3_frame_upscale import frame_upscale_enabled, upscale_video
    try:
        from .dialog_engine import get_dialog_engine
        if get_dialog_engine().unload_model():
            from .prompt_translator import shrink_working_set
            shrink_working_set()
            log.info("[h3_chain] 后处理前已卸对话引擎减压 RAM")
    except Exception:  # noqa: BLE001 - 卸载失败不阻断
        pass
    post = dest
    if face_refine_enabled():
        first = shots[0] if shots else {}
        chain_prompt = str(first.get("prompt", [""])[0]) if first else ""
        # 进度映射（2026-10-02 用户报「进度条重置」）：pass 内部上报的是
        # 自身局部进度（0→0.95），直通会覆盖采样期已到 ~0.9 的全局进度
        # =进度条倒退假象。现映射进全局区间：采样 0~0.68 / 脸修复
        # 0.70~0.85 / 放大 0.85~0.99 / 完成 1.0
        post = refine_video(
            post, prompt=chain_prompt, ref_images=ref_imgs,
            progress_cb=lambda f, s: report(
                0.70 + 0.15 * max(0.0, min(1.0, f)), s))
        try:
            engine.unload()
            log.info("[h3_chain] 脸修复毕已卸 H3 权重（放大前减压）")
        except Exception:  # noqa: BLE001 - 卸载失败不阻断
            pass
    if frame_upscale_enabled():
        post = upscale_video(
            post, progress_cb=lambda f, s: report(
                0.85 + 0.14 * max(0.0, min(1.0, f)), s))
    # 全局锐化 pass（2026-10-12 用户令「原生+全局锐化+FaceRefine」）：
    # 原生直出整帧 CAS 锐化——修正全屏播放下 720p 原生纹理的感知软化
    # （只有脸走 FaceRefine，背景锐差被拉大=「脸清景糊」主诉根因）。
    # 秒级 ffmpeg 滤镜，失败零阻断回退原片
    if h3_sharpen_enabled():
        post = _sharpen_pass(
            post, strength=_sharpen_strength(),
            progress_cb=lambda s: report(0.99, s))
    if post != dest:
        dest = post
    # 音轨文件级强制（2026-10-02 三报定案）：C 段显式 台词：无+音效：无
    # → 提示词禁令（soundscape/A 段中和/音频铁律）已被模型三次无视
    # （B 段散文被当旁白朗读）——直接换静音轨执行用户标注
    _has_dlg, _has_sfx, _explicit_mute = _chain_audio_spec(
        [r["description"] for r in rows], seconds)
    if _explicit_mute and not _has_dlg and not _has_sfx:
        report(0.99, "音轨静音处理")
        dest = _mute_audio_track(dest)
    report(1.0, "完成")
    total_seconds = sum(float(s["length"]) for s in shots) / 24.0
    # 真实档位标签（2026-10-11 挡位战役：turbo/非 turbo 与步数由本引擎
    # 决定，调用方 model_used 直取，消「标签写死 turbo 名不符实」）
    engine_label = ("h3_chain_turbo_v10" if _turbo
                    else f"h3_chain_v10_s{_steps}" + ("_2s" if _two_stage else ""))
    if _turbo and _two_stage:
        # turbo 双采版 720p（同图换配方，2026-10-24 拍板）
        engine_label = f"h3_chain_turbo2s_s{_steps}"
    if _turbo_two_pass:
        # ×2 放大产线如实标签（大显存机型专用）
        engine_label = f"h3_chain_turbo2p_s{_steps}"
    if carry_image:
        engine_label += "_carry"  # 承接生效可见性（2026-10-24 行级承接）
    return {"file_path": str(dest), "resolution": f"{width}x{height}",
            "duration_seconds": total_seconds,
            "engine_label": engine_label,
            "run_name": run_name, "start_clip": start_clip}


def _poll_chain_history(engine: Any, prompt_id: str, seconds: float,
                        frames: int, report: Callable[[float, str], None],
                        check_cancel: Callable[[], None] | None = None,
                        on_cancel: Callable[[Any], None] | None = None) -> str:
    """链式轮询：整体 ETA 按帧数线性 + 队列活性检测（复用导演台口径）。

    check_cancel：每轮（3s）取消检查，命中经 on_cancel 中断 ComfyUI
    当前 prompt 后原样上抛（2026-09-02 视频队列取消接线）。
    """
    eta = max(seconds * 3.0, frames * 5.5 + 6 * 5.0)
    deadline = time.monotonic() + eta + 900.0
    start = time.monotonic()
    while time.monotonic() < deadline:
        if check_cancel is not None:
            try:
                check_cancel()
            except Exception:
                if on_cancel is not None:
                    on_cancel(engine)
                raise
        time.sleep(3.0)
        report(min(0.06 + 0.9 * (time.monotonic() - start) / eta,
                   0.96), "链式采样中")
        hist = engine._api("GET", f"/history/{prompt_id}", timeout=5.0)
        if hist is None:
            continue
        entry = hist.get(prompt_id) or {}
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            import json as _j
            raise ApiError("COMFY_EXEC_FAILED", "链式执行失败: " + _j.dumps(
                status.get("messages") or [], ensure_ascii=False)[:400])
        for node_out in (entry.get("outputs") or {}).values():
            for item in (node_out.get("images") or node_out.get("videos") or []):
                fn = str(item.get("filename") or "")
                sub = str(item.get("subfolder") or "")
                if fn.endswith(".mp4") and "final" in sub:
                    report(0.98, "拼装完成")
                    return f"{sub}/{fn}" if sub else fn
        if status.get("completed"):
            raise ApiError("COMFY_NO_OUTPUT", "链式执行完成但无成片产物")
        q = engine._api("GET", "/queue", timeout=5.0)
        if q is not None:
            busy = {str(it[1]) for it in (q.get("queue_running") or [])}
            pend = {str(it[1]) for it in (q.get("queue_pending") or [])}
            if prompt_id not in busy | pend:
                raise ApiError("VIDEO_ENCODE_FAILED", "链式任务已被中断（不在队列且无产物）")
    # 超时止损（round-2026-09-20 F6）：链式 H3 继续空跑会拖死后续任务
    try:
        engine._api("POST", "/interrupt", timeout=5.0)
    except Exception:  # noqa: BLE001 - 止损失败不改变原异常
        pass
    raise ApiError("VIDEO_ENCODE_FAILED", "链式生成超时", )
