"""漫画模块 C2：AI 写分格脚本（2026-09-08，参考 AIMangaStudio 思路）。

故事梗概 → N 格画面描述，一键填充漫画工作台分格网格。

路线（2026-09-08 用户裁定「先本地」）：只走本地对话引擎
（module_config 的漫剧槽模型，缺省 qwen3-vl-4b）；云端 manga.text
槽接线挂 docs/未完成清单.md，复苏时补 5 行槽位解析即接
（模式参考 storyboard._ai_split_block_sync）。

实现口径与漫剧 AI 切分/描述词链同构：dialog 功能锁 + ensure_loaded
自动装载（未加载必须全自动铁律）+ run_blocking 卸载推理 + JSON
数组解析（代码围栏剥离 + 截断抢救，复用小说模块 repair_json_prefix）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Body
from pydantic import BaseModel, Field, ValidationError

from ...data.database import get_db_safe
from ...middleware.error_handler import ApiError, ok
from ...middleware.feature_lock import acquire_or_raise
from ...services.inference.dialog_engine import get_dialog_engine
from ...services.offload import run_blocking
from .common import (
    _ensure_storyboard,
    _make_row,
    _now,
    _public_row_to_db,
    _row_to_storyboard_row,
    manga_dialog_model_id,
)

log = logging.getLogger("omnispace.api.manga.comic_script")

router = APIRouter()

# 格数边界：太少不成页、太多一次推理挤 token（12 格 × 80 字 ≈ 千级 token）
MAX_PANELS = 12

_SCRIPT_PROMPT = """你是资深漫画分镜师。把下面的故事梗概拆分成 {n} 格漫画分格脚本。

要求：
- 每格输出一段中文画面描述（40~80 字），写清人物动作、场景环境、情绪与构图视角；
- 各格按剧情推进排序，节奏有起伏（起-承-转-合）；
- 相邻各格描述不得互相复读；
- 只输出 JSON 数组，不要任何解释或代码围栏，格式：["第一格画面描述", "第二格画面描述"]。

故事梗概：
{story}
"""


class ComicScriptRequest(BaseModel):
    """AI 写分格请求体。"""
    project_id: str = Field(min_length=1, max_length=64)
    story: str = Field(min_length=1, max_length=2000)
    panels: int = Field(default=4, ge=1, le=MAX_PANELS)
    # True=清空现有分格后填充；False=追加到网格尾部
    replace: bool = False


def build_script_prompt(story: str, panels: int) -> str:
    """分镜提示词（纯函数，测试锁定用）。"""
    return _SCRIPT_PROMPT.format(n=panels, story=story.strip())


def _salvage_json_array(payload: str) -> str:
    """顶层数组截断抢救（纯函数）：砍残尾、补 `]`。

    novel_service.repair_json_prefix 只回退「嵌套在外层结构内」的
    边界，顶层数组（栈空）永远不触发——分格输出恰是顶层数组，故此处
    自带数组版：记录最后一个「完整字符串元素」的结束位置（数组一层
    深度内），解析失败时截到该处并闭合。找不到边界原样返回（上层抛
    原始错误）。
    """
    stack: list[str] = []
    in_str = False
    esc = False
    last_elem_end = -1
    for i, ch in enumerate(payload):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                if stack and stack[-1] == "[" and len(stack) == 1:
                    last_elem_end = i  # 顶层数组内一个完整字符串值结束
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    if last_elem_end < 0:
        return payload
    cut = payload[:last_elem_end + 1].rstrip().rstrip(",")
    return cut + "]"


def parse_panel_descriptions(raw: str, expect: int) -> list[str]:
    """模型输出 → 分格描述数组（纯函数）。

    容错链：剥代码围栏 → json.loads → 失败走数组截断抢救再解析。
    只收非空字符串；数量以模型实际产出为准（少于请求格数=诚实返回
    实得数，不注水补齐）；超过期望格数截断。
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        # 剥 ```json / ``` 围栏（取首行围栏头与尾围栏之间的正文）
        lines = text.splitlines()
        body = [ln for ln in lines[1:] if not ln.strip().startswith("```")]
        text = "\n".join(body).strip()
    for candidate in (text, _salvage_json_array(text)):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, list):
            items = [str(x).strip() for x in data
                     if isinstance(x, str) and str(x).strip()]
            return items[:expect] if expect > 0 else items
    return []


def _persist_rows(project_id: str, descriptions: list[str],
                  replace: bool) -> list[dict]:
    """分格描述落库为分镜行（追加或清空重填），返回全量行。"""
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法写入分格")
    sb = _ensure_storyboard(db, project_id)
    sid = sb["id"]
    if replace:
        db.delete("storyboard_rows", "storyboard_id=?", (sid,))
    count_row = db.query_one(
        "SELECT COUNT(*) AS n FROM storyboard_rows WHERE storyboard_id=?",
        (sid,))
    existing = int(count_row["n"]) if count_row else 0
    for i, desc in enumerate(descriptions):
        row = _make_row(existing + i + 1, description=desc)
        db.insert("storyboard_rows",
                  _public_row_to_db(row, sid, sort_index=existing + i))
    db.update("storyboards", {"updated_at": _now()}, "id=?", (sid,))
    db.update("projects", {"updated_at": _now()}, "id=?", (project_id,))
    rows = db.query(
        "SELECT * FROM storyboard_rows WHERE storyboard_id=?"
        " ORDER BY sort_index ASC, shot_number ASC", (sid,))
    return [_row_to_storyboard_row(r) for r in rows]


@router.post("/manga/comic/script-generate")
async def comic_script_generate(
        body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """AI 写分格：故事梗概 → N 格画面描述 → 落库分镜行（本地引擎）。

    未加载必须全自动：ensure_loaded 自动装载（有界等待），失败才报错
    且带出路（与漫剧 ai-describe 同口径）。
    """
    try:
        req = ComicScriptRequest(**(body or {}))
    except ValidationError as exc:
        raise ApiError("PARAM_INVALID",
                       "参数不合法：" + "; ".join(
                           f"{'/'.join(str(loc) for loc in e['loc'])}: {e['msg']}"
                           for e in exc.errors()),
                       detail={"errors": exc.errors()}) from exc
    engine = get_dialog_engine()
    lock = await acquire_or_raise("dialog", task_id=None)
    try:
        # 本地路（2026-09-08 裁定）：未就绪即自动装载漫剧槽模型
        if not engine.is_ready:
            if not await run_blocking(engine.ensure_loaded, manga_dialog_model_id()):
                status = engine.get_status()
                raise ApiError(
                    "DIALOG_NOT_READY",
                    "对话模型未加载且自动加载失败，请稍后重试或在对话模块手动加载",
                    detail={"engine_state": status["state"],
                            "last_error": status["last_error"]})
        prompt = build_script_prompt(req.story, req.panels)
        try:
            raw = (await run_blocking(
                engine.chat, [{"role": "user", "content": prompt}],
                temperature=0.7,
                max_new_tokens=2048)).strip()
        except Exception as exc:  # noqa: BLE001 - 推理失败收敛为语义错误码
            raise ApiError("MODEL_INFERENCE_FAILED",
                           f"分格脚本推理失败：{exc}") from exc
        descriptions = parse_panel_descriptions(raw, req.panels)
        if not descriptions:
            raise ApiError(
                "MODEL_INFERENCE_FAILED",
                "模型未返回可用的分格描述（可能输出被截断或格式异常），请重试",
                detail={"raw_head": (raw or "")[:200]})
        rows = await run_blocking(_persist_rows, req.project_id,
                                  descriptions, req.replace)
        log.info("AI 写分格完成: project=%s 请求=%d 实得=%d replace=%s",
                    req.project_id, req.panels, len(descriptions), req.replace)
        return ok({"rows": rows, "requested": req.panels,
                   "generated": len(descriptions)})
    finally:
        await lock.release("dialog")
# 本项目仅供学习使用，商业授权请+Q 3559331368
