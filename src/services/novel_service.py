"""小说生成编排服务（批2 小说模块 MVP，2026-09-05）。

职责（实施计划 §2.3，docs/新模块与多显卡实施计划-2026-09-05.md）：
- 分层生成流水线：灵感→作品大纲树（卷/章细纲）→章节正文→逐章摘要链；
- 每章生成注入四件套：前情摘要链 + 本章细纲 + 角色卡 + 世界观 RAG；
  RAG 直调 injection_service.retrieve 自拼注入，**不走 enhance_chat**
  ——其创作意图收敛逻辑（含「小说」正则）会把资料收敛到 3 条，
  对正文续写太抠（世界观细节要给够）；
- 串行任务队列（NovelJobQueue）：单 worker=对话模型单驻永不并发；
  API 落库 pending + 入队即返回 + 位次/进度轮询；取消=排队移除/
  运行检查点（步骤间 raise_if_cancelled）；
- 推理铁律：ensure_loaded 15s 级阻塞 + chat 秒级，一律经 run_blocking
  卸线程池，绝不在事件循环直调（对齐 storyboard._ai_split_block_sync）；
- 功能锁：生成属 dialog 资源域（批1 gpu_domains），由队列 worker 在
  每个任务开始前 acquire_or_raise("dialog")，毕即释放——多卡机器上
  与异卡的绘画天然并行，单卡机器与历史互斥语义一致。

MVP 明确不做：无人值守自动成书、多模型混排、富文本排版、整卷续写
（「一个一个来」：先跑通章粒度人在回路）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..data.database import get_db_safe
from .offload import run_blocking

log = logging.getLogger("omnispace.services.novel")

# ── 参数（MVP 口径）────────────────────────────────────────────
PREV_SUMMARY_WINDOW = 5    # 前情摘要回看章数
PREV_SUMMARY_CHARS = 200   # 单章摘要截断
PREV_TAIL_CHARS = 600      # 上一章结尾原文带入长度（衔接用）
RAG_TOP_K = 6              # 世界观检索条数
RAG_MAX_CHARS = 2000       # 世界观注入总长上限（对齐 injection 口径）
CHAR_MAX_CHARS = 120       # 单角色卡注入截断
MAX_CHARS_INJECT = 8       # 角色卡注入上限
MAX_OUTLINE_VOLUMES = 5
MAX_CHAPTERS_PER_VOLUME = 20
CHAPTER_MAX_TOKENS = 2048  # 正文生成长度（≈2000-3000 汉字）
PROMPT_BODY_MAX_CHARS = 3600  # 拼装后正文区上限（max_prefill_tokens=3072 安全线）
REPEAT_WINDOW = 80         # 复读检测窗口（字符）
REPEAT_MIN_LEN = 400       # 短于此不检测（超短文天然重复率高，免误伤）
# 防复读双发参数（实弹 19:54：rep 1.1/1.25 仍复读——加码频率惩罚+升温；
# 复读高发于细纲写完后的注水段，prompt 侧「可短不可注水」双管齐下）
ANTI_REPEAT_ATTEMPTS = (  # (repetition_penalty, temperature, frequency_penalty)
    (1.15, 0.85, 0.4),
    (1.3, 1.0, 0.7),
)
# 小说生成偏好模型（实弹 20:03：4B GGUF 是复读重灾区且参数曾被忽略）。
# 依次取第一个存在于 models/ 的：8B AWQ 走 vLLM（质量高、采样参数生效）；
# 都没有则落当前选中模型（GGUF 侧经 repeat_penalty 映射仍生效）。
NOVEL_PREFERRED_MODELS = ("qwen3-vl-8b-awq", "qwen3-vl-4b")

_VALID_LEVELS = ("book", "volume", "chapter_outline")
_JOB_PROGRESS: dict[str, dict] = {}   # task_id → {stage, detail}（内存态，照分镜切分进度）


class NovelEngineNotReady(RuntimeError):
    """对话模型未就绪且自动装载失败（报错带出路，前端原样展示）。"""


class NovelJobCancelled(Exception):
    """任务取消信号（排队移除/运行检查点共用）。"""


# ═══════════════════════════════════════════════════════════════
#  串行任务队列（单 worker = 对话模型单驻）
# ═══════════════════════════════════════════════════════════════

@dataclass
class NovelJob:
    """队列任务：kind ∈ outline|chapter|characters。"""
    task_id: str
    kind: str
    project_id: str = ""
    chapter_id: str = ""
    cancelled: bool = field(default=False, repr=False)
    run: Callable[[NovelJob, NovelJobQueue], Awaitable[None]] = None  # type: ignore[assignment]


class NovelJobQueue:
    """小说生成串行队列（asyncio 单 worker；进程内单例）。

    与 video_queue（daemon 线程版）的取舍差异：小说生成的重活全部经
    run_blocking 下沉线程池，worker 本体保持 async 即可自然串行，省去
    功能锁跨线程回投——worker 内直接 await acquire_or_raise("dialog")。
    """

    _instance: NovelJobQueue | None = None

    def __init__(self) -> None:
        self._jobs: deque[NovelJob] = deque()
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task | None = None
        self._current: NovelJob | None = None

    @classmethod
    def instance(cls) -> NovelJobQueue:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def submit(self, job: NovelJob) -> int:
        """入队（FIFO 尾部），返回排队位次（1 起，含本任务）。"""
        async with self._lock:
            self._jobs.append(job)
            pos = len(self._jobs)
        self._ensure_worker()
        log.info("小说任务入队: %s kind=%s 位次=%d", job.task_id, job.kind, pos)
        return pos

    def _ensure_worker(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._worker = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while True:
            async with self._lock:
                if not self._jobs:
                    self._worker = None
                    return
                job = self._jobs.popleft()
            if job.cancelled:
                continue
            self._current = job
            try:
                # 功能锁：dialog 资源域（批1）。排队等让位而非拒绝
                # （「一律受理、排队等跑」，同 video_queue 语义）。
                from ..middleware.feature_lock import acquire_or_raise
                lock = await acquire_or_raise("dialog", task_id=job.task_id)
                try:
                    await job.run(job, self)
                finally:
                    await lock.release("dialog")
            except NovelJobCancelled:
                log.info("小说任务已取消: %s", job.task_id)
            except Exception as exc:  # noqa: BLE001 - 失败留痕给前端横幅
                log.warning("小说任务异常收尾: %s %s", job.task_id, exc, exc_info=True)
                _JOB_PROGRESS[job.task_id] = {
                    "stage": "error",
                    "detail": f"生成失败：{exc}"[:200],
                }
            finally:
                self._current = None
                prog = _JOB_PROGRESS.get(job.task_id)
                if not (isinstance(prog, dict) and prog.get("stage") == "error"):
                    _JOB_PROGRESS.pop(job.task_id, None)

    def position(self, task_id: str) -> int | None:
        for i, j in enumerate(self._jobs):
            if j.task_id == task_id:
                return i + 1
        return None

    def request_cancel(self, task_id: str) -> str:
        """取消：'queued'（已出队）/ 'running'（置旗标，检查点收割）/
        'missing'。"""
        if self._current is not None and self._current.task_id == task_id:
            self._current.cancelled = True
            return "running"
        for j in self._jobs:
            if j.task_id == task_id:
                j.cancelled = True
                self._jobs.remove(j)
                return "queued"
        return "missing"

    def raise_if_cancelled(self, task_id: str) -> None:
        """运行中检查点：任务被置取消旗标即抛 NovelJobCancelled。"""
        if self._current is not None and self._current.task_id == task_id \
                and self._current.cancelled:
            raise NovelJobCancelled(task_id)

    def snapshot(self) -> dict:
        """队列快照（进度轮询端点消费）。"""
        return {
            "current": ({"task_id": self._current.task_id,
                         "kind": self._current.kind,
                         "project_id": self._current.project_id,
                         "chapter_id": self._current.chapter_id}
                        if self._current else None),
            "queue": [{"task_id": j.task_id, "kind": j.kind,
                       "project_id": j.project_id,
                       "chapter_id": j.chapter_id}
                      for j in self._jobs],
        }


def get_job_queue() -> NovelJobQueue:
    return NovelJobQueue.instance()


# ═══════════════════════════════════════════════════════════════
#  推理核（线程池内同步执行）与 RAG
# ═══════════════════════════════════════════════════════════════

# 引擎装载有界等待（秒）：与对话 API 层同口径——冷启动/预热占显存时
# 排队等装载完成，而不是立刻失败（用户产品铁律：不许「再点一次」）
ENGINE_WAIT_S = 300.0


def _ensure_engine_sync() -> str:
    """确保对话引擎就绪且尽量用大模型（同步核，必须经 run_blocking）。

    云端路由（2026-09-06 文本槽位拆分）：「写作台」工位绑定云端连接时
    优先 ensure_loaded(cloud::…)——引擎内部完成本地↔云端切换（就绪
    同目标=matches_target 短路），返回 "cloud:<模型>" 标识；未绑定保持
    本地偏好模型链旧行为。

    本地路径：优先装 NOVEL_PREFERRED_MODELS 里第一个存在的（8B AWQ=
    vLLM：长文质量高且防复读采样参数真实生效）；偏好都缺失则装载当前
    选中模型。装载失败做有界轮询（ENGINE_WAIT_S，与对话页同口径）。
    返回实际就绪的模型 id（排障用）。
    """
    from ..config import MODELS_DIR
    from .inference.dialog_engine import get_dialog_engine
    eng = get_dialog_engine()
    # ── 写作台云端槽位（小说队列已持 dialog 功能锁，云端调用不占
    #    本地显存，锁持有无害）──
    try:
        from .cloud_provider_service import resolve_slot_cloud_model
        _cloud_model = resolve_slot_cloud_model("novel.text")
    except Exception:  # noqa: BLE001 - 解析失败按本地
        _cloud_model = None
    if _cloud_model:
        if not eng.ensure_loaded(_cloud_model):
            raise NovelEngineNotReady(
                "云端写作连接未就绪：" + (eng.last_error() or "请到"
                "「设置 → 云端 API 服务」点「测试连接」排查"))
        # backend.model_id = 裸模型名（remote load 内已剥 cloud:: 前缀）；
        # eng.model_name 是完整虚拟 id（含 cloud:: 前缀），直接拼会双前缀
        return f"cloud:{getattr(eng._backend, 'model_id', '') or 'unknown'}"
    for mid in NOVEL_PREFERRED_MODELS:
        if (MODELS_DIR / mid).is_dir():
            if eng.is_ready and eng.model_name == mid:
                return mid
            # 先卸当前对话模型腾显存：ensure_loaded 的显存准入「只看
            # 不腾」，开机预热常驻的 4B 不卸，8B 永远装不上（实弹
            # 20:14 事故）。此刻小说队列已持有 dialog 功能锁，与用户
            # 对话不会抢跑。
            cur = eng.model_name if eng.is_ready else ""
            if cur and cur != mid:
                try:
                    from .model_manager import get_model_manager
                    get_model_manager().unload_model(cur)
                    log.info("小说换装模型: 已卸 %s 腾显存给 %s", cur, mid)
                except Exception as exc:  # noqa: BLE001 - 卸载失败继续试
                    log.warning("卸载 %s 失败（继续尝试装载）: %s", cur, exc, exc_info=True)
            eng.ensure_loaded(mid)
            if eng.is_ready:
                log.info("小说生成使用对话模型: %s", mid)
                return mid
            log.warning("偏好模型 %s 装载失败，尝试下一个偏好", mid)
    deadline = time.time() + ENGINE_WAIT_S
    while True:
        if eng.is_ready or eng.ensure_loaded():
            log.info("小说生成使用对话模型(当前选中): %s",
                     eng.model_name or "unknown")
            return eng.model_name or "unknown"
        if time.time() >= deadline:
            raise NovelEngineNotReady(
                "对话模型未就绪且自动装载失败（已等待 5 分钟）：请到"
                "「模型管理」页确认对话引擎/模型状态后重试；若绘画等"
                "其他功能正占用显存，等其结束后再试")
        time.sleep(10.0)


def _llm_sync(prompt: str, *, temperature: float = 0.7,
              max_new_tokens: int = 1024,
              extra_params: dict | None = None) -> str:
    """对话引擎同步推理核（必须经 run_blocking 调用）。

    未就绪先走 ensure_loaded 自动装载链（用户产品铁律：不许「再点
    一次」）；装载失败（含开机预热占满显存的窗口期）做有界轮询
    （ENGINE_WAIT_S，与对话页同口径），超时才报错且带出路。
    """
    from .inference.dialog_engine import get_dialog_engine
    eng = get_dialog_engine()
    deadline = time.time() + ENGINE_WAIT_S
    while True:
        if eng.is_ready or eng.ensure_loaded():
            break
        if time.time() >= deadline:
            raise NovelEngineNotReady(
                "对话模型未就绪且自动装载失败（已等待 5 分钟）：请到"
                "「模型管理」页确认对话引擎/模型状态后重试；若绘画等"
                "其他功能正占用显存，等其结束后再试")
        time.sleep(10.0)
    reply = eng.chat([{"role": "user", "content": prompt}],
                     temperature=temperature,
                     max_new_tokens=max_new_tokens,
                     extra_params=extra_params)
    return reply or ""


def retrieve_worldview_sync(query: str,
                            top_k: int = RAG_TOP_K) -> str:
    """世界观 RAG 检索并拼注入块（同步核；纯函数式拼装，可测）。

    直调 retrieve（不走 enhance_chat 的创作收敛），总量钳
    RAG_MAX_CHARS；空库/异常返回空串不阻断生成。
    """
    if not query or not query.strip():
        return ""
    try:
        from .injection_service import get_injection_service
        rows = get_injection_service().retrieve(query.strip(),
                                                top_k=top_k) or []
    except Exception as exc:  # noqa: BLE001 - 检索失败不阻断正文生成
        log.warning("世界观检索失败（按无资料继续）: %s", exc, exc_info=True)
        return ""
    blocks: list[str] = []
    total = 0
    for r in rows:
        c = str(r.get("content") or "").strip()
        if not c:
            continue
        if total + len(c) > RAG_MAX_CHARS:
            break
        blocks.append(f"- {c}")
        total += len(c) + 2
    return "\n".join(blocks)


# ═══════════════════════════════════════════════════════════════
#  Prompt 拼装（纯函数，可测）
# ═══════════════════════════════════════════════════════════════

# 批3 P13（2026-09-19）：降AI味档位指令（注入章节正文 prompt 尾部）。
# standard=现状零注入；light=句长/套话两件；heavy=light 基础上叠五件。
_STYLE_PRESET_BLOCKS: dict[str, str] = {
    "standard": "",
    "light": (
        "- 拟真要求·轻：写得像真人写手——句长自然错落，长短句交替；"
        "少用四字格与排比句；少写总结式陈述，多用具体动作和细节呈现情绪；\n"
    ),
    "heavy": (
        "- 拟真要求·重：写得像真人写手，避开一切AI腔——\n"
        "  · 句长错落，允许一句成段；段落长短交替，别每段都差不多大；\n"
        "  · 用词避开通用高频词（如\"瞬间\"\"不禁\"\"顿时\"），换成更具体"
        "贴切的说法；\n"
        "  · 禁用\"仿佛/宛如/犹如/恍若\"类模板比喻，要比喻就用新鲜的、"
        "带生活质感的；\n"
        "  · 情感要有波动层次：同一场景里人物情绪会转折，不要从头到尾"
        "一个浓度；\n"
        "  · 不同人物说话语气要有区分（口头禅/句式长短/用词习惯）；"
        "不写\"总之/综上\"式收束；\n"
    ),
}


def build_outline_prompt(*, description: str, genre: str,
                         style_notes: str,
                         requirement: str = "",
                         plan_chapters: int = 0,
                         volume_count: int = 0,
                         words_per_chapter: int = 0) -> str:
    # 批3 P4（2026-09-19）：篇幅规划三参数约束出纲。LLM 守数是「约」
    # 语义（落库后如实显示实际数）；不传时维持历史缺省完全兼容。
    if plan_chapters > 0 or volume_count > 0 or words_per_chapter > 0:
        vol_txt = (f"分 {volume_count} 卷" if volume_count > 0
                   else "分卷由你合理安排")
        plan_txt = (f"- 篇幅规划：全书约 {plan_chapters} 章、{vol_txt}"
                    if plan_chapters > 0 else f"- 篇幅规划：{vol_txt}")
        if words_per_chapter > 0:
            plan_txt += (f"，每章正文约 {words_per_chapter} 字"
                         "（细纲信息量以撑起该字数为度）")
        plan_txt += "；章数贴近计划，允许 ±2 章浮动；\n"
    else:
        plan_txt = "- 共 2~3 卷，每卷 4~6 章（总章数不超过 15，宁精勿多）；\n"
    return (
        "你是资深小说主编。请根据以下灵感设定，创作一部长篇小说的分层大纲。\n\n"
        f"【作品灵感】{description or '（由你自由发挥一个高概念创意）'}\n"
        f"【题材】{genre or '不限'}\n"
        f"【文风/基调】{style_notes or '不限'}\n"
        f"【补充要求】{requirement or '无'}\n\n"
        "要求：\n"
        + plan_txt +
        "- 卷概要写清本卷主线冲突与结局；章细纲 60~100 字，写清事件与钩子；\n"
        "- 只输出 JSON，禁止输出任何其他文字或代码块标记，格式：\n"
        '{"title":"书名","logline":"一句话主线",'
        '"volumes":[{"title":"卷名","summary":"本卷概要",'
        '"chapters":[{"title":"章名","outline":"本章细纲"}]}]}\n\n'
        "JSON："
    )


def build_chapter_prompt(*, project_name: str, genre: str,
                         style_notes: str, chapter_title: str,
                         outline_text: str,
                         prev_summaries: list[tuple[int, str]],
                         prev_tail: str, characters: list[dict],
                         worldview: str,
                         target_words: int = 1500,
                         style_preset: str = "standard") -> str:
    """章节正文 prompt：前情摘要链 + 上一章结尾 + 本章细纲 + 角色卡 +
    世界观 RAG 四件套（各段独立钳长，总量钳 PROMPT_BODY_MAX_CHARS）。

    批3 P13（2026-09-19）style_preset 降AI味档位：standard=现状；
    light/heavy 注入拟真指令（heavy 在 light 上叠加低频词/情感维度/
    禁模板句/段落节奏/人物语气五件）。
    """
    style = style_notes.strip() or "现代白话，视角统一，节奏明快，画面感强"
    sum_lines = [
        f"第{idx}章：{s[:PREV_SUMMARY_CHARS]}"
        for idx, s in prev_summaries[-PREV_SUMMARY_WINDOW:]
    ]
    char_lines = [
        f"- {(c.get('name') or '').strip()}"
        f"（{(c.get('role') or '').strip() or '角色'}）："
        f"{(c.get('summary') or '').strip()[:CHAR_MAX_CHARS]}"
        for c in characters[:MAX_CHARS_INJECT] if (c.get("name") or "").strip()
    ]
    parts = [
        "你是一位网文写手。请依据以下资料撰写小说正文的一章。\n",
        f"【作品】《{project_name}》（{genre or '不限题材'}）",
        f"【文风要求】{style}",
    ]
    if sum_lines:
        parts.append("【前情摘要】\n" + "\n".join(sum_lines))
    if prev_tail.strip():
        parts.append("【上一章结尾】\n" + prev_tail.strip()[-PREV_TAIL_CHARS:])
    outline_display = outline_text.strip() or (
        "（无细纲，按前情自然推进一个完整事件）")
    parts.append(
        f"【本章题目】{chapter_title or '（无题）'}\n"
        f"【本章细纲】{outline_display}")
    if char_lines:
        parts.append("【主要角色】\n" + "\n".join(char_lines))
    if worldview.strip():
        parts.append("【世界观资料】（仅供参考，与本章无关的忽略）\n"
                     + worldview.strip()[:RAG_MAX_CHARS])
    parts.append(
        "写作要求：\n"
        "- 直接输出正文：不要章节号、不要标题行、不要任何解释或括号注记；\n"
        f"- 篇幅约 {target_words} 字，可短不可注水：细纲事件写完就自然"
        "收束结尾，绝不为了凑字数重复或展开新支线；\n"
        "- 情节贴合【本章细纲】，开头自然衔接【上一章结尾】；\n"
        "- 人物言行符合【主要角色】人设，设定遵循【世界观资料】；\n"
        "- 严禁复读：同一句话或同一段落绝对不得出现第二次，情节推进"
        "后不回头、不换说法重写已发生的事。\n"
        + _STYLE_PRESET_BLOCKS.get(style_preset, "")
        + "\n正文：")
    body = "\n\n".join(parts)
    # 总量安全钳（max_prefill_tokens=3072 的汉字安全线）
    if len(body) > PROMPT_BODY_MAX_CHARS + 400:
        body = body[:PROMPT_BODY_MAX_CHARS + 400] + "\n（资料过长已截断）\n\n正文："
    return body


def build_summary_prompt(chapter_title: str, content: str) -> str:
    return (
        f"以下是小说一章的正文（题目：《{chapter_title}》）。"
        "用不超过 120 字概括本章剧情要点（关键人物/事件/埋下的伏笔），"
        "直接输出概述，不要任何前缀：\n\n"
        + content[:4000])


def build_characters_prompt(*, description: str, genre: str,
                            basis: str = "") -> str:
    return (
        "你是小说策划。请为以下作品设计主要角色卡。\n\n"
        f"【作品灵感】{description or '（自由发挥）'}\n"
        f"【题材】{genre or '不限'}\n"
        f"【补充要求】{basis or '无'}\n\n"
        "要求：设计 3~6 个核心角色，summary 写清外貌特征/性格/动机/口癖"
        "（60~120 字）；只输出 JSON 数组，禁止其他文字，格式：\n"
        '[{"name":"角色名","role":"主角|配角|反派","summary":"人设"}]\n\n'
        "JSON：")


def repair_json_prefix(payload: str) -> str:
    """截断 JSON 抢救（纯函数）：回退到最后一个完整值边界并补齐闭合括号。

    适用「模型输出撞 token 上限被拦腰截断」场景：截断点前的结构
    本身是合法前缀，砍掉残尾、补上缺失的 }/] 即可整体可解析。
    全文本就完整时原样返回；完全无法定位边界时原样返回（由上层
    抛原始解析错误）。
    """
    stack: list[str] = []
    in_str = False
    esc = False
    last_safe = -1
    for i, ch in enumerate(payload):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            if stack:
                # 仍包在外层结构内 → 此处之后都是可丢弃残尾
                last_safe = i
    if last_safe < 0:
        return payload
    cut = payload[:last_safe + 1]
    stack2: list[str] = []
    in_str = False
    esc = False
    for ch in cut:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack2.append(ch)
        elif ch in "}]":
            if stack2:
                stack2.pop()
    return cut + "".join("}" if c == "{" else "]" for c in reversed(stack2))


def parse_outline_json(raw: str) -> dict:
    """解析大纲 LLM 输出（剥代码围栏 → JSON → 截断抢救 → 结构校验/钳量）。

    解析失败/结构非法抛 ValueError（带原文头 200 字便于排障）。
    """
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text,
                  flags=re.MULTILINE).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"大纲输出不含 JSON：{text[:200]}")
    payload = text[start:end + 1]
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        # 输出撞 token 上限被截断（实弹 18:55：char 2298 处断裂）——
        # 回退到最后一个完整值边界并补齐闭合括号后重试
        data = json.loads(repair_json_prefix(payload))
    if not isinstance(data, dict) or not isinstance(
            data.get("volumes"), list) or not data["volumes"]:
        raise ValueError(f"大纲 JSON 缺 volumes：{text[:200]}")
    vols = data["volumes"][:MAX_OUTLINE_VOLUMES]
    for v in vols:
        if not isinstance(v, dict):
            raise ValueError("volumes 元素非法")
        v["title"] = str(v.get("title") or "").strip() or "未命名卷"
        v["summary"] = str(v.get("summary") or "").strip()
        chs = v.get("chapters")
        v["chapters"] = chs[:MAX_CHAPTERS_PER_VOLUME] if isinstance(
            chs, list) else []
        for c in v["chapters"]:
            if isinstance(c, dict):
                c["title"] = str(c.get("title") or "").strip() or "未命名章"
                c["outline"] = str(c.get("outline") or "").strip()
    data["title"] = str(data.get("title") or "").strip() or "未命名小说"
    data["logline"] = str(data.get("logline") or "").strip()
    data["volumes"] = vols
    return data


def parse_characters_json(raw: str) -> list[dict]:
    """解析角色卡 LLM 输出（同 parse_outline_json 的宽容策略）。"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text,
                  flags=re.MULTILINE).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError(f"角色卡输出不含 JSON 数组：{text[:200]}")
    payload = text[start:end + 1]
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = json.loads(repair_json_prefix(payload))
    if not isinstance(data, list):
        raise ValueError("角色卡 JSON 非数组")
    out: list[dict] = []
    for item in data[:8]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append({"name": name,
                    "role": str(item.get("role") or "").strip(),
                    "summary": str(item.get("summary") or "").strip()})
    if not out:
        raise ValueError("角色卡解析为空")
    return out


def clean_chapter_text(raw: str) -> str:
    """正文清洗：剥围栏/标题行/前置客套（纯函数，可测）。"""
    text = (raw or "").strip()
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.MULTILINE)
    lines = text.splitlines()
    kept: list[str] = []
    for _i, ln in enumerate(lines):
        s = ln.strip()
        if not kept and s and (re.match(r"^第[一二三四五六七八九十百千0-9]+章",
                                        s)
                               or re.match(r"^Chapter\s", s, re.IGNORECASE)
                               or s.startswith(("《", "以下是", "好的", "正文"))
                               and len(s) < 40):
            continue  # 丢弃开头处的标题行/客套话
        kept.append(ln)
    return "\n".join(kept).strip()


def detect_repetition(text: str, window: int = REPEAT_WINDOW) -> str | None:
    """复读循环检测（纯函数，可测）：任一定长窗口在全文出现 ≥2 次即
    判定复读，返回该窗口片段；干净文本返回 None。

    针对 2026-09-05 实弹事故：8B 模型长文生成时同一段落原样反复抄写
    （用户实测同 500 字块连抄 3 遍）。滑窗+子串计数，正文 ≤4k 字量级
    毫秒级；超短文本免检（REPEAT_MIN_LEN，天然重复率高防误伤）。
    """
    t = re.sub(r"\s+", "", text or "")
    if len(t) < REPEAT_MIN_LEN:
        return None
    step = max(1, window // 2)
    for i in range(0, len(t) - window + 1, step):
        frag = t[i:i + window]
        if t.count(frag) >= 2:
            return frag
    return None


# ═══════════════════════════════════════════════════════════════
#  伏笔账本检查（纯函数，可测）
# ═══════════════════════════════════════════════════════════════

def check_foreshadows(rows: list[dict], chapters: list[dict]) -> dict:
    """伏笔体检：未回收清单 + 回收章缺失（dangling）。

    rows: novel_foreshadows 行；chapters: novel_chapters 轻行
    （id/chapter_index/title）。纯函数无 IO。
    """
    idx_map = {c.get("id"): int(c.get("chapter_index") or 0)
               for c in chapters}
    unrecalled: list[dict] = []
    dangling: list[dict] = []
    for r in rows:
        if (r.get("status") or "") == "planted":
            cid = r.get("planted_chapter_id") or ""
            unrecalled.append({
                "id": r.get("id"),
                "description": r.get("description"),
                "planted_chapter_index": idx_map.get(cid) if cid else None,
            })
        if (r.get("status") or "") == "payoff" and (
                r.get("payoff_chapter_id")
                and r["payoff_chapter_id"] not in idx_map):
            dangling.append({"id": r.get("id"),
                             "description": r.get("description")})
    return {"planted_total": sum(
                1 for r in rows if r.get("status") == "planted"),
            "payoff_total": sum(
                1 for r in rows if r.get("status") == "payoff"),
            "dropped_total": sum(
                1 for r in rows if r.get("status") == "dropped"),
            "unrecalled": unrecalled,
            "dangling_payoff": dangling}


# ═══════════════════════════════════════════════════════════════
#  导出拼装（纯函数，可测）
# ═══════════════════════════════════════════════════════════════

def build_export_text(project: dict, chapters: list[dict],
                      fmt: str = "txt") -> str:
    """整书导出：txt=纯文本；md=Markdown（章标题 ###）。"""
    title = project.get("name") or "未命名小说"
    parts: list[str] = []
    if fmt == "md":
        parts.append(f"# {title}")
        if project.get("description"):
            parts.append(f"> {project['description']}")
    else:
        parts.append(title)
        if project.get("description"):
            parts.append(project["description"])
        parts.append("=" * 24)
    for ch in sorted(chapters, key=lambda c: c.get("chapter_index") or 0):
        heading = f"第{ch.get('chapter_index') or 0}章 {ch.get('title') or ''}".strip()
        body = (ch.get("content") or "").strip()
        if fmt == "md":
            parts.append(f"\n### {heading}\n")
        else:
            parts.append(f"\n{heading}\n")
        parts.append(body or "（本章暂无正文）")
    return "\n".join(parts) + "\n"


# ═══════════════════════════════════════════════════════════════
#  任务 runner（队列 worker 内 await 执行）
# ═══════════════════════════════════════════════════════════════

def _now() -> float:
    return time.time()


def _db():
    db = get_db_safe()
    if db is None:
        raise RuntimeError("数据库不可用")
    return db


def _get_project(db, project_id: str) -> dict | None:
    return db.query_one("SELECT * FROM novel_projects WHERE id=?",
                        (project_id,))


async def run_outline_job(job: NovelJob, queue: NovelJobQueue) -> None:
    """大纲树生成：灵感 → 卷/章细纲 → 落库（含 chapter 待写行）。"""
    db = _db()
    project = _get_project(db, job.project_id)
    if project is None:
        raise RuntimeError(f"项目不存在: {job.project_id}")
    await run_blocking(_ensure_engine_sync)
    _JOB_PROGRESS[job.task_id] = {"stage": "generating", "detail": "构思大纲中"}
    req = _JOB_PROGRESS[job.task_id]
    # 批3 P4：篇幅规划三参数（建作品时存 meta；旧项目无此键=历史缺省）。
    # meta 在 DB 行里是 JSON 字符串——旧代码 isinstance(dict) 守卫对字符串
    # 恒 False，requirement 实为死参；此处经 parse_json_field 修正。
    _om = parse_json_field(project.get("meta"))
    _meta = _om if isinstance(_om, dict) else {}
    plan_chapters = int(_meta.get("plan_chapters") or 0)
    volume_count = int(_meta.get("volume_count") or 0)
    words_per_chapter = int(_meta.get("words_per_chapter") or 0)
    # 章数多时大纲 JSON 变长，生成上限随计划章数放宽（每章细纲≈120token）
    outline_tokens = 3072 + (max(0, plan_chapters - 15) * 150 if plan_chapters else 0)
    prompt = build_outline_prompt(
        description=project.get("description") or "",
        genre=project.get("genre") or "",
        style_notes=project.get("style_notes") or "",
        requirement=str(_meta.get("requirement") or ""),
        plan_chapters=plan_chapters,
        volume_count=volume_count,
        words_per_chapter=words_per_chapter)
    raw = await run_blocking(
        _llm_sync, prompt, temperature=0.75,
        max_new_tokens=min(outline_tokens, 8192))
    queue.raise_if_cancelled(job.task_id)
    req["detail"] = "解析大纲结构"
    data = await run_blocking(parse_outline_json, raw)

    # 落库：旧大纲树与待写章清掉重建（人在回路 = 生成后逐节点可改）
    pid = job.project_id
    now = _now()
    await run_blocking(lambda: db.delete("novel_chapters", "project_id=? AND status='pending'", (pid,)))
    await run_blocking(lambda: db.delete("novel_outlines", "project_id=?", (pid,)))
    book_id = uuid.uuid4().hex
    await run_blocking(lambda: db.insert("novel_outlines", {
        "id": book_id, "project_id": pid, "parent_id": "", "level": "book",
        "sort_index": 0, "title": data["title"],
        "content": data["logline"], "created_at": now, "updated_at": now}))
    ch_no = 0
    for vi, vol in enumerate(data["volumes"]):
        vol_id = uuid.uuid4().hex
        await run_blocking(lambda vi=vi, vol=vol, vol_id=vol_id: db.insert("novel_outlines", {
            "id": vol_id, "project_id": pid, "parent_id": book_id,
            "level": "volume", "sort_index": vi, "title": vol["title"],
            "content": vol["summary"], "created_at": now,
            "updated_at": now}))
        for ci, ch in enumerate(vol.get("chapters") or []):
            ch_no += 1
            node_id = uuid.uuid4().hex
            await run_blocking(lambda ch=ch, ci=ci, node_id=node_id, vol_id=vol_id: db.insert("novel_outlines", {
                "id": node_id, "project_id": pid, "parent_id": vol_id,
                "level": "chapter_outline", "sort_index": ci,
                "title": ch["title"], "content": ch["outline"],
                "created_at": now, "updated_at": now}))
            await run_blocking(lambda ch=ch, ch_no=ch_no, node_id=node_id: db.insert("novel_chapters", {
                "id": uuid.uuid4().hex, "project_id": pid,
                "outline_id": node_id, "chapter_index": ch_no,
                "title": ch["title"], "status": "pending",
                "created_at": now, "updated_at": now}))
    req["stage"] = "done"
    req["detail"] = f"完成：{len(data['volumes'])} 卷 {ch_no} 章"
    from ..services.event_log import log_event
    log_event("novel", "outline_generated",
              f"《{project.get('name')}》大纲生成完成：{len(data['volumes'])}卷 {ch_no}章",
              level="success")


async def run_chapter_job(job: NovelJob, queue: NovelJobQueue) -> None:
    """章节正文生成：四件套注入 → 正文 → 摘要链回写。"""
    db = _db()
    ch = await run_blocking(lambda: db.query_one("SELECT * FROM novel_chapters WHERE id=?",
                      (job.chapter_id,)))
    if ch is None:
        raise RuntimeError(f"章节不存在: {job.chapter_id}")
    project = _get_project(db, job.project_id)
    if project is None:
        raise RuntimeError(f"项目不存在: {job.project_id}")
    await run_blocking(lambda: db.update("novel_chapters",
              {"status": "generating", "progress": 0.05, "error": ""},
              "id=?", (job.chapter_id,)))
    try:
        await run_blocking(_ensure_engine_sync)
        outline = (await run_blocking(lambda: db.query_one(
            "SELECT * FROM novel_outlines WHERE id=?",
            (ch.get("outline_id") or "",)))
            if ch.get("outline_id") else None)
        prev_rows = await run_blocking(lambda: db.query(
            "SELECT id, chapter_index, title, summary, content, status "
            "FROM novel_chapters WHERE project_id=? AND chapter_index<? "
            "ORDER BY chapter_index",
            (job.project_id, ch.get("chapter_index") or 0)))
        summaries = [(r["chapter_index"], r.get("summary") or "")
                     for r in prev_rows
                     if r.get("status") == "done" and (r.get("summary") or "").strip()]
        prev_tail = ""
        if prev_rows:
            last = prev_rows[-1]
            if last.get("status") == "done":
                prev_tail = (last.get("content") or "")[-PREV_TAIL_CHARS:]
        characters = await run_blocking(lambda: db.query(
            "SELECT name, role, summary FROM novel_characters "
            "WHERE project_id=? ORDER BY created_at", (job.project_id,)))

        outline_text = (outline.get("content") if outline else "") or \
            str((parse_json_field(ch.get("meta")) or {}).get("outline") or "")
        query = " ".join(x for x in (
            project.get("genre"), project.get("description"),
            ch.get("title") or "", outline_text) if x)
        worldview = await run_blocking(retrieve_worldview_sync, query)
        queue.raise_if_cancelled(job.task_id)
        await run_blocking(lambda: db.update("novel_chapters", {"progress": 0.3}, "id=?",
                  (job.chapter_id,)))

        # 批3 P4/P13：篇幅与文风从作品 meta 读（建作品时存；可随时改）
        _pm = parse_json_field(project.get("meta"))
        _meta = _pm if isinstance(_pm, dict) else {}
        words_target = int(_meta.get("words_per_chapter") or 0) or 1500
        style_preset = str(_meta.get("style_preset") or "standard")
        if style_preset not in _STYLE_PRESET_BLOCKS:
            style_preset = "standard"
        prompt = build_chapter_prompt(
            project_name=project.get("name") or "",
            genre=project.get("genre") or "",
            style_notes=project.get("style_notes") or "",
            chapter_title=ch.get("title") or "",
            outline_text=outline_text,
            prev_summaries=summaries,
            prev_tail=prev_tail,
            characters=characters,
            worldview=worldview,
            target_words=words_target,
            style_preset=style_preset)
        # 生成上限随目标字数缩放（汉字≈0.75token/字 +512 余量，
        # 上限钳 CHAPTER_MAX_TOKENS×2 防超长耗时；1500 字≈历史 2048 档）
        chapter_tokens = min(int(words_target * 0.75) + 512,
                             CHAPTER_MAX_TOKENS * 2)
        # 防复读双发（2026-09-05 实弹：8B 模型 2 千字级长文会陷入复读
        # 循环，同段反复抄写）——首发带 repetition_penalty=1.1；检出
        # 复读则升温加压重试一次；两连复读诚实报错带出路，拒绝把
        # 低质量正文写进库。
        content = ""
        # 批3 P13：重拟真档略升温（多样性与拟真度正相关；轻/标准不动）
        _temp_delta = 0.05 if style_preset == "heavy" else 0.0
        for attempt, (rep_penalty, temp, freq_penalty) in enumerate(
                ANTI_REPEAT_ATTEMPTS, start=1):
            raw = await run_blocking(
                _llm_sync, prompt, temperature=temp + _temp_delta,
                max_new_tokens=chapter_tokens,
                extra_params={"repetition_penalty": rep_penalty,
                              "frequency_penalty": freq_penalty})
            queue.raise_if_cancelled(job.task_id)
            candidate = await run_blocking(clean_chapter_text, raw)
            repeated = await run_blocking(detect_repetition, candidate)
            if repeated is None:
                content = candidate
                break
            log.warning(
                "章节生成第 %d 次检出复读循环（片段=%s…），%s",
                attempt, repeated[:30],
                "升温加压重试" if attempt < len(ANTI_REPEAT_ATTEMPTS)
                else "放弃入库")
        if not content:
            raise RuntimeError(
                "模型连续多次陷入复读循环（同段反复抄写），已拒绝把低"
                "质量正文入库；请点「重新生成本章」再试一次，若反复出现"
                "建议把本章细纲拆得更细或降低目标篇幅")
        await run_blocking(lambda: db.update("novel_chapters", {"progress": 0.7}, "id=?",
                  (job.chapter_id,)))
        summary = ""
        if content:
            try:
                summary = await run_blocking(
                    _llm_sync, build_summary_prompt(
                        ch.get("title") or "", content),
                    temperature=0.2, max_new_tokens=256)
                summary = summary.strip().splitlines()[0][:300] \
                    if summary.strip() else ""
            except Exception as exc:  # noqa: BLE001 - 摘要失败不影响正文
                log.warning("章节摘要生成失败（正文已保留）: %s", exc, exc_info=True)
        word_count = len(content)
        await run_blocking(lambda content=content: db.update("novel_chapters", {
            "content": content, "summary": summary,
            "word_count": word_count, "status": "done", "progress": 1.0,
            "updated_at": _now()}, "id=?", (job.chapter_id,)))
        log.info("章节生成完成: %s《%s》%d 字",
                 job.chapter_id, ch.get("title"), word_count)
    except NovelJobCancelled:
        await run_blocking(lambda: db.update("novel_chapters", {
            "status": "error", "progress": 0.0,
            "error": "已取消（可重新生成）", "updated_at": _now()},
            "id=?", (job.chapter_id,)))
    except Exception as exc:
        err_msg = str(exc)[:300]  # exc 出 except 块即被删——先快照进闭包
        await run_blocking(lambda: db.update("novel_chapters", {
            "status": "error", "progress": 0.0,
            "error": err_msg, "updated_at": _now()},
            "id=?", (job.chapter_id,)))
        raise


async def run_characters_job(job: NovelJob, queue: NovelJobQueue) -> None:
    """角色卡批量生成（一次 LLM 调用产出 3~6 张，照 comic_asset 批量范式）。"""
    db = _db()
    project = _get_project(db, job.project_id)
    if project is None:
        raise RuntimeError(f"项目不存在: {job.project_id}")
    await run_blocking(_ensure_engine_sync)
    _JOB_PROGRESS[job.task_id] = {"stage": "generating", "detail": "设计角色中"}
    basis = str(job.chapter_id or "")  # 复用字段带补充要求（API 层填入）
    raw = await run_blocking(
        _llm_sync, build_characters_prompt(
            description=project.get("description") or "",
            genre=project.get("genre") or "", basis=basis),
        temperature=0.8, max_new_tokens=1200)
    queue.raise_if_cancelled(job.task_id)
    items = await run_blocking(parse_characters_json, raw)
    now = _now()
    for it in items:
        await run_blocking(lambda it=it: db.insert("novel_characters", {
            "id": uuid.uuid4().hex, "project_id": job.project_id,
            "name": it["name"], "role": it["role"],
            "summary": it["summary"], "created_at": now,
            "updated_at": now}))
    _JOB_PROGRESS[job.task_id] = {"stage": "done",
                                  "detail": f"完成：{len(items)} 个角色"}


def parse_json_field(value: Any) -> Any:
    """novel 表 meta JSON 列反序列化（容错）。"""
    if isinstance(value, (dict, list)) or value is None:
        return value
    try:
        return json.loads(value) if value else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def job_progress(task_id: str) -> dict:
    return _JOB_PROGRESS.get(task_id, {"stage": "queued", "detail": "排队中"})


def job_failures(limit: int = 3) -> list[dict]:
    """最近失败留痕（进度轮询端点消费，前端据此亮错误横幅）。"""
    out: list[dict] = []
    for tid, meta in _JOB_PROGRESS.items():
        if isinstance(meta, dict) and meta.get("stage") == "error":
            out.append({"task_id": tid, "stage": "error",
                        "detail": str(meta.get("detail") or "")})
    return out[-limit:]


def import_worldbuilding_sync(content: str, topic: str) -> int:
    """世界观素材入库（同步核，经 run_blocking 调用）。

    走 knowledge_service.process_page 完整管线（分段→评估→质检闸
    knowledge_quality_gate→向量化），返回新增条数。
    """
    from .knowledge_service import get_knowledge_service
    added = get_knowledge_service().process_page(content, topic=topic)
    return len(added or [])
