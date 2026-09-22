# 本项目仅供学习使用，商业授权请+Q 3559331368
"""思考/正文分界策略与复读循环检测（2026-09-22 自 dialog_engine 拆出）。

dialog_engine 是架构基线巨石（只许拆不许涨），本模块承载其自包含的
思考协议策略面：
  - THINKING_SYSTEM_SUFFIX【最终回答】标记协议（提示词侧思考引导）；
  - _ThinkingStreamParser 流式 reasoning/content 双通道切分；
  - is_native_reasoning_model 原生思考型号判定（与 vLLM 启动注入同源，
    防双思考打架的旁路依据，2026-09-10 根修）；
  - detect_repeat_loop 复读循环流式检测（2026-09-22 对话防复读 B 案）；
  - 长文两步走（2026-09-22 用户拍板）：先列提纲再按提纲写——计划型
    记忆，复读高发于无计划的注水段（写作台实测）。
dialog_engine / api.dialog 转出口径不变（import 路径零破坏）。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dialog_engine import DialogEngine

# 深度思考模式追加段（2026-08-22 思考过程展示）：Qwen3-VL Instruct
# 模板无 enable_thinking 变量（实测渲染 diff 为空）；实测模型对
# <think> 标签指令遵守度不足（0/1 输出标签），但对【步骤名】标记
# 协议 100% 遵守 → 思考/正文分界改用【最终回答】标记协议。
THINKING_SYSTEM_SUFFIX = (
    "\n\n【深度思考模式】收到问题后，请先展示你的思考过程，"
    "严格按以下四步框架组织，每步以【步骤名】开头：\n"
    "【问题分析】拆解问题的核心诉求与关键约束；\n"
    "【信息检索】列出回答所需的已知信息与缺失信息；\n"
    "【方案评估】对比候选方案的优劣；\n"
    "【决策依据】说明最终选择的理由。\n"
    "四步思考完成后，另起一行输出标记【最终回答】，然后在标记之后"
    "输出最终回答（简洁准确，不要重复思考内容）。"
)

# 思考/正文分界标记（与 THINKING_SYSTEM_SUFFIX 协议配对）
_ANSWER_MARKER = "【最终回答】"


def is_native_reasoning_model(model_id: str | None) -> bool:
    """模型思考是否已由 vLLM 服务端 reasoning parser 剥离（原生思考型）。

    与 vllm_service.VLLM_REASONING_PARSERS 启动注入同源（2026-09-22
    抽单源）：deepseek-r1 / qwen35 系启动即带 --reasoning-parser，
    <think> 由服务端剥离——对这类模型叠加提示词思考协议会双思考打架
    （2026-09-10 根修，旁路必须保留）；其余 vLLM 模型（如
    qwen3-vl-8b-awq Instruct，无原生思考）走【最终回答】标记协议。
    """
    name = (model_id or "").lower()
    if not name:
        return False
    try:
        from ...engines.vllm_service import VLLM_REASONING_PARSERS
        pairs = VLLM_REASONING_PARSERS
    except Exception:  # noqa: BLE001 - 接线缺失按保守名单兜底
        pairs = (("deepseek-r1", ""), ("qwen35", ""))
    return any(needle in name for needle, _parser in pairs)


# 复读循环流式检测参数（2026-09-22 对话防复读 B 案；写作台
# novel_service.detect_repetition 是「全文任一 80 字窗出现 2 次」的
# 章节级判据，对话直接套用会对正常排比/对白误伤——按流式场景调参。
# 21:23 三次实弹（4B 系统报告）：带递增序号的循环（"1. 健康度良好…
# 2. 健康度良好…"，序号变正文同）与超大块循环绕过「相同尾巴」判据
# ——v3 加窗口 n 元片段重复计数：16 字片段（含 ≥6 实词字符，防表格
# 分隔线误伤）在窗口内出现 ≥4 次即判循环。
REPEAT_LOOP_MIN_BLOCK = 8
REPEAT_LOOP_MAX_BLOCK = 500
REPEAT_LOOP_MIN_CYCLES = 4
REPEAT_NGRAM = 16
REPEAT_NGRAM_MIN_COUNT = 4
REPEAT_NGRAM_MIN_ALNUM = 6
REPEAT_NGRAM_STEP = 4


def _has_repeated_ngram(t: str) -> bool:
    """窗口 n 元片段重复计数（v3）：任一 16 字实词片段出现 ≥4 次。"""
    counts: dict[str, int] = {}
    for i in range(0, len(t) - REPEAT_NGRAM + 1, REPEAT_NGRAM_STEP):
        g = t[i:i + REPEAT_NGRAM]
        if sum(c.isalnum() for c in g) >= REPEAT_NGRAM_MIN_ALNUM:
            c = counts.get(g, 0) + 1
            if c >= REPEAT_NGRAM_MIN_COUNT:
                return True
            counts[g] = c
    return False


def detect_repeat_loop(text: str) -> bool:
    """复读循环检测（纯函数，可测）：流式正文是否陷入循环。

    判据（任一命中即 True）：
      1. 尾部块级循环：去空白后尾部同一 ≥8 字块连续重复 ≥4 次
         （覆盖同句/对白轮替；块长上限 500 覆盖大块循环）；
      2. 窗口 n 元重复：任一 16 字实词片段在输入内出现 ≥4 次
         （覆盖递增序号/交错变体循环——正文相同前缀不同也算）。
    调用方传 seen[-4096:] 作窗口，正常对白/排比不误伤。
    """
    t = "".join((text or "").split())
    n = len(t)
    max_block = min(REPEAT_LOOP_MAX_BLOCK, n // REPEAT_LOOP_MIN_CYCLES)
    for k in range(REPEAT_LOOP_MIN_BLOCK, max_block + 1):
        if t[-k * REPEAT_LOOP_MIN_CYCLES:] == t[-k:] * REPEAT_LOOP_MIN_CYCLES:
            return True
    return _has_repeated_ngram(t)


REPEAT_CUT_NOTICE = (
    "\n\n（检测到内容重复循环，已自动截断——可点本条消息的"
    "「重新生成」重试）")


class _ThinkingStreamParser:
    """流式思考分隔解析器（思考过程展示，2026-08-22）。

    协议（实测裁定）：把后端原始文本流切分为 reasoning / content 双通道——
      - thinking 模式下初始即 reasoning 态（思考先行）；
      - reasoning 态遇 ``【最终回答】`` 切换 content 态（标记本身吞掉
        不输出，正文气泡只见答案）；
      - holdback：产出尾部若是标记真前缀（``【`` ``【最`` ``【最终`` 等）
        扣留至可判定（标记跨 chunk 分割安全），其余直通零缓冲；
      - 退化：EOF 仍未见标记（模型未遵守协议）→ reasoning 全文复制为
        content 一条事件（保底气泡可见正文，宁重复不丢答案）。
    """

    _SEP = _ANSWER_MARKER
    # 标记的全部真前缀，用于尾部扣留判定
    _PREFIXES = tuple(sorted(
        {_ANSWER_MARKER[:i] for i in range(1, len(_ANSWER_MARKER))},
        key=len, reverse=True))

    def __init__(self) -> None:
        self._in_reasoning = True    # thinking 模式思考先行
        self._seen_sep = False
        self._reasoning_all = ""     # 退化复制用（全文累积）
        self._buf = ""

    def _tail_prefix_len(self, s: str) -> int:
        """s 尾部匹配标记前缀的最长长度（0 = 无扣留）。"""
        for p in self._PREFIXES:
            if s.endswith(p):
                return len(p)
        return 0

    def feed(self, text: str) -> list[tuple[str, str]]:
        """喂入一段增量，产出 [(kind, chunk), ...]（kind: reasoning/content）。"""
        self._buf += text
        out: list[tuple[str, str]] = []
        while True:
            if self._in_reasoning:
                idx = self._buf.find(self._SEP)
                if idx >= 0:
                    if idx:
                        chunk = self._buf[:idx]
                        out.append(("reasoning", chunk))
                        self._reasoning_all += chunk
                    self._buf = self._buf[idx + len(self._SEP):]
                    self._in_reasoning = False
                    self._seen_sep = True
                    continue
                keep = self._tail_prefix_len(self._buf)
                emit, self._buf = self._buf[:len(self._buf) - keep], \
                    self._buf[len(self._buf) - keep:]
                if emit:
                    out.append(("reasoning", emit))
                    self._reasoning_all += emit
            else:
                # 正文态：分隔只切一次，其后标记按字面量直通
                if self._buf:
                    out.append(("content", self._buf))
                    self._buf = ""
            return out

    def flush(self) -> list[tuple[str, str]]:
        """EOF 冲刷：残余按当前状态归属；未见标记时思考全文复制为正文。"""
        out: list[tuple[str, str]] = []
        if self._buf:
            if self._in_reasoning:
                out.append(("reasoning", self._buf))
                self._reasoning_all += self._buf
            else:
                out.append(("content", self._buf))
            self._buf = ""
        if not self._seen_sep and self._reasoning_all:
            out.append(("content", self._reasoning_all))
        return out


# ── 长文两步走（2026-09-22 用户拍板：对话页长文先列提纲再写）────────
_OUTLINE_SKIP_RE = re.compile(r"提纲|大纲|目录")
_OUTLINE_SYSTEM_PROMPT = (
    "你是创作策划。根据用户请求写一份简明创作提纲：主题定位、结构分段"
    "（每段一两句说明写什么）、关键情节推进点与结局走向。只输出提纲"
    "本身，不超过 300 字，不要输出正文。")


def is_longform_request(text: str) -> bool:
    """长文创作请求判定（启发式）：字数暗示直接命中；文体+创作动词
    组合命中。用户本身在要提纲/大纲时不介入。"""
    if not text or _OUTLINE_SKIP_RE.search(text):
        return False
    if re.search(r"\d{3,}\s*字|长篇|中篇", text):
        return True
    return bool(re.search(r"小说|故事|剧本|散文|文章|作文|番外|章节", text)
                and re.search(r"写|创作|续写|来一篇|生成", text))


def longform_outline(engine: DialogEngine, request_text: str) -> str:
    """两步走①：单轮轻上下文生成长文提纲；失败返回空串=退化一步直写。

    engine 为 DialogEngine（仅类型标注用，运行时零耦合——绕开
    thinking_policy ← dialog_engine 的循环导入）。
    """
    try:
        msgs = [
            {"role": "system", "content": _OUTLINE_SYSTEM_PROMPT},
            {"role": "user", "content": request_text},
        ]
        out = (engine.chat(msgs, None, 0.6, 700) or "").strip()
        return out.strip("`").strip()[:1500]
    except Exception:  # noqa: BLE001 - 提纲失败不阻断对话
        return ""


def outline_system_block(outline: str) -> str:
    """两步走②：提纲 → 注入系统提示的「必须遵循」块。"""
    return (
        "\n\n【创作提纲·必须遵循】按以下提纲结构完整撰写正文，"
        "情节持续推进，禁止原地打转或复读已输出的句子：\n"
        + outline)
