"""小说模块 API（批2 MVP，2026-09-05；/novel/* 前缀）。

薄端点层：CRUD 照 api/manga/comic.py 范式（get_db_safe → 手写校验 →
db.insert/update/delete → ok() 信封）；生成类端点只做校验 + 落库 +
入队即返回，实际执行在 novel_service.NovelJobQueue（单 worker 串行，
dialog 功能锁由 worker 统一编排）。
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Body, Query

from ..data.database import get_db_safe, parse_json
from ..middleware.error_handler import ApiError, ok
from ..services import novel_service as svc
from ..services.novel_service import (
    NovelJob,
    build_export_text,
    check_foreshadows,
    get_job_queue,
)
from ..services.offload import run_blocking

log = logging.getLogger("omnispace.api.novel")

router = APIRouter()


def _now() -> float:
    return time.time()


def _db():
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，无法操作小说项目")
    return db


def _require_project(db, project_id: str) -> dict:
    row = db.query_one("SELECT * FROM novel_projects WHERE id=?",
                       (project_id,))
    if row is None:
        raise ApiError("NOVEL_PROJECT_NOT_FOUND",
                       detail={"project_id": project_id})
    return row


def _project_row(r: dict) -> dict:
    return {
        "id": r["id"], "name": r.get("name", ""),
        "genre": r.get("genre", ""),
        "description": r.get("description", ""),
        "style_notes": r.get("style_notes", ""),
        "status": r.get("status", "active"),
        "meta": parse_json(r.get("meta"), {}),
        "created_at": r.get("created_at", 0),
        "updated_at": r.get("updated_at", 0),
    }


def _chapter_row(r: dict, with_content: bool = False) -> dict:
    out = {
        "id": r["id"], "project_id": r.get("project_id", ""),
        "outline_id": r.get("outline_id", ""),
        "chapter_index": r.get("chapter_index", 0),
        "title": r.get("title", ""),
        "summary": r.get("summary", ""),
        "word_count": r.get("word_count", 0),
        "status": r.get("status", "pending"),
        "progress": r.get("progress", 0.0),
        "error": r.get("error", ""),
        "updated_at": r.get("updated_at", 0),
    }
    if with_content:
        out["content"] = r.get("content", "")
    return out


# ══ 项目 CRUD ═════════════════════════════════════════════════

@router.post("/novel/project/create")
def novel_project_create(body: dict = Body(...)) -> dict[str, Any]:
    db = _db()
    name = str(body.get("name") or "").strip()
    if not name:
        raise ApiError("NOVEL_NAME_REQUIRED", "请填写作品名")
    if len(name) > 60:
        raise ApiError("NOVEL_NAME_TOO_LONG", "作品名最长 60 字")
    dup = db.query_one("SELECT id FROM novel_projects WHERE name=?",
                       (name,))
    if dup is not None:
        raise ApiError("NOVEL_PROJECT_NAME_DUPLICATED",
                       detail={"name": name})
    pid = uuid.uuid4().hex
    now = _now()
    db.insert("novel_projects", {
        "id": pid, "name": name,
        "genre": str(body.get("genre") or "").strip()[:40],
        "description": str(body.get("description") or "").strip()[:2000],
        "style_notes": str(body.get("style_notes") or "").strip()[:500],
        "created_at": now, "updated_at": now})
    return ok({"project_id": pid, "name": name})


@router.get("/novel/project/list")
def novel_project_list() -> dict[str, Any]:
    db = _db()
    rows = db.query(
        "SELECT * FROM novel_projects ORDER BY updated_at DESC")
    items = []
    for r in rows:
        item = _project_row(r)
        item["chapter_total"] = db.count(
            "novel_chapters", "project_id=?", (r["id"],))
        item["chapter_done"] = db.count(
            "novel_chapters", "project_id=? AND status='done'", (r["id"],))
        item["total_words"] = db.query_one(
            "SELECT COALESCE(SUM(word_count),0) AS w FROM novel_chapters "
            "WHERE project_id=? AND status='done'", (r["id"],))["w"]
        items.append(item)
    return ok({"items": items, "total": len(items)})


@router.get("/novel/project/{project_id}")
def novel_project_get(project_id: str) -> dict[str, Any]:
    db = _db()
    return ok(_project_row(_require_project(db, project_id)))


@router.put("/novel/project/{project_id}")
def novel_project_update(project_id: str,
                         body: dict = Body(...)) -> dict[str, Any]:
    db = _db()
    _require_project(db, project_id)
    data: dict[str, Any] = {"updated_at": _now()}
    for key, cap in (("name", 60), ("genre", 40),
                     ("description", 2000), ("style_notes", 500)):
        if key in body:
            val = str(body.get(key) or "").strip()
            if key == "name" and not val:
                raise ApiError("NOVEL_NAME_REQUIRED", "作品名不能为空")
            data[key] = val[:cap]
    db.update("novel_projects", data, "id=?", (project_id,))
    return ok({"project_id": project_id})


@router.delete("/novel/project/{project_id}")
def novel_project_delete(project_id: str) -> dict[str, Any]:
    db = _db()
    _require_project(db, project_id)
    for table in ("novel_outlines", "novel_chapters", "novel_characters",
                  "novel_foreshadows"):
        db.delete(table, "project_id=?", (project_id,))
    db.delete("novel_projects", "id=?", (project_id,))
    return ok({"project_id": project_id, "deleted": True})


# ══ 大纲树 ═════════════════════════════════════════════════════

@router.post("/novel/outline/generate")
async def novel_outline_generate(body: dict = Body(...)) -> dict[str, Any]:
    """灵感 → 分层大纲树（卷/章细纲 + 待写章节行）。入队即返回。"""
    db = _db()
    project = _require_project(db, str(body.get("project_id") or ""))
    q = get_job_queue()
    task_id = f"novel-outline-{project['id']}-{uuid.uuid4().hex[:6]}"
    # 大纲重建属项目级：同项目已有大纲任务在队则拒绝（防重复连点）
    snap = q.snapshot()
    dup = [t for t in (snap["queue"] + ([snap["current"]] if snap["current"] else []))
           if t["kind"] == "outline" and t["project_id"] == project["id"]]
    if dup:
        raise ApiError("NOVEL_TASK_DUPLICATED", "该项目大纲已在生成队列中")
    pos = await q.submit(NovelJob(
        task_id=task_id, kind="outline", project_id=project["id"],
        run=svc.run_outline_job))
    return ok({"task_id": task_id, "position": pos})


@router.get("/novel/outline/{project_id}")
def novel_outline_list(project_id: str) -> dict[str, Any]:
    db = _db()
    _require_project(db, project_id)
    rows = db.query(
        "SELECT * FROM novel_outlines WHERE project_id=? "
        "ORDER BY sort_index, created_at", (project_id,))
    items = [{"id": r["id"], "parent_id": r.get("parent_id", ""),
              "level": r.get("level", ""), "sort_index": r.get("sort_index", 0),
              "title": r.get("title", ""), "content": r.get("content", ""),
              "status": r.get("status", "draft")} for r in rows]
    return ok({"project_id": project_id, "items": items,
               "total": len(items)})


@router.put("/novel/outline/{outline_id}")
def novel_outline_update(outline_id: str,
                         body: dict = Body(...)) -> dict[str, Any]:
    db = _db()
    row = db.query_one("SELECT * FROM novel_outlines WHERE id=?",
                       (outline_id,))
    if row is None:
        raise ApiError("NOVEL_OUTLINE_NOT_FOUND", detail={"id": outline_id})
    data: dict[str, Any] = {"updated_at": _now()}
    if "title" in body:
        data["title"] = str(body.get("title") or "").strip()[:120]
    if "content" in body:
        data["content"] = str(body.get("content") or "").strip()[:8000]
    if "status" in body and body.get("status") in ("draft", "confirmed"):
        data["status"] = body["status"]
    db.update("novel_outlines", data, "id=?", (outline_id,))
    return ok({"id": outline_id})


@router.delete("/novel/outline/{outline_id}")
def novel_outline_delete(outline_id: str) -> dict[str, Any]:
    db = _db()
    n = db.delete("novel_outlines", "id=?", (outline_id,))
    if not n:
        raise ApiError("NOVEL_OUTLINE_NOT_FOUND", detail={"id": outline_id})
    return ok({"id": outline_id, "deleted": True})


# ══ 章节 ═══════════════════════════════════════════════════════

@router.get("/novel/chapters/{project_id}")
def novel_chapter_list(project_id: str) -> dict[str, Any]:
    db = _db()
    _require_project(db, project_id)
    rows = db.query(
        "SELECT * FROM novel_chapters WHERE project_id=? "
        "ORDER BY chapter_index, created_at", (project_id,))
    items = [_chapter_row(r) for r in rows]
    return ok({"project_id": project_id, "items": items,
               "total": len(items)})


@router.get("/novel/chapter/{chapter_id}")
def novel_chapter_get(chapter_id: str) -> dict[str, Any]:
    db = _db()
    row = db.query_one("SELECT * FROM novel_chapters WHERE id=?",
                       (chapter_id,))
    if row is None:
        raise ApiError("NOVEL_CHAPTER_NOT_FOUND", detail={"id": chapter_id})
    return ok(_chapter_row(row, with_content=True))


@router.post("/novel/chapter/create")
def novel_chapter_create(body: dict = Body(...)) -> dict[str, Any]:
    """手工建章（可带细纲，落 outline 节点 + 章行）。"""
    db = _db()
    project = _require_project(db, str(body.get("project_id") or ""))
    title = str(body.get("title") or "").strip()[:120] or "未命名章"
    outline_text = str(body.get("outline") or "").strip()[:8000]
    nxt = db.query_one(
        "SELECT COALESCE(MAX(chapter_index),0)+1 AS n FROM novel_chapters "
        "WHERE project_id=?", (project["id"],))
    now = _now()
    outline_id = ""
    if outline_text:
        outline_id = uuid.uuid4().hex
        db.insert("novel_outlines", {
            "id": outline_id, "project_id": project["id"],
            "parent_id": "", "level": "chapter_outline",
            "sort_index": nxt["n"], "title": title,
            "content": outline_text, "created_at": now, "updated_at": now})
    cid = uuid.uuid4().hex
    db.insert("novel_chapters", {
        "id": cid, "project_id": project["id"], "outline_id": outline_id,
        "chapter_index": nxt["n"], "title": title,
        "content": str(body.get("content") or ""),
        "created_at": now, "updated_at": now})
    return ok({"chapter_id": cid, "chapter_index": nxt["n"]})


@router.put("/novel/chapter/{chapter_id}")
def novel_chapter_update(chapter_id: str,
                         body: dict = Body(...)) -> dict[str, Any]:
    """保存人工编辑（生成中的章节拒绝覆盖——防手滑洗掉在跑任务）。"""
    db = _db()
    row = db.query_one("SELECT * FROM novel_chapters WHERE id=?",
                       (chapter_id,))
    if row is None:
        raise ApiError("NOVEL_CHAPTER_NOT_FOUND", detail={"id": chapter_id})
    if row.get("status") == "generating":
        raise ApiError("NOVEL_CHAPTER_BUSY",
                       "本章正在生成中，请等生成结束或先取消再编辑")
    data: dict[str, Any] = {"updated_at": _now()}
    if "title" in body:
        data["title"] = str(body.get("title") or "").strip()[:120]
    if "content" in body:
        content = str(body.get("content") or "")
        data["content"] = content
        data["word_count"] = len(content.strip())
        # 人工改过正文：状态视为 done（不再标 pending）
        if row.get("status") != "done":
            data["status"] = "done"
    if "summary" in body:
        data["summary"] = str(body.get("summary") or "").strip()[:300]
    db.update("novel_chapters", data, "id=?", (chapter_id,))
    return ok({"id": chapter_id})


@router.delete("/novel/chapter/{chapter_id}")
def novel_chapter_delete(chapter_id: str) -> dict[str, Any]:
    db = _db()
    n = db.delete("novel_chapters", "id=?", (chapter_id,))
    if not n:
        raise ApiError("NOVEL_CHAPTER_NOT_FOUND", detail={"id": chapter_id})
    return ok({"id": chapter_id, "deleted": True})


# ══ 生成编排（入队/进度/取消）═════════════════════════════════

async def _enqueue_chapter(db, project_id: str, chapter_id: str) -> str:
    task_id = f"novel-ch-{chapter_id}-{uuid.uuid4().hex[:6]}"
    db.update("novel_chapters",
              {"status": "pending", "progress": 0.0, "error": ""},
              "id=?", (chapter_id,))
    await get_job_queue().submit(NovelJob(
        task_id=task_id, kind="chapter", project_id=project_id,
        chapter_id=chapter_id, run=svc.run_chapter_job))
    return task_id


@router.post("/novel/chapter/{chapter_id}/generate")
async def novel_chapter_generate(chapter_id: str) -> dict[str, Any]:
    """单章生成：入队即返回（排队位次 + task_id）。"""
    db = _db()
    ch = db.query_one("SELECT * FROM novel_chapters WHERE id=?",
                      (chapter_id,))
    if ch is None:
        raise ApiError("NOVEL_CHAPTER_NOT_FOUND", detail={"id": chapter_id})
    if ch.get("status") == "generating":
        raise ApiError("NOVEL_CHAPTER_BUSY", "本章正在生成中")
    task_id = await _enqueue_chapter(db, ch["project_id"], chapter_id)
    pos = get_job_queue().position(task_id)
    return ok({"task_id": task_id, "position": pos})


@router.post("/novel/chapters/generate")
async def novel_chapters_generate_batch(body: dict = Body(...)) -> dict[str, Any]:
    """批量入队（按章序串行生成；单 worker 保证不并发）。

    skip_completed=True 时跳过已完成（done）章节，只生成未完成的
    （用户迭代项 2026-09-07：之前 done 章也会被重做）；默认 False 保持
    「全部重跑」兼容语义。
    """
    db = _db()
    project = _require_project(db, str(body.get("project_id") or ""))
    skip_completed = bool(body.get("skip_completed", False))
    rows = db.query(
        "SELECT id, status FROM novel_chapters WHERE project_id=? "
        "ORDER BY chapter_index", (project["id"],))
    if not rows:
        raise ApiError("NOVEL_NO_CHAPTERS", "项目还没有章节，先生成大纲")
    tasks: list[dict] = []
    skipped = 0
    for r in rows:
        if r.get("status") == "generating":
            continue
        if skip_completed and r.get("status") == "done":
            skipped += 1
            continue
        task_id = await _enqueue_chapter(db, project["id"], r["id"])
        tasks.append({"task_id": task_id, "chapter_id": r["id"]})
    return ok({"tasks": tasks, "queued": len(tasks), "skipped_completed": skipped})


@router.get("/novel/generate/progress")
async def novel_generate_progress(
        project_id: str = Query("")) -> dict[str, Any]:
    """进度轮询：队列快照 + 任务阶段 + 在跑章节的 DB 状态。"""
    q = get_job_queue()
    snap = q.snapshot()
    items: list[dict] = []
    for i, t in enumerate(snap["queue"]):
        items.append({**t, "position": i + 1,
                      "stage": svc.job_progress(t["task_id"])})
    current = None
    if snap["current"]:
        current = {**snap["current"], "position": 0,
                   "stage": svc.job_progress(snap["current"]["task_id"])}
    chapter_states: list[dict] = []
    if project_id:
        db = _db()
        rows = db.query(
            "SELECT id, status, progress, error FROM novel_chapters "
            "WHERE project_id=? AND status IN ('generating','error') "
            "ORDER BY chapter_index", (project_id,))
        chapter_states = rows
    return ok({"current": current, "queue": items,
               "chapter_states": chapter_states,
               "failures": svc.job_failures(),
               "active": bool(current or items)})


@router.post("/novel/generate/cancel")
async def novel_generate_cancel(body: dict = Body(...)) -> dict[str, Any]:
    task_id = str(body.get("task_id") or "")
    if not task_id:
        raise ApiError("NOVEL_TASK_ID_REQUIRED", "缺少 task_id")
    result = get_job_queue().request_cancel(task_id)
    return ok({"task_id": task_id, "result": result})


# ══ 角色卡 ═════════════════════════════════════════════════════

@router.get("/novel/characters/{project_id}")
def novel_character_list(project_id: str) -> dict[str, Any]:
    db = _db()
    _require_project(db, project_id)
    rows = db.query(
        "SELECT * FROM novel_characters WHERE project_id=? "
        "ORDER BY created_at", (project_id,))
    return ok({"project_id": project_id, "items": rows,
               "total": len(rows)})


@router.post("/novel/characters/create")
def novel_character_create(body: dict = Body(...)) -> dict[str, Any]:
    db = _db()
    project = _require_project(db, str(body.get("project_id") or ""))
    name = str(body.get("name") or "").strip()[:60]
    if not name:
        raise ApiError("NOVEL_CHAR_NAME_REQUIRED", "角色名不能为空")
    cid = uuid.uuid4().hex
    now = _now()
    db.insert("novel_characters", {
        "id": cid, "project_id": project["id"], "name": name,
        "role": str(body.get("role") or "").strip()[:20],
        "summary": str(body.get("summary") or "").strip()[:1000],
        "created_at": now, "updated_at": now})
    return ok({"character_id": cid})


@router.put("/novel/characters/{character_id}")
def novel_character_update(character_id: str,
                           body: dict = Body(...)) -> dict[str, Any]:
    db = _db()
    data: dict[str, Any] = {"updated_at": _now()}
    for key, cap in (("name", 60), ("role", 20), ("summary", 1000)):
        if key in body:
            data[key] = str(body.get(key) or "").strip()[:cap]
    n = db.update("novel_characters", data, "id=?", (character_id,))
    if not n:
        raise ApiError("NOVEL_CHARACTER_NOT_FOUND",
                       detail={"id": character_id})
    return ok({"id": character_id})


@router.delete("/novel/characters/{character_id}")
def novel_character_delete(character_id: str) -> dict[str, Any]:
    db = _db()
    n = db.delete("novel_characters", "id=?", (character_id,))
    if not n:
        raise ApiError("NOVEL_CHARACTER_NOT_FOUND",
                       detail={"id": character_id})
    return ok({"id": character_id, "deleted": True})


@router.post("/novel/characters/generate")
async def novel_characters_generate(
        body: dict = Body(...)) -> dict[str, Any]:
    """AI 批量生成角色卡（入队即返回）。basis=补充要求（复用 job.chapter_id 字段）。"""
    db = _db()
    project = _require_project(db, str(body.get("project_id") or ""))
    q = get_job_queue()
    task_id = f"novel-chars-{project['id']}-{uuid.uuid4().hex[:6]}"
    pos = await q.submit(NovelJob(
        task_id=task_id, kind="characters", project_id=project["id"],
        chapter_id=str(body.get("basis") or "").strip()[:300],
        run=svc.run_characters_job))
    return ok({"task_id": task_id, "position": pos})


# ══ 伏笔账本 ═══════════════════════════════════════════════════

@router.get("/novel/foreshadows/{project_id}")
def novel_foreshadow_list(project_id: str) -> dict[str, Any]:
    db = _db()
    _require_project(db, project_id)
    rows = db.query(
        "SELECT * FROM novel_foreshadows WHERE project_id=? "
        "ORDER BY created_at", (project_id,))
    chapters = db.query(
        "SELECT id, chapter_index, title FROM novel_chapters "
        "WHERE project_id=?", (project_id,))
    return ok({"project_id": project_id, "items": rows,
               "check": check_foreshadows(rows, chapters),
               "total": len(rows)})


@router.post("/novel/foreshadows/create")
def novel_foreshadow_create(body: dict = Body(...)) -> dict[str, Any]:
    db = _db()
    project = _require_project(db, str(body.get("project_id") or ""))
    desc = str(body.get("description") or "").strip()
    if not desc:
        raise ApiError("NOVEL_FORESHADOW_DESC_REQUIRED", "请填写伏笔内容")
    fid = uuid.uuid4().hex
    now = _now()
    db.insert("novel_foreshadows", {
        "id": fid, "project_id": project["id"], "description": desc[:1000],
        "planted_chapter_id": str(body.get("planted_chapter_id") or ""),
        "note": str(body.get("note") or "").strip()[:500],
        "created_at": now, "updated_at": now})
    return ok({"foreshadow_id": fid})


@router.put("/novel/foreshadows/{foreshadow_id}")
def novel_foreshadow_update(foreshadow_id: str,
                            body: dict = Body(...)) -> dict[str, Any]:
    db = _db()
    data: dict[str, Any] = {"updated_at": _now()}
    if "description" in body:
        data["description"] = str(body.get("description") or "").strip()[:1000]
    if "note" in body:
        data["note"] = str(body.get("note") or "").strip()[:500]
    if "status" in body and body.get("status") in (
            "planted", "payoff", "dropped"):
        data["status"] = body["status"]
    if "planted_chapter_id" in body:
        data["planted_chapter_id"] = str(body.get("planted_chapter_id") or "")
    if "payoff_chapter_id" in body:
        data["payoff_chapter_id"] = str(body.get("payoff_chapter_id") or "")
    n = db.update("novel_foreshadows", data, "id=?", (foreshadow_id,))
    if not n:
        raise ApiError("NOVEL_FORESHADOW_NOT_FOUND",
                       detail={"id": foreshadow_id})
    return ok({"id": foreshadow_id})


@router.delete("/novel/foreshadows/{foreshadow_id}")
def novel_foreshadow_delete(foreshadow_id: str) -> dict[str, Any]:
    db = _db()
    n = db.delete("novel_foreshadows", "id=?", (foreshadow_id,))
    if not n:
        raise ApiError("NOVEL_FORESHADOW_NOT_FOUND",
                       detail={"id": foreshadow_id})
    return ok({"id": foreshadow_id, "deleted": True})


# ══ 世界观入库 / 导出 ══════════════════════════════════════════

@router.post("/novel/worldbuilding/import")
async def novel_worldbuilding_import(
        body: dict = Body(...)) -> dict[str, Any]:
    """世界观素材入知识库（走质检闸 + 向量化，经 run_blocking）。"""
    db = _db()
    project = _require_project(db, str(body.get("project_id") or ""))
    content = str(body.get("content") or "").strip()
    if len(content) < 20:
        raise ApiError("NOVEL_WORLDBUILD_TOO_SHORT",
                       "世界观素材太短（至少 20 字）")
    topic = str(body.get("topic") or "").strip() or f"小说设定-{project['name']}"
    added = await run_blocking(
        svc.import_worldbuilding_sync, content, topic[:60])
    return ok({"added": added, "topic": topic[:60]})


@router.post("/novel/export")
def novel_export(body: dict = Body(...)) -> dict[str, Any]:
    db = _db()
    project = _require_project(db, str(body.get("project_id") or ""))
    fmt = str(body.get("fmt") or "txt").lower()
    if fmt not in ("txt", "md"):
        raise ApiError("NOVEL_EXPORT_FMT_INVALID", "fmt 只支持 txt/md")
    chapters = db.query(
        "SELECT chapter_index, title, content FROM novel_chapters "
        "WHERE project_id=? ORDER BY chapter_index", (project["id"],))
    content = build_export_text(project, chapters, fmt)
    safe_name = (project.get("name") or "novel").replace(
        "/", "_").replace("\\", "_")[:40]
    return ok({"filename": f"{safe_name}.{fmt}", "content": content,
               "chapter_total": len(chapters)})
