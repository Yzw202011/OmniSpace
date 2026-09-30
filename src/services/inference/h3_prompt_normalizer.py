"""H3 Prompt 转义层：任意输入格式 → 官方三段式（2026-09-30 拍板 C）。

背景（docs/audit/h3-prompt-official-alignment-2026-09-30.md 批1-c）：
H3 官方把 prompt 写作规范开源为 Agent Skill 三件套（本仓
skills/h3_prompt_writing/，官方仓 .agents/skills/h3-prompt-writing/ 镜像）。
官方架构中的 Context-IR（指令理解改写层）未开源但官方声明"可按
Prompting Guidance 自建上下文处理系统"——本模块即自建转义层。

两条路（按输入格式自动分流，出口统一官方语法）：
1. 确定性拼装（abc_to_official）：A/B/C 结构化数据在手时纯函数
   拼官方三段式（integrated_multimodal_description + overall_soundscape
   + non_diegetic_music + 首帧对齐指令行），零 LLM 依赖，子件翻译
   由调用方注入（同 h3_convert 模式）。
2. LLM 改写（freeform_to_official）：用户自由文本（非 A/B/C 协议）
   经对话引擎按官方规则改写；规则真源 = skills/h3_prompt_writing/
   references/base-en.txt 运行时加载（单源，不硬编码复制）。
   校验失败重试一次，再失败诚实降级返回 None（调用方回退原文）。

降级铁律：转义失败绝不阻断生成——原文直喂行为与接入前完全一致。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable

from ...config import ROOT_DIR

log = logging.getLogger("omnispace.inference.h3_prompt_normalizer")

# 官方规则真源（批1 入库镜像；上游 = MiniMax-AI/MiniMax-H3 官方仓）
_OFFICIAL_GUIDE = (ROOT_DIR / "skills" / "h3_prompt_writing"
                   / "references" / "base-en.txt")

# 三段式字段名（官方 base-en §2.2；顺序锁定）
_THREE_FIELDS = ("integrated_multimodal_description",
                 "overall_soundscape",
                 "non_diegetic_music")

_CJK_RE = re.compile(r"[一-鿿　-〿＀-￯]")

# A/B/C 协议判据（与 video._detect_row_grid / h3_convert.parse_abc 同族：
# C 段时间轴标记存在即结构化；宽松判据降低误判自由文本）
_ABC_MARK_RE = re.compile(
    r"(?m)^[ABC][.、．]|C[.、．]\s*分镜时间轴|\[\d[\d.]*s?\s*[-–~—]\s*\d[\d.]*s?\]")


# ───────────────────────── 格式探测 ─────────────────────────

def detect_input_format(desc: str) -> str:
    """输入描述词格式探测：'abc'（结构化协议）| 'freeform'（自由文本）。

    判据：出现 A./B./C. 段标记、分镜时间轴标题或 [Xs-Ys] 时间轴行
    任一即判 abc（宁多判：abc 误判 freeform 只是多走一次确定性
    拼装，无副作用；freeform 误判 abc 才会丢结构）。
    """
    text = (desc or "").strip()
    if not text:
        return "freeform"
    return "abc" if _ABC_MARK_RE.search(text) else "freeform"


# ─────────────────── 确定性三段式拼装（A/B/C） ───────────────────

def abc_to_official(*, first_frame_anchored: bool,
                    shot_desc_en: str, style_en: str,
                    sfx_en: str, has_music: bool = False,
                    music_en: str = "") -> str:
    """A/B/C 单镜结构化数据 → 官方三段式（base-en §2/§3.1 I2VA 形态）。

    官方 Part-1 指令行 + 三核心字段；正文子件须已英文化（调用方经
    _h3_translator 翻译，与 h3_convert._maybe_en 同约定）。首帧锚
    声明按官方 I2VA 措辞（<Picture 1> = 0.00s 实际首帧）。
    """
    desc = (shot_desc_en or "").strip()
    parts: list[str] = []
    if first_frame_anchored:
        parts.append(
            "For the target video, at 0.00 seconds into the target video, "
            "<Picture 1> (from [Shot 1]) is fully referenced.")
    body_bits = [b for b in ((style_en or "").strip(), desc) if b]
    body = "[Shot 1] " + (" ".join(body_bits) or "as described")
    fields = [
        f"integrated_multimodal_description: {body}",
        "overall_soundscape: "
        + ((sfx_en or "").strip()
           or "Quiet ambient atmosphere only; no prominent sound events."),
        "non_diegetic_music: "
        + ((music_en or "").strip() if has_music else "N/A"),
    ]
    parts.extend(fields)
    return "\n\n".join(parts)


# ─────────────────── LLM 自由文本改写（freeform） ───────────────────

_rules_cache: str | None = None
_rules_lock = threading.Lock()


def load_official_rules() -> str:
    """官方 base-en.txt 规则全文（进程内缓存；缺失返回空串由调用方降级）。"""
    global _rules_cache
    with _rules_lock:
        if _rules_cache is None:
            try:
                _rules_cache = _OFFICIAL_GUIDE.read_text(encoding="utf-8")
            except OSError as exc:  # noqa: BLE001 - 规则文件缺失走降级
                log.warning("官方规则文件缺失，自由文本改写降级: %s", exc)
                _rules_cache = ""
        return _rules_cache


_SYSTEM_TMPL = (
    "You rewrite a free-form video description into the official MiniMax "
    "H3 prompt format. Follow the OFFICIAL GUIDE below exactly.\n"
    "\n"
    "== OFFICIAL GUIDE BEGIN ==\n{guide}\n== OFFICIAL GUIDE END ==\n"
    "\n"
    "TASK RULES:\n"
    "1. Output ONLY the final prompt: the alignment instruction line "
    "(skip it when no reference image is used), then exactly three "
    "fields in order: integrated_multimodal_description, "
    "overall_soundscape, non_diegetic_music.\n"
    "2. Write sections in English. Preserve any dialogue, lyrics, or "
    "visible on-screen text verbatim in the original language inside "
    "<d>[lang] ...</d> or English double quotes.\n"
    "3. Single video, single shot unless the user clearly describes "
    "multiple shots; then use [Shot N] with strictly increasing cut "
    "times like 'At 00:03.500'. The first shot has NO timestamp.\n"
    "4. Expand faithfully — enrich composition, environment, lighting, "
    "camera motion (type + amplitude + speed) and sound consistent "
    "with the user's intent. NEVER invent new characters, events, or "
    "plot points not present or implied.\n"
    "5. No explanations, no markdown fences.\n"
    "\n"
    "USER DESCRIPTION:\n{user_text}"
)


def _validate_output(text: str) -> bool:
    """改写输出校验：三字段齐 + <d> 外无大量中文残留。"""
    if not text or not text.strip():
        return False
    for f in _THREE_FIELDS:
        if f"{f}:" not in text:
            return False
    outside_d = re.sub(r"<d>.*?</d>", "", text, flags=re.S)
    cjk = len(_CJK_RE.findall(outside_d))
    return cjk <= 8  # 与 prompt_translator 失败判据同口径


def _strip_fence(text: str) -> str:
    """剥偶发 markdown 围栏/前缀噪声。"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    t = re.sub(r"^(final prompt|official prompt|prompt)\s*[:：]\s*",
               "", t, flags=re.IGNORECASE)
    return t


def freeform_to_official(user_text: str,
                         chat: Callable[..., str] | None = None,
                         *, max_tokens: int = 1024,
                         max_retries: int = 1) -> str | None:
    """自由文本 → 官方三段式；不可用/校验失败诚实降级返回 None。

    Args:
        user_text: 用户自由描述（任意格式任意语言）
        chat: 对话引擎 chat 函数（缺省懒取 dialog_engine，与
              prompt_translator 同引擎复用分时换载）
        max_tokens: 改写输出上限（官方正文 350-500 词 + 声音段）
        max_retries: 校验失败重试次数
    """
    guide = load_official_rules()
    if not guide:
        return None
    text = (user_text or "").strip()
    if not text:
        return None

    if chat is None:
        from .dialog_engine import get_dialog_engine
        engine = get_dialog_engine()
        if not engine.is_ready and not engine.ensure_loaded(None):
            log.warning("对话引擎不可用，自由文本转义跳过: %s",
                        engine.get_status().get("last_error"))
            return None
        chat = engine.chat  # type: ignore[method-assign]

    system = _SYSTEM_TMPL.format(guide=guide, user_text=text[:6000])
    attempts = max_retries + 1
    for i in range(attempts):
        try:
            out = chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": text}],
                temperature=0.4,
                max_new_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001 - 引擎故障走降级
            log.warning("自由文本转义异常（降级原文）: %s", exc)
            return None
        out = _strip_fence(out)
        if _validate_output(out):
            log.info("自由文本已转义官方三段式: %d -> %d 字符",
                     len(text), len(out))
            return out
        log.warning("转义输出校验失败（第 %d/%d 次），%s",
                    i + 1, attempts,
                    "重试" if i < attempts - 1 else "降级原文")
    return None


# ───────────────────────── 统一入口 ─────────────────────────

def normalize_prompt(desc: str, *,
                     abc_builder: Callable[[], str] | None = None,
                     chat: Callable[..., str] | None = None) -> str:
    """统一转义入口：abc → 确定性拼装；freeform → LLM 改写。

    Args:
        desc: 原始描述词（探测依据）
        abc_builder: A/B/C 结构化拼装回调（调用方持有拆解数据时
                     注入；None 时 abc 输入原样返回——由既有链处理）
        chat: 对话引擎注入（测试用；缺省懒取）

    Returns:
        官方三段式文本；任何失败路径返回原文（零阻断铁律）。
    """
    text = (desc or "").strip()
    if not text:
        return text
    fmt = detect_input_format(text)
    if fmt == "abc":
        return abc_builder() if abc_builder is not None else text
    official = freeform_to_official(text, chat)
    return official if official is not None else text


def abc_text_to_official(shots: list[dict], *, style_en: str = "",
                         shot_offset_s: float = 0.0) -> str:
    """已拆解的 A/B/C 镜头列表 → 官方三段式多镜文本（普通单段链用）。

    与 _official_segment_prompt（网格链）同语法真源（base-en §2/§4）：
    - 多镜合并一段：[Shot 1] 无时间戳，后续镜 `At 00:0X.XXX` 切镜
      （时间戳相对首镜起点，严格递增）
    - 运镜并入镜头自然句（翻译模板产出三维句形态）；台词 <d>[Chinese]
      逐字；声音两段取各镜 sfx 合并（去重保序）
    - 子件英文由调用方翻译注入（shots 内字段须已英文化）；本函数只
      做确定性拼装，零 LLM。

    Args:
        shots: [{text, camera, sfx, dialogue, start, end}, ...]（首帧
               保持段应已被解析层剔除）
        style_en: A 段全局风格（英文化后）
        shot_offset_s: 时间戳基准偏移（普通链传首镜 start 归零即可）
    """
    body_shots = [s for s in shots if (s.get("text") or s.get("dialogue")
                                       or s.get("camera"))]
    if not body_shots:
        return ""
    t0 = (body_shots[0].get("start") or 0.0) + shot_offset_s
    parts: list[str] = []
    sfx_seen: list[str] = []
    for i, sh in enumerate(body_shots):
        seg = (sh.get("text") or "").strip()
        cam = (sh.get("camera") or "").strip()
        if cam:
            seg = (f"{seg} {cam}" if cam.lower().startswith(
                ("the camera", "camera"))
                else f"{seg}. The camera motion: {cam}")
        dlg = (sh.get("dialogue") or "").strip()
        if dlg:
            seg = f"{seg.rstrip('.')} (S1) says: <d>[Chinese] {dlg}</d>"
        if i == 0:
            parts.append(f"[Shot 1] {seg}".strip())
        else:
            cut = max(0.0, (sh.get("start") or 0.0) + shot_offset_s - t0)
            total_ms = int(round(cut * 1000))
            mm, rem = divmod(total_ms, 60000)
            sec = rem / 1000.0
            parts.append(f"[Shot {i + 1}] At {mm:02d}:{sec:06.3f}, "
                         f"the camera cuts to {seg}".strip())
        sfx = (sh.get("sfx") or "").strip()
        if sfx and sfx not in sfx_seen:
            sfx_seen.append(sfx)
    body = " ".join(parts)
    desc = (f"{style_en.strip()} {body}".strip()
            if style_en.strip() else body)
    sfx_block = ((". ".join(sfx_seen) + ".") if sfx_seen
                 else "Quiet ambient atmosphere only; no prominent sound events.")
    return (
        "integrated_multimodal_description: "
        + desc + "\n\n"
        + "overall_soundscape: " + sfx_block + "\n\n"
        + "non_diegetic_music: N/A"
    )


def abc_builder_from_desc(desc: str, *, parse_abc, translate) -> str:
    """A/B/C 描述词文本 → 官方三段式（普通单段链入口）。

    parse_abc/translate 由调用方注入（API 层的 _parse_abc_shots 与
    _h3_translator——与网格链共用同一翻译模板）。非 abc 输入、拆解
    无实体镜头、拼装异常：返回空串（调用方回退 normalize_prompt
    自由文本路），零阻断。
    """
    try:
        if detect_input_format(desc) != "abc":
            return ""
        parsed = parse_abc(desc) or {}
        shots = parsed.get("shots") or []
        if not shots:
            return ""
        import re as _re

        m = _re.search(r"(?m)^B[.、．]", desc)
        a_text = desc[:m.start()].strip() if m else desc
        style_en = translate(_re.sub(
            r"^A[.、．]\s*(?:全局风格|Global\s*style)?\s*[：:]?\s*",
            "", a_text, flags=_re.IGNORECASE))
        shots_en = [{
            **sh,
            "text": translate(sh.get("text") or ""),
            "camera": translate(sh.get("camera") or ""),
            "sfx": translate(sh.get("sfx") or ""),
        } for sh in shots]
        return abc_text_to_official(shots_en, style_en=style_en)
    except Exception as exc:  # noqa: BLE001 - 拼装失败回退既有链
        log.warning("abc_builder_from_desc 拼装失败（回退既有链）: %s", exc)
        return ""
