"""学习主题/会话/设置 API 路由（TASK-036 所需 API + TASK-014/040/042，v2.3）。

端点清单（由 main.py 以 /v1 前缀挂载）：
- POST   /learn/topic/create      创建学习主题
- GET    /learn/topic/list        主题列表
- DELETE /learn/topic/delete      删除主题（body: {id}）
- PUT    /learn/topic/{id}        更新主题
- POST   /learn/session/start     启动学习会话（后台线程跑 Agent 循环）
- POST   /learn/session/pause     暂停
- POST   /learn/session/resume    恢复
- POST   /learn/session/stop      停止
- GET    /learn/session/status    会话状态
- WS     /learn/session/progress  会话进度实时推送（2s 快照，文档B §7.1.2）
- GET    /learn/session/logs      操作日志
- GET    /learn/session/report    学习完成报告
- GET    /learn/settings          读取学习设置
- PUT    /learn/settings          更新学习设置（持久化 learning_settings 表）
- GET    /learn/quota             当前资源配额快照（TASK-014）

约定：router 不带 prefix；成功 ok(data)；错误抛 ApiError；
学习域错误码 6xxxx 段（61001-61008）；
数据表 learning_topics/learning_sessions/learning_logs/learning_settings
由 browser_agent_service.ensure_learning_tables() 自建（CREATE TABLE IF NOT EXISTS）；
数据库不可用时降级到内存存储（规格 §4.1 容错降级）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, Query, WebSocket, WebSocketDisconnect

from ..data.database import Database, get_db_safe, parse_json
from ..middleware.error_handler import ApiError, ok
from ..services.browser_agent_service import (
    ERR_QUOTA_DENIED,
    ERR_SESSION_RUNNING,
    ERR_TOPIC_LIMIT,
    ERR_TOPIC_NOT_FOUND,
    LearningError,
    LearningSession,
    ensure_learning_tables,
    get_browser_agent_service,
    get_learning_settings,
    get_traffic_today,
    update_learning_settings,
)
from ..services.learning_scheduler import get_learning_scheduler

router = APIRouter()
log = logging.getLogger("omnispace.api.learning")

MAX_TOPICS = 50          # 主题数量上限
DEFAULT_TOPIC_KEYWORDS = 4

# ── 内存降级存储（数据库不可用时）────────────────────────────────────
_mem_topics: dict[str, dict] = {}


def _now() -> float:
    return time.time()


def _db() -> Database | None:
    return get_db_safe()


def _agent_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """调用 Agent 服务并把 LearningError 转为 ApiError。"""
    try:
        return fn(*args, **kwargs)
    except LearningError as exc:
        raise ApiError(exc.code, exc.message, detail=exc.detail) from exc


# ═══════════════════════════════════════════════════════════════════
#  主题管理
# ═══════════════════════════════════════════════════════════════════

# LEARN-004/067：学习深度 → 页数预算映射（shallow/standard/deep）
DEPTH_MAX_PAGES = {"shallow": 8, "standard": 20, "deep": 50}


def _valid_seed_urls(raw: Any) -> list[str]:
    """seed_urls 校验（LEARN-005）：仅接受 http/https URL，上限 20 条。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ApiError("SYSTEM_PARAM_INVALID", "seed_urls 必须是数组")
    urls: list[str] = []
    for u in raw[:20]:
        s = str(u or "").strip()
        if not s:
            continue
        if not s.startswith(("http://", "https://")):
            raise ApiError("SYSTEM_PARAM_INVALID", f"seed_urls 仅支持 http/https 链接: {s[:60]}")
        urls.append(s)
    return urls


def _topic_row_to_dict(r: dict) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "keywords": parse_json(r.get("keywords"), []),
        "status": r.get("status", "active"),
        "progress": float(r.get("progress", 0.0) or 0.0),
        "knowledge_count": int(r.get("knowledge_count", 0) or 0),
        "source": r.get("source", "manual"),
        "depth": r.get("depth", "standard") or "standard",
        "seed_urls": parse_json(r.get("seed_urls"), []),
        "max_pages": int(r.get("max_pages", 20) or 20),
        "created_at": r.get("created_at", 0),
        "updated_at": r.get("updated_at", 0),
    }


@router.post("/learn/topic/create")
def topic_create(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """创建学习主题：{name, keywords?, source?, depth?, seed_urls?}。

    LEARN-003：同名主题拒绝（LEARN_TOPIC_NAME_DUPLICATED）。
    LEARN-004/067：depth=shallow/standard/deep → max_pages 8/20/50。
    LEARN-005：seed_urls 作为会话起始页面（启动时注入 budget）。
    """
    name = str((body or {}).get("name", "") or "").strip()
    if not name:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少必填参数: name")
    keywords = (body or {}).get("keywords") or []
    if not isinstance(keywords, list):
        raise ApiError("UNSUPPORTED_FORMAT", "keywords 必须是数组")
    keywords = [str(k).strip() for k in keywords if str(k).strip()][
        :DEFAULT_TOPIC_KEYWORDS * 3]
    source = str((body or {}).get("source", "manual") or "manual")
    depth = str((body or {}).get("depth", "standard") or "standard").lower()
    if depth not in DEPTH_MAX_PAGES:
        raise ApiError("SYSTEM_PARAM_INVALID",
                       f"depth 仅支持 {sorted(DEPTH_MAX_PAGES)}")
    seed_urls = _valid_seed_urls((body or {}).get("seed_urls"))
    max_pages = DEPTH_MAX_PAGES[depth]
    topic_id = uuid.uuid4().hex
    now = _now()

    db = _db()
    if db is not None:
        try:
            ensure_learning_tables()
            if db.count("learning_topics") >= MAX_TOPICS:
                raise ApiError(ERR_TOPIC_LIMIT,
                               f"学习主题数量已达上限（{MAX_TOPICS}个）")
            if db.query_one(
                    "SELECT id FROM learning_topics WHERE name=?",
                    (name,)) is not None:
                raise ApiError("LEARN_TOPIC_NAME_DUPLICATED",
                               f"已存在同名学习主题: {name}",
                               detail={"name": name},
                               suggestion="换一个主题名称，或对既有主题使用克隆/编辑")
            db.insert("learning_topics", {
                "id": topic_id, "name": name, "keywords": keywords,
                "status": "active", "progress": 0.0, "knowledge_count": 0,
                "source": source, "depth": depth, "seed_urls": seed_urls,
                "max_pages": max_pages,
                "created_at": now, "updated_at": now,
            })
            row = db.query_one(
                "SELECT id, name, keywords, status, progress,"
                " knowledge_count, source, depth, seed_urls, max_pages,"
                " created_at, updated_at"
                " FROM learning_topics WHERE id=?",
                (topic_id,))
            return ok(_topic_row_to_dict(row), message="学习主题已创建")
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("主题落库失败，降级内存存储: %s", exc, exc_info=True)

    if len(_mem_topics) >= MAX_TOPICS:
        raise ApiError(ERR_TOPIC_LIMIT,
                       f"学习主题数量已达上限（{MAX_TOPICS}个）")
    if any(t.get("name") == name for t in _mem_topics.values()):
        raise ApiError("LEARN_TOPIC_NAME_DUPLICATED",
                       f"已存在同名学习主题: {name}",
                       detail={"name": name})
    topic = {"id": topic_id, "name": name, "keywords": keywords,
             "status": "active", "progress": 0.0, "knowledge_count": 0,
             "source": source, "depth": depth, "seed_urls": seed_urls,
             "max_pages": max_pages, "created_at": now, "updated_at": now}
    _mem_topics[topic_id] = topic
    return ok(topic, message="学习主题已创建（内存模式）")


@router.get("/learn/topic/list")
def topic_list(keyword: str = Query(default=""),
               status: str = Query(default=""),
               sort: str = Query(default="created_desc")) -> dict[str, Any]:
    """主题列表（LEARN-006）：keyword 模糊匹配名称/关键词、status 过滤、
    sort=created_desc|created_asc|name|progress，附各主题最新会话进度。"""
    agent = get_browser_agent_service()
    active = agent.active_session()
    db = _db()
    if db is not None:
        try:
            ensure_learning_tables()
            rows = db.query(
                "SELECT id, name, keywords, status, progress,"
                " knowledge_count, source, depth, seed_urls, max_pages,"
                " created_at, updated_at"
                " FROM learning_topics ORDER BY created_at DESC")
            items = [_topic_row_to_dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            log.warning("主题查询失败，降级内存存储: %s", exc, exc_info=True)
            items = sorted(_mem_topics.values(),
                           key=lambda t: t.get("created_at", 0), reverse=True)
    else:
        items = sorted(_mem_topics.values(),
                       key=lambda t: t.get("created_at", 0), reverse=True)
    # LEARN-006：筛选与排序
    if keyword:
        needle = keyword.strip().lower()
        items = [t for t in items
                 if needle in str(t.get("name", "")).lower()
                 or any(needle in str(k).lower()
                        for k in t.get("keywords", []))]
    if status:
        items = [t for t in items if t.get("status") == status]
    if sort == "created_asc":
        items.sort(key=lambda t: t.get("created_at", 0))
    elif sort == "name":
        items.sort(key=lambda t: str(t.get("name", "")))
    elif sort == "progress":
        items.sort(key=lambda t: float(t.get("progress", 0.0)), reverse=True)
    else:  # created_desc 默认
        items.sort(key=lambda t: t.get("created_at", 0), reverse=True)
    if active is not None:
        for item in items:
            if item["id"] == active.topic_id:
                item["progress"] = round(active.coverage, 3)
                item["active_session_id"] = active.session_id
    return ok({"items": items, "total": len(items)})


@router.delete("/learn/topic/delete")
def topic_delete(body: dict = Body(default_factory=dict),
                 id: str = Query(default="")) -> dict[str, Any]:
    """删除主题（body.id / body.topic_id / ?id= 均可）。"""
    topic_id = str((body or {}).get("id", "") or (body or {}).get("topic_id", "")
                   or id or "").strip()
    if not topic_id:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少必填参数: id")
    db = _db()
    deleted = 0
    if db is not None:
        try:
            ensure_learning_tables()
            deleted = db.delete("learning_topics", "id=?", (topic_id,))
        except Exception as exc:  # noqa: BLE001
            log.warning("主题删除失败: %s", exc, exc_info=True)
    if _mem_topics.pop(topic_id, None) is not None:
        deleted += 1
    if deleted == 0:
        raise ApiError(ERR_TOPIC_NOT_FOUND, "学习主题不存在",
                       detail={"id": topic_id})
    return ok({"id": topic_id, "deleted": True}, message="主题已删除")


@router.post("/learn/topic/clone")
def topic_clone(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """克隆学习主题（LEARN-069）：复制名称/关键词/深度/种子URL设置。

    请求体: {"topic_id": str, "name"?: str（缺省=原名-副本）,
             "include_knowledge"?: bool（预留，当前仅复制设置）}
    新主题进度归零；名称冲突自动追加序号。
    """
    topic_id = str((body or {}).get("topic_id", "") or "").strip()
    if not topic_id:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少必填参数: topic_id")
    db = _db()
    src: dict | None = None
    if db is not None:
        try:
            ensure_learning_tables()
            row = db.query_one(
                "SELECT id, name, keywords, status, progress,"
                " knowledge_count, source, depth, seed_urls, max_pages,"
                " created_at, updated_at FROM learning_topics WHERE id=?",
                (topic_id,))
            if row is not None:
                src = _topic_row_to_dict(row)
        except Exception as exc:  # noqa: BLE001
            log.warning("主题查询失败: %s", exc, exc_info=True)
    if src is None and topic_id in _mem_topics:
        src = dict(_mem_topics[topic_id])
    if src is None:
        raise ApiError(ERR_TOPIC_NOT_FOUND, "学习主题不存在",
                       detail={"topic_id": topic_id})

    base_name = str((body or {}).get("name", "") or "").strip() \
        or f"{src['name']}-副本"
    # 名称冲突自动追加序号（clone 语义是允许复制的，不用 003 拒绝逻辑）
    existing_names: set[str] = set()
    if db is not None:
        try:
            existing_names = {r["name"] for r in db.query(
                "SELECT name FROM learning_topics")}
        except Exception:  # noqa: BLE001
            log.debug("topic_clone: 降级忽略", exc_info=True)
    existing_names |= {t.get("name", "") for t in _mem_topics.values()}
    name = base_name
    seq = 2
    while name in existing_names:
        name = f"{base_name}{seq}"
        seq += 1

    new_id = uuid.uuid4().hex
    now = _now()
    record = {
        "id": new_id, "name": name,
        "keywords": list(src.get("keywords", [])),
        "status": "active", "progress": 0.0, "knowledge_count": 0,
        "source": "clone",
        "depth": src.get("depth", "standard"),
        "seed_urls": list(src.get("seed_urls", [])),
        "max_pages": int(src.get("max_pages", 20) or 20),
        "created_at": now, "updated_at": now,
    }
    if db is not None:
        try:
            if db.count("learning_topics") >= MAX_TOPICS:
                raise ApiError(ERR_TOPIC_LIMIT,
                               f"学习主题数量已达上限（{MAX_TOPICS}个）")
            db.insert("learning_topics", record)
            return ok(record, message=f"已克隆为新主题: {name}")
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("克隆落库失败，降级内存存储: %s", exc, exc_info=True)
    if len(_mem_topics) >= MAX_TOPICS:
        raise ApiError(ERR_TOPIC_LIMIT,
                       f"学习主题数量已达上限（{MAX_TOPICS}个）")
    _mem_topics[new_id] = record
    return ok(record, message=f"已克隆为新主题（内存模式）: {name}")


@router.put("/learn/topic/{topic_id}")
def topic_update(topic_id: str, body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """更新主题：{name?, keywords?, status?}。"""
    patch: dict = {}
    if "name" in (body or {}):
        name = str(body["name"] or "").strip()
        if not name:
            raise ApiError("SYSTEM_PARAM_INVALID", "name 不能为空")
        patch["name"] = name
    if "keywords" in (body or {}):
        kws = body["keywords"]
        if not isinstance(kws, list):
            raise ApiError("UNSUPPORTED_FORMAT", "keywords 必须是数组")
        patch["keywords"] = [str(k).strip() for k in kws if str(k).strip()]
    if "status" in (body or {}):
        status = str(body["status"] or "").strip()
        if status not in ("active", "archived", "paused"):
            raise ApiError("UNSUPPORTED_FORMAT", "status 仅支持 active/archived/paused")
        patch["status"] = status
    if not patch:
        raise ApiError("SYSTEM_PARAM_INVALID", "没有可更新的字段")

    db = _db()
    if db is not None:
        try:
            ensure_learning_tables()
            patch["updated_at"] = _now()
            affected = db.update("learning_topics", patch, "id=?",
                                 (topic_id,))
            if affected == 0:
                raise ApiError(ERR_TOPIC_NOT_FOUND, "学习主题不存在",
                               detail={"id": topic_id})
            row = db.query_one(
                "SELECT id, name, keywords, status, progress,"
                " knowledge_count, source, created_at, updated_at"
                " FROM learning_topics WHERE id=?",
                (topic_id,))
            return ok(_topic_row_to_dict(row), message="主题已更新")
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("主题更新失败，降级内存存储: %s", exc, exc_info=True)

    topic = _mem_topics.get(topic_id)
    if topic is None:
        raise ApiError(ERR_TOPIC_NOT_FOUND, "学习主题不存在",
                       detail={"id": topic_id})
    topic.update(patch)
    topic["updated_at"] = _now()
    return ok(topic, message="主题已更新（内存模式）")


# ═══════════════════════════════════════════════════════════════════
#  学习会话
# ═══════════════════════════════════════════════════════════════════

def _resolve_session_id(session_id: str = "", body: dict | None = None) -> str:
    """session_id 解析：显式 > body > 当前活跃会话。"""
    sid = (session_id or "").strip()
    if not sid and body:
        sid = str(body.get("session_id", "") or "").strip()
    if not sid:
        active = get_browser_agent_service().active_session()
        if active is not None:
            sid = active.session_id
    if not sid:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少 session_id，且当前没有活跃学习会话")
    return sid


def _persist_session_row(session: LearningSession) -> None:
    """会话状态落库（best effort）。"""
    db = _db()
    if db is None:
        return
    try:
        ensure_learning_tables()
        now = _now()
        db.sql(
            "INSERT INTO learning_sessions (id, topic_id, goal, budget, "
            "status, pages_visited, knowledge_extracted, coverage, "
            "stop_reason, started_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "pages_visited=excluded.pages_visited, "
            "knowledge_extracted=excluded.knowledge_extracted, "
            "coverage=excluded.coverage, "
            "stop_reason=excluded.stop_reason, updated_at=excluded.updated_at",
            (session.session_id, session.topic_id, session.goal,
             json.dumps(session.budget.to_dict(), ensure_ascii=False),
             session.status,
             session.pages_visited, session.knowledge_extracted,
             session.coverage, session.stop_reason, session.started_at,
             session.created_at, now))
    except Exception as exc:  # noqa: BLE001
        log.debug("会话落库失败（忽略）: %s", exc)


@router.post("/learn/session/start")
def session_start(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """启动学习会话：{topic_id, budget?, enqueue?}。后台线程运行 Agent 循环。

    - 校验主题存在（61001）
    - 校验配额（创作活跃/断网/AI推理 → 61005）
    - 同时只允许一个活跃会话（61008）；enqueue=true 时改入等待队列
      （LEARN-056，当前会话结束后由调度器自动启动队首）
    - LEARN-005/004：主题 seed_urls/max_pages 注入会话预算
    """
    body = body or {}
    topic_id = str(body.get("topic_id", "") or "").strip()
    if not topic_id:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少必填参数: topic_id")

    # 主题存在性与目标文本（附 depth/seed_urls/max_pages）
    goal = ""
    topic_seeds: list[str] = []
    topic_max_pages = 0
    db = _db()
    if db is not None:
        try:
            ensure_learning_tables()
            row = db.query_one(
                "SELECT name, keywords, seed_urls, max_pages"
                " FROM learning_topics WHERE id=?",
                (topic_id,))
            if row is not None:
                goal = row["name"]
                topic_seeds = parse_json(row.get("seed_urls"), [])
                topic_max_pages = int(row.get("max_pages", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("主题查询失败: %s", exc, exc_info=True)
    if not goal and topic_id in _mem_topics:
        goal = _mem_topics[topic_id]["name"]
        topic_seeds = list(_mem_topics[topic_id].get("seed_urls", []))
        topic_max_pages = int(_mem_topics[topic_id].get("max_pages", 0) or 0)
    if not goal:
        raise ApiError(ERR_TOPIC_NOT_FOUND, "学习主题不存在",
                       detail={"topic_id": topic_id})

    settings = get_learning_settings()
    if not settings.get("enabled", True):
        raise ApiError(ERR_QUOTA_DENIED, "联网学习开关已关闭，请在设置中开启")

    scheduler = get_learning_scheduler()
    pause, reason = scheduler.should_pause_learning()
    if pause:
        raise ApiError(ERR_QUOTA_DENIED,
                       f"当前资源状态不允许联网学习（{reason}）",
                       detail={"reason": reason},
                       suggestion="结束创作/恢复联网后再试")

    agent = get_browser_agent_service()
    budget = body.get("budget") or {}
    budget.setdefault("max_time_minutes",
                      int(settings.get("max_time_minutes", 30) or 30))
    # LEARN-004：主题深度决定的页数预算优先于全局默认
    budget.setdefault("max_pages",
                      topic_max_pages
                      or int(settings.get("max_pages", 20) or 20))
    # LEARN-005：种子 URL 作为会话起始页面
    if topic_seeds:
        budget.setdefault("seed_urls", topic_seeds)

    if agent.active_session() is not None:
        if body.get("enqueue") is True:
            from ..services.learning_scheduler import enqueue_waiting_session, list_waiting_sessions
            pos = enqueue_waiting_session(topic_id, budget)
            if pos == 0:
                raise ApiError(ERR_SESSION_RUNNING,
                               "等待队列已满（20 个），请稍后再试")
            return ok({"queued": True, "position": pos,
                       "topic_id": topic_id,
                       "waiting": list_waiting_sessions()},
                      message=f"已加入等待队列（第 {pos} 位），"
                              "当前会话结束后自动启动")
        raise ApiError(ERR_SESSION_RUNNING,
                       "已有学习会话进行中，请先停止当前会话",
                       suggestion="或提交 enqueue=true 加入等待队列")

    session = agent.create_session(topic_id, goal, budget=budget)
    _persist_session_row(session)
    _agent_call(agent.start_session, session)
    log.info("学习会话已启动: %s topic=%s goal=%s",
             session.session_id[:8], topic_id[:8], goal)
    return ok(session.to_status_dict(), message="学习会话已启动")


@router.post("/learn/session/pause")
def session_pause(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """暂停学习会话。"""
    sid = _resolve_session_id(body=body)
    session = _agent_call(get_browser_agent_service().pause_session, sid)
    _persist_session_row(session)
    return ok(session.to_status_dict(), message="已暂停")


@router.post("/learn/session/resume")
def session_resume(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """恢复学习会话。"""
    sid = _resolve_session_id(body=body)
    session = _agent_call(get_browser_agent_service().resume_session, sid)
    _persist_session_row(session)
    return ok(session.to_status_dict(), message="已恢复")


@router.post("/learn/session/stop")
def session_stop(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """停止学习会话（Agent 循环下一拍退出并生成报告）。"""
    sid = _resolve_session_id(body=body)
    session = _agent_call(get_browser_agent_service().stop_session, sid)
    return ok(session.to_status_dict(), message="停止请求已受理")


@router.get("/learn/session/status")
def session_status(session_id: str = Query(default="")) -> dict[str, Any]:
    """会话状态快照；session_id 缺省时返回活跃会话或最近一次会话。"""
    agent = get_browser_agent_service()
    sid = (session_id or "").strip()
    session = agent.get_session(sid) if sid else agent.active_session()
    if session is None and not sid:
        sessions = agent.list_sessions()
        session = max(sessions, key=lambda s: s.created_at) if sessions else None
    if session is None:
        # 尝试从数据库恢复最近会话状态
        db = _db()
        if db is not None:
            try:
                ensure_learning_tables()
                if sid:
                    row = db.query_one(
                        "SELECT id, topic_id, goal, budget, status,"
                        " pages_visited, knowledge_extracted, coverage,"
                        " stop_reason, trigger_type, started_at, ended_at,"
                        " created_at, updated_at"
                        " FROM learning_sessions WHERE id=?", (sid,))
                else:
                    row = db.query_one(
                        "SELECT id, topic_id, goal, budget, status,"
                        " pages_visited, knowledge_extracted, coverage,"
                        " stop_reason, trigger_type, started_at, ended_at,"
                        " created_at, updated_at"
                        " FROM learning_sessions "
                        "ORDER BY created_at DESC LIMIT 1")
                if row is not None:
                    return ok({
                        "session_id": row["id"], "topic_id": row["topic_id"],
                        "goal": row.get("goal", ""),
                        "status": row.get("status", ""),
                        "pages_visited": row.get("pages_visited", 0),
                        "knowledge_extracted":
                            row.get("knowledge_extracted", 0),
                        "coverage": row.get("coverage", 0.0),
                        "budget": parse_json(row.get("budget"), {}),
                        "stop_reason": row.get("stop_reason", ""),
                        "source": "db",
                    })
            except Exception as exc:  # noqa: BLE001
                log.warning("会话状态查询失败: %s", exc, exc_info=True)
        return ok({"status": "idle", "message": "当前没有学习会话"})
    _persist_session_row(session)
    return ok(session.to_status_dict())


def _session_progress_snapshot() -> dict:
    """活跃/最近会话的状态快照（WS 推送用，与 GET /learn/session/status 同构）。"""
    agent = get_browser_agent_service()
    session = agent.active_session()
    if session is None:
        sessions = agent.list_sessions()
        session = max(sessions, key=lambda s: s.created_at) if sessions else None
    if session is None:
        return {"status": "idle"}
    return session.to_status_dict()


@router.websocket("/learn/session/progress")
async def learn_session_progress_ws(websocket: WebSocket) -> None:
    """学习进度实时推送（文档B §7.1.2 /learn/session/progress WS）。

    每 2 秒推送一次会话状态快照（审计 R2-B05：替代前端轮询
    GET /learn/session/status），消息格式：
    {"type": "learn_progress", "data": {status, topic, pages_visited, ...}}
    无会话时 data={"status": "idle"}；客户端断开即结束。
    快照采集为内存读（agent 会话对象），不阻塞事件循环。
    """
    from ..middleware.cors import ws_origin_guard
    if not await ws_origin_guard(websocket):  # 审计 R3-SEC1：防 CSWSH
        return
    await websocket.accept()
    log.info("学习进度推送 WebSocket 已连接")
    try:
        while True:
            try:
                payload = _session_progress_snapshot()
            except Exception as exc:  # noqa: BLE001
                log.debug("学习进度快照采集失败: %s", exc)
                payload = {"status": "idle"}
            await websocket.send_json(
                {"type": "learn_progress", "data": payload})
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        log.info("学习进度推送 WebSocket 已断开")
    except Exception as exc:  # noqa: BLE001
        log.warning("学习进度推送异常：%s", exc, exc_info=True)


@router.get("/learn/session/logs")
def session_logs(session_id: str = Query(default=""),
                 limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
    """会话操作日志（时间戳/动作/理由/结果）。"""
    agent = get_browser_agent_service()
    sid = _resolve_session_id(session_id=session_id)
    session = agent.get_session(sid)
    if session is not None:
        logs = list(session.logs)[-limit:]
        return ok({"session_id": sid, "items": logs, "total": len(logs)})
    # 内存无此会话 → 查 learning_logs 表
    db = _db()
    if db is not None:
        try:
            ensure_learning_tables()
            rows = db.query(
                "SELECT ts, action, reason, result FROM learning_logs "
                "WHERE session_id=? ORDER BY ts DESC LIMIT ?",
                (sid, limit))
            items = list(reversed(rows))
            return ok({"session_id": sid, "items": items,
                       "total": len(items), "source": "db"})
        except Exception as exc:  # noqa: BLE001
            log.warning("日志查询失败: %s", exc, exc_info=True)
    raise ApiError("LEARN_SESSION_NOT_FOUND", "学习会话不存在", detail={"session_id": sid})


@router.get("/learn/session/report")
def session_report(session_id: str = Query(default="")) -> dict[str, Any]:
    """学习完成报告（进行中的会话返回当前进度报告）。"""
    agent = get_browser_agent_service()
    sid = _resolve_session_id(session_id=session_id)
    session = agent.get_session(sid)
    if session is not None:
        return ok(agent.build_report(session))
    db = _db()
    if db is not None:
        try:
            ensure_learning_tables()
            row = db.query_one(
                "SELECT id, topic_id, goal, budget, status,"
                " pages_visited, knowledge_extracted, coverage,"
                " stop_reason, trigger_type, started_at, ended_at,"
                " created_at, updated_at"
                " FROM learning_sessions WHERE id=?", (sid,))
            if row is not None:
                logs = db.query(
                    "SELECT ts, action, reason, result FROM learning_logs "
                    "WHERE session_id=? ORDER BY ts ASC LIMIT 100", (sid,))
                return ok({
                    "session_id": row["id"], "topic_id": row["topic_id"],
                    "goal": row.get("goal", ""),
                    "status": row.get("status", ""),
                    "stop_reason": row.get("stop_reason", ""),
                    "pages_visited": row.get("pages_visited", 0),
                    "knowledge_extracted": row.get("knowledge_extracted", 0),
                    "coverage": row.get("coverage", 0.0),
                    "budget": parse_json(row.get("budget"), {}),
                    "logs": logs, "source": "db",
                })
        except Exception as exc:  # noqa: BLE001
            log.warning("报告查询失败: %s", exc, exc_info=True)
    raise ApiError("LEARN_SESSION_NOT_FOUND", "学习会话不存在", detail={"session_id": sid})


# ═══════════════════════════════════════════════════════════════════
#  学习设置与配额
# ═══════════════════════════════════════════════════════════════════

@router.get("/learn/settings")
def learn_settings_get() -> dict[str, Any]:
    """读取学习设置（时长/页数/搜索引擎/黑白名单/广告过滤/流量/
    学习时段/自动微调频率/行为学习开关）。"""
    return ok(get_learning_settings())


@router.put("/learn/settings")
def learn_settings_put(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """更新学习设置并持久化到 learning_settings 表（变更即时生效）。"""
    if not isinstance(body, dict) or not body:
        raise ApiError("SYSTEM_PARAM_INVALID", "请求体必须是非空 JSON 对象")
    # 基本校验
    if "max_time_minutes" in body:
        v = int(body["max_time_minutes"])
        if not 1 <= v <= 60:
            raise ApiError("UNSUPPORTED_FORMAT", "max_time_minutes 范围为 1~60")
        body["max_time_minutes"] = v
    if "max_pages" in body:
        v = int(body["max_pages"])
        if not 1 <= v <= 200:
            raise ApiError("UNSUPPORTED_FORMAT", "max_pages 范围为 1~200")
        body["max_pages"] = v
    if "search_engine" in body:
        from ..services.browser_agent_service import SEARCH_ENGINES
        if body["search_engine"] not in SEARCH_ENGINES:
            raise ApiError("UNSUPPORTED_FORMAT",
                           f"search_engine 仅支持 {list(SEARCH_ENGINES)}")
    for list_key in ("domain_whitelist", "domain_blacklist",
                     "schedule_windows"):
        if list_key in body and not isinstance(body[list_key], list):
            raise ApiError("UNSUPPORTED_FORMAT", f"{list_key} 必须是数组")
    if "daily_traffic_limit_mb" in body:
        v = float(body["daily_traffic_limit_mb"])
        if v <= 0:
            raise ApiError("UNSUPPORTED_FORMAT", "daily_traffic_limit_mb 必须大于 0")
        body["daily_traffic_limit_mb"] = v
    # LEARN-037：资源阈值设置（cpu/mem/bandwidth 上限校验）
    for key, lo, hi in (("cpu_percent_limit", 1.0, 100.0),
                        ("mem_percent_limit", 1.0, 100.0),
                        ("bandwidth_mbps_limit", 0.1, 1000.0)):
        if key in body:
            v = float(body[key])
            if not lo <= v <= hi:
                raise ApiError("SYSTEM_PARAM_INVALID", f"{key} 范围为 {lo}~{hi}")
            body[key] = v
    # §4.3 自适应：用户重新开启自动微调频率（非 off）时清除质量下降暂停旗标
    if "auto_finetune_frequency" in body \
            and str(body["auto_finetune_frequency"]) != "off":
        body["auto_train_paused"] = False
    merged = update_learning_settings(body)
    log.info("学习设置已更新: %s", sorted(body.keys()))
    return ok(merged, message="设置已保存")


@router.get("/learn/quota")
def learn_quota() -> dict[str, Any]:
    """当前资源配额快照（TASK-014 配额矩阵 + 流量消耗）。

    LEARN-063：容量接近上限（知识库 ≥90% 或主题数 ≥90%）时返回
    near_capacity=true 及明细，供前端告警。"""
    scheduler = get_learning_scheduler()
    quota = scheduler.current_quota()
    settings = get_learning_settings()
    traffic_today = get_traffic_today()
    quota["traffic"] = {
        "today_bytes": traffic_today,
        "today_mb": round(traffic_today / 1048576, 3),
        "daily_limit_mb": float(settings.get("daily_traffic_limit_mb", 50)
                                or 50),
    }
    quota["evaluation"] = scheduler.evaluate()
    # LEARN-063：容量告警
    capacity: dict[str, Any] = {"near_capacity": False, "reasons": []}
    try:
        from ..services.knowledge_service import get_knowledge_service
        kstats = get_knowledge_service().stats()
        k_total = int(kstats.get("total", 0) or 0)
        k_cap = int(kstats.get("capacity", 100000) or 100000)
        if k_cap > 0 and k_total >= k_cap * 0.9:
            capacity["near_capacity"] = True
            capacity["reasons"].append(
                f"知识库容量 {k_total}/{k_cap}（≥90%）")
        capacity["knowledge"] = {"total": k_total, "capacity": k_cap}
    except Exception:  # noqa: BLE001
        log.debug("learn_quota: 降级忽略", exc_info=True)
    try:
        db = _db()
        t_count = db.count("learning_topics") if db is not None \
            else len(_mem_topics)
        if t_count >= MAX_TOPICS * 0.9:
            capacity["near_capacity"] = True
            capacity["reasons"].append(
                f"主题数量 {t_count}/{MAX_TOPICS}（≥90%）")
        capacity["topics"] = {"total": t_count, "capacity": MAX_TOPICS}
    except Exception:  # noqa: BLE001
        log.debug("learn_quota: 降级忽略", exc_info=True)
    quota["capacity"] = capacity
    return ok(quota)


# ═══════════════════════════════════════════════════════════════════
#  契约别名（规格 §7.1.2：/learn/settings/get|update、/learn/session/log）
# ═══════════════════════════════════════════════════════════════════

# 本项目仅供学习使用，商业授权请+Q 3559331368
