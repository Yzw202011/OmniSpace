"""对话 API 路由（TASK-004 真实推理实现，兼容规格 §4.2 端点路径）。

端点清单：
- POST   /dialog/send                    发送消息（支持 stream=true 返回 SSE）
- POST   /chat/stream                    SSE 流式对话（F-011，等价 /dialog/send?stream=true）
- GET    /dialog/history                 获取历史（session_id, limit=50）
- GET    /dialog/sessions                获取会话列表
- POST   /dialog/sessions                创建新会话
- DELETE /dialog/sessions/{session_id}   删除会话
- GET    /dialog/status                  对话引擎状态
- POST   /chat/send                      /dialog/send 别名（文档 TASK-004）
- GET    /chat/history                   分页对话历史（page/page_size）
- POST   /chat/clear                     清空会话历史

真实推理链路：
  接收消息 → RAG 注入（injection_service 契约，容错）→ build_context 组装
  → DialogEngine(Qwen3-VL) 流式/非流式推理 → 持久化 dialog_messages

错误约定：
  - SYSTEM_PARAM_INVALID 空消息/缺少必填参数（审计 R1-06，替代误用的 30001）
  - 30003/30004 模型未就绪/加载失败（含显存不足描述）
  - 40002 输入过长；40005 会话不存在；40007 功能互斥
流式期间持有 "dialog" 功能锁（规格 §6.1）。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import time
import uuid

from fastapi import APIRouter, Body, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from ..config import DIALOG_MAX_INPUT_CHARS
from ..data.crypto import decrypt_text, encrypt_text
from ..data.database import get_db_safe, parse_json
from ..data.models import DialogSessionCreate
from ..middleware.error_handler import ApiError, ok
from ..middleware.feature_lock import acquire_or_raise
from ..services.inference.dialog_engine import (
    DEFAULT_SYSTEM_PROMPT,
    get_dialog_engine,
)

router = APIRouter()
log = logging.getLogger("omnispace.api.dialog")

# ── 内存态兜底存储（数据库不可用时降级，保证不崩溃）──────────────────
_mock_sessions: dict[str, dict] = {}
_mock_messages: dict[str, list[dict]] = {}
# 流式生成停止标记（session_id 集合，/chat/stop 置位，流结束后清除）
_stop_flags: set[str] = set()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _now() -> float:
    return time.time()


def _row_to_session(row: dict) -> dict:
    return {
        "id": row["id"],
        "title": row.get("title", "新对话"),
        "model": row.get("model", ""),
        "pinned": bool(row.get("pinned", 0)),
        "mode": row.get("mode", "") or "",
        "created_at": row.get("created_at", 0),
        "updated_at": row.get("updated_at", 0),
    }


def _row_to_message(row: dict) -> dict:
    return {
        "id": row["id"],
        "session_id": row.get("session_id", ""),
        "role": row["role"],
        # 要求#36：content 落库加密，读取统一经此漏斗解密（明文历史透传）
        "content": decrypt_text(row.get("content", "")),
        "attachments": parse_json(row.get("attachments"), None),
        "images": parse_json(row.get("attachments"), None) or [],
        "model_used": row.get("model_used", ""),
        "engine": row.get("model_used", ""),
        "rating": int(row.get("rating", 0) or 0),
        "favorite": bool(row.get("favorite", 0)),
        "created_at": row.get("timestamp", 0),
        "timestamp": row.get("timestamp", 0),
    }


def _sse(payload) -> str:
    """格式化一条 SSE 事件。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── 多模态图片解码 ──────────────────────────────────────────────────

def _decode_images(images: list | None) -> list:
    """把 base64 图片列表解码为 PIL.Image；坏数据跳过。"""
    if not images:
        return []
    try:
        from PIL import Image
    except Exception:
        return []
    out = []
    for item in images[:4]:  # 最多 4 张
        try:
            if isinstance(item, dict):
                item = item.get("data") or item.get("base64") or ""
            if not isinstance(item, str) or not item:
                continue
            if "," in item and item.split(",", 1)[0].startswith("data:"):
                item = item.split(",", 1)[1]
            img = Image.open(io.BytesIO(base64.b64decode(item)))
            out.append(img.convert("RGB"))
        except Exception as exc:
            log.warning("图片解码失败（跳过）: %s", exc)
    return out


# ── RAG 注入（契约：另一 agent 实现，容错 import）─────────────────────

def _rag_enhance(message: str) -> tuple[str, list]:
    """调用 injection_service 增强，返回 (注入文本, refs)。失败返回 ("", [])。"""
    try:
        from ..services.injection_service import get_injection_service  # type: ignore
        svc = get_injection_service()
        result = svc.enhance_chat(message)
        if isinstance(result, tuple) and len(result) == 2:
            return result[0] or "", result[1] or []
        if isinstance(result, str):
            return result, []
    except Exception as exc:
        log.debug("RAG 注入不可用，跳过: %s", exc)
    return "", []


# ── 被动补全（文档B §7.1.6.1 步骤4，审计 R2-B01）──────────────────────
# 流程：检测回复不确定性标记 → 提取关键词 → 浏览器快速搜索 1~3 页 →
#       补充上下文重推理一次；同时 fire dialog_gap 触发器沉淀长期学习。
# 全程容错：浏览器/网络不可用、搜索超时、重推理失败均回退原回复，不崩溃。

_UNCERTAINTY_MARKERS = (
    "我不确定", "无法确定", "我不能确定", "不太确定", "没有相关信息",
    "没有足够的信息", "没有足够信息", "信息不足", "我不知道", "无法回答",
    "无法提供准确", "无法给出准确", "无法确认", "找不到相关", "缺乏相关",
    "暂未掌握", "没有掌握", "超出我的知识", "知识库中没有", "资料不足",
    "无法核实", "不能确认", "没有查到", "不确定是否",
)

_PASSIVE_MAX_PAGES = 3          # 快速搜索页数上限（文档：1~3 页）
_PASSIVE_PAGE_CHARS = 1500      # 每页截取正文字符数
_PASSIVE_TOTAL_CHARS = 3000     # 补充上下文总字符上限
_PASSIVE_SEARCH_BUDGET_S = 25.0  # 快速搜索总时间预算（秒）

# 轻量关键词提取的停用片段（无 jieba 依赖，面向疑问句去噪）
_STOP_PHRASES = (
    "请问", "告诉我", "帮我", "我想知道", "你知道吗", "是什么", "什么是",
    "为什么", "怎么", "怎样", "如何", "哪一些", "哪些", "哪个", "哪里",
    "多少", "是不是", "能不能", "可以不可以", "吗", "呢", "吧", "啊",
    "的", "了", "着", "在", "和", "与", "及", "或", "又", "也", "都",
)


def _detect_uncertainty(reply: str) -> bool:
    """检测回复中的不确定性标记（文档B §7.1.6.1 步骤4 触发条件）。"""
    if not reply:
        return False
    head = reply[:800]  # 不确定性声明通常出现在开头/结论段
    tail = reply[-400:]
    probe = head + "\n" + tail
    return any(marker in probe for marker in _UNCERTAINTY_MARKERS)


def _extract_keywords(message: str, max_kw: int = 5) -> list[str]:
    """轻量关键词提取（无 NLP 依赖）：拉丁/数字词 + 去停用片段后的中文长段。"""
    text = re.sub(r"[\s，。！？；：、“”‘’（）()【】《》<>…—\-·,.!?;:'\"[\]{}]+",
                  "\n", message.strip())
    segs = [s for s in text.split("\n") if s]
    kws: list[str] = []
    for seg in segs:
        latin = re.findall(r"[A-Za-z0-9][A-Za-z0-9_+#.\-]{1,}", seg)
        kws.extend(latin)
        cleaned = seg
        for sp in _STOP_PHRASES:
            cleaned = cleaned.replace(sp, " ")
        for piece in cleaned.split():
            piece = piece.strip()
            # 中文段取 2~12 字（过短无信息量，过长搜索引擎不友好）
            if 2 <= len(piece) <= 12 and piece not in kws:
                kws.append(piece)
            elif len(piece) > 12 and piece[:12] not in kws:
                kws.append(piece[:12])
    # 去重保序，限量
    seen: dict[str, None] = {}
    for k in kws:
        seen.setdefault(k, None)
    return list(seen.keys())[:max_kw]


def _fire_dialog_gap(sid: str, message: str, keywords: list[str]) -> None:
    """沉淀长期学习：fire dialog_gap 触发器（learning_scheduler 默认动作
    会把缺口主题沉淀进 learning_topics，供空闲/定时触发器后续学习）。"""
    try:
        from ..services.learning_scheduler import (
            TRIGGER_DIALOG_GAP, get_learning_scheduler)
        get_learning_scheduler().fire_trigger(TRIGGER_DIALOG_GAP, {
            "session_id": sid,
            "message": message[:200],
            "keywords": keywords,
        })
    except Exception as exc:  # noqa: BLE001 - 触发器失败不影响对话主链
        log.debug("dialog_gap 触发器触发失败（忽略）: %s", exc)


def _quick_search_supplement(keywords: list[str]) -> str:
    """快速搜索 1~3 页并截取正文（同步阻塞，调用方须放线程池）。

    复用浏览器池（headless Chromium）：导航搜索页 → 提取结果链接 →
    依序读内容页正文。离线/浏览器不可用/超时均返回 ""。
    全程不填写任何表单（仅直接导航搜索 URL，TC-S-012 约束）。
    """
    if not keywords:
        return ""
    try:
        from ..services.browser_agent_service import (
            _is_content_link, build_search_url)
        from ..services.browser_pool import get_browser_pool
    except Exception as exc:  # noqa: BLE001
        log.debug("被动补全搜索依赖不可用: %s", exc)
        return ""

    t0 = time.time()
    browser = None
    pool = None
    try:
        pool = get_browser_pool()
        browser = pool.acquire(timeout=5.0)
    except Exception as exc:  # noqa: BLE001
        log.debug("被动补全：浏览器实例获取失败（跳过搜索）: %s", exc)
        if pool is not None and browser is not None:
            pool.release(browser)
        return ""

    texts: list[str] = []
    try:
        query = " ".join(keywords[:3])
        browser.navigate(build_search_url(query))
        try:
            links = browser.get_links()
        except Exception:  # noqa: BLE001
            links = []
        # 搜索页自身的可见文本也计入（部分搜索引擎直接给摘要）
        try:
            page_text = (browser.get_text() or "")[:_PASSIVE_PAGE_CHARS]
            if page_text.strip():
                texts.append(page_text)
        except Exception:  # noqa: BLE001
            pass
        # 依序读内容页（总量 ≤3 页，含搜索页；遵守时间预算）
        for link in links:
            if len(texts) >= _PASSIVE_MAX_PAGES:
                break
            if time.time() - t0 > _PASSIVE_SEARCH_BUDGET_S:
                break
            href = str((link or {}).get("href") or "")
            if not href or not _is_content_link(href):
                continue
            try:
                browser.navigate(href)
                body = (browser.get_text() or "")[:_PASSIVE_PAGE_CHARS]
                if body.strip():
                    texts.append(body)
            except Exception as exc:  # noqa: BLE001 - 单页失败跳下一页
                log.debug("被动补全读页失败（跳过）: %s", exc)
                continue
    except Exception as exc:  # noqa: BLE001
        log.debug("被动补全搜索异常（回退原回复）: %s", exc)
    finally:
        try:
            if pool is not None:
                pool.release(browser)
        except Exception:  # noqa: BLE001
            pass

    combined = "\n\n".join(t.strip() for t in texts if t.strip())
    combined = re.sub(r"[ \t]+", " ", combined)
    return combined[:_PASSIVE_TOTAL_CHARS]


def _passive_reinfer(engine, message: str, history: list[dict],
                     knowledge_text: str, supplement: str,
                     images: list, temperature: float,
                     max_new_tokens: int, max_ctx: int) -> str:
    """补充上下文后重推理一次（同步阻塞，调用方须放线程池）。"""
    augmented = (knowledge_text or "")
    augmented += ("\n\n[联网补充资料]\n" + supplement) if supplement else ""
    messages = engine.build_context(
        message, history=history, knowledge_text=augmented,
        system_prompt=DEFAULT_SYSTEM_PROMPT, images=images or None,
        max_tokens=max_ctx,
    )
    return engine.chat(messages, images or None, temperature, max_new_tokens)


async def _maybe_passive_completion(
        engine, message: str, reply: str, history: list[dict],
        knowledge_text: str, sid: str, images: list,
        temperature: float, max_new_tokens: int,
        max_ctx: int) -> tuple[str, dict | None]:
    """被动补全编排（审计 R2-B01）。

    命中不确定性标记 → fire dialog_gap 触发器 → 线程池快速搜索 →
    有补充资料则重推理一次。返回 (最终回复, 补全信息|None)。
    任何子步骤失败均回退原回复（诚实降级，不伪造补充内容）。
    """
    if not _detect_uncertainty(reply):
        return reply, None
    keywords = _extract_keywords(message)
    log.info("检测到回复不确定性标记，启动被动补全: sid=%s keywords=%s",
             sid[:8], keywords)
    _fire_dialog_gap(sid, message, keywords)
    supplement = ""
    try:
        supplement = await asyncio.to_thread(_quick_search_supplement,
                                             keywords)
    except Exception as exc:  # noqa: BLE001
        log.debug("被动补全搜索失败（回退原回复）: %s", exc)
    if not supplement.strip():
        return reply, {"triggered": True, "keywords": keywords,
                       "supplemented": False}
    try:
        new_reply = await asyncio.to_thread(
            _passive_reinfer, engine, message, history, knowledge_text,
            supplement, images, temperature, max_new_tokens, max_ctx)
    except Exception as exc:  # noqa: BLE001
        log.warning("被动补全重推理失败（回退原回复）: %s", exc)
        return reply, {"triggered": True, "keywords": keywords,
                       "supplemented": False}
    if not new_reply.strip():
        return reply, {"triggered": True, "keywords": keywords,
                       "supplemented": False}
    return new_reply, {"triggered": True, "keywords": keywords,
                       "supplemented": True,
                       "supplement_chars": len(supplement)}



# ── 持久化辅助 ──────────────────────────────────────────────────────

def _ensure_session(sid: str, title_seed: str, model: str) -> None:
    now = _now()
    db = get_db_safe()
    if db is not None:
        try:
            sess = db.query_one("SELECT id FROM dialog_sessions WHERE id=?",
                                (sid,))
            if sess is None:
                db.insert("dialog_sessions", {
                    "id": sid,
                    "title": title_seed[:20] or "新对话",
                    "model": model,
                    "created_at": now,
                    "updated_at": now,
                })
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("会话落库失败，降级内存: %s", exc)
    if sid not in _mock_sessions:
        _mock_sessions[sid] = {
            "id": sid, "title": title_seed[:20] or "新对话",
            "model": model, "created_at": now, "updated_at": now,
        }
        _mock_messages.setdefault(sid, [])


def _save_message(sid: str, role: str, content: str,
                  model_used: str = "", attachments=None) -> dict:
    msg = {
        "id": uuid.uuid4().hex,
        "session_id": sid,
        "role": role,
        "content": content,
        "attachments": attachments,
        "model_used": model_used,
        "timestamp": _now(),
    }
    db = get_db_safe()
    if db is not None:
        try:
            # 要求#36：落库前加密 content；返回值保持明文供调用方使用
            stored = dict(msg)
            stored["content"] = encrypt_text(content)
            db.insert("dialog_messages", stored)
            db.update("dialog_sessions", {"updated_at": msg["timestamp"]},
                      "id=?", (sid,))
            return _row_to_message(msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("消息落库失败，降级内存: %s", exc)
    _mock_messages.setdefault(sid, []).append(msg)
    if sid in _mock_sessions:
        _mock_sessions[sid]["updated_at"] = msg["timestamp"]
    return {k: v for k, v in msg.items() if k != "session_id"}


def _load_history(sid: str, max_rounds: int = 20) -> list[dict]:
    """取最近 N 轮历史（user/assistant 成对，时间升序）供上下文组装。"""
    limit = max_rounds * 2
    db = get_db_safe()
    rows: list[dict] = []
    if db is not None:
        try:
            rows = db.query(
                "SELECT role, content FROM dialog_messages WHERE session_id=? "
                "ORDER BY timestamp DESC LIMIT ?",
                (sid, limit),
            )
            rows.reverse()
        except Exception as exc:  # noqa: BLE001
            log.warning("历史查询失败，降级内存: %s", exc)
            rows = []
    if not rows and sid in _mock_messages:
        rows = [{"role": m["role"], "content": m["content"]}
                for m in _mock_messages[sid][-limit:]]
    return [{"role": r["role"], "content": decrypt_text(r.get("content", ""))} for r in rows
            if r.get("role") in ("user", "assistant")]


# ── 发送消息 ────────────────────────────────────────────────────────

@router.post("/dialog/send")
@router.post("/chat/send")
async def dialog_send(body: dict = Body(default_factory=dict)):
    """发送对话消息（真实推理）。

    请求: {"message"|"content": str, "images"?: [base64], "model"?: str,
           "session_id"?: str, "stream"?: bool, "temperature"?: float,
           "max_new_tokens"?: int}
    stream=true → SSE（data: {"token":...} / {"knowledge_refs":...} / [DONE]）
    stream 缺省/false → 一次性返回完整文本。
    """
    message = str(body.get("message") or body.get("content") or "").strip()
    if not message:
        raise ApiError("SYSTEM_PARAM_INVALID", "消息不能为空")
    # 单条消息字符上限（DoS 防护）：与 token 上下文窗口分开判定，
    # 防止超长字符输入进入分词/推理管线导致阻塞。
    if len(message) > DIALOG_MAX_INPUT_CHARS:
        raise ApiError(40002,
                       f"输入内容过长（{len(message)} 字符），"
                       f"单条消息上限 {DIALOG_MAX_INPUT_CHARS} 字符，请缩短后重试")

    sid = str(body.get("session_id") or "").strip() or uuid.uuid4().hex
    model_req = body.get("model") or None
    stream = bool(body.get("stream", False))
    try:
        temperature = float(body.get("temperature", 0.7))
    except (TypeError, ValueError):
        temperature = 0.7
    try:
        max_new_tokens = int(body.get("max_new_tokens", 1024))
    except (TypeError, ValueError):
        max_new_tokens = 1024

    images = _decode_images(body.get("images") or body.get("attachments"))

    engine = get_dialog_engine()
    lock = await acquire_or_raise("dialog", task_id=sid)
    lock_handed_off = False  # 流式路径下锁移交给 SSE 生成器
    try:
        # 新一轮发送开始时丢弃陈旧停止标记：/chat/stop 在空闲会话上
        # 置标记后无活跃流触发 finally 清理，若不清除会使下一次流式
        # 生成被立即停止（0 token）。停止进行中的流不受影响——标记在
        # 本次 send 之后设置，生成循环正常感知。
        _stop_flags.discard(sid)
        # 引擎未加载时尝试加载；失败 → 30xxx 段友好错误。
        # 审计 R1-04：ensure_loaded 为 15s 级阻塞调用，放线程池执行，
        # 避免卡住事件循环
        if not engine.is_ready and not await asyncio.to_thread(
                engine.ensure_loaded, model_req):
            status = engine.get_status()
            code = 30004 if status["state"] == "error" else 30003
            raise ApiError(code,
                           status["last_error"] or "对话模型未就绪，请稍后再试",
                           detail={"engine": status})

        # RAG 注入
        knowledge_text, refs = _rag_enhance(message)

        # 组装上下文（系统 Prompt + 注入 + 历史 + 当前输入）
        history = _load_history(sid)
        max_ctx = min(int(body.get("context_length", 8192) or 8192), 8192)
        messages = engine.build_context(
            message, history=history, knowledge_text=knowledge_text,
            system_prompt=DEFAULT_SYSTEM_PROMPT, images=images or None,
            max_tokens=max_ctx,
        )

        _ensure_session(sid, message, model_req or engine.model_name)
        _save_message(sid, "user", message,
                      attachments=body.get("attachments"))

        if stream:
            resp = await _stream_response(
                engine, lock, sid, message, history, knowledge_text,
                messages, images, refs,
                temperature, max_new_tokens, max_ctx)
            lock_handed_off = True
            return resp

        # 非流式
        try:
            reply = await asyncio.to_thread(
                engine.chat, messages, images or None,
                temperature, max_new_tokens)
        except RuntimeError as exc:
            raise ApiError(30004, str(exc) or "对话推理失败") from exc
        # 被动补全（文档B §7.1.6.1 步骤4，R2-B01）：
        # 不确定性回复 → 快速搜索 1~3 页 → 补充上下文重推理一次
        reply, passive = await _maybe_passive_completion(
            engine, message, reply, history, knowledge_text, sid,
            images, temperature, max_new_tokens, max_ctx)
        msg = _save_message(sid, "assistant", reply,
                            model_used=engine.model_name)
        return ok({
            "session_id": sid,
            "message": msg,
            "knowledge_refs": refs,
            "passive_completion": passive,
            "first_token_ms": round(engine.last_first_token_ms, 1),
        })
    finally:
        # 流式路径下锁由 SSE 生成器持有至流结束；其余路径在此释放
        if not lock_handed_off:
            await lock.release("dialog")


@router.post("/chat/stream")
async def chat_stream(body: dict = Body(default_factory=dict)):
    """SSE 流式对话（F-011 / 文档 §7.1.4 /v1/chat → /stream）。

    等价于 POST /dialog/send 且强制 stream=true：
    Content-Type: text/event-stream
    事件序列：data: {"session_id"} → data: {"knowledge_refs"}? →
              data: {"token": str}* → data: {"meta": {...}} → data: [DONE]
    错误事件：data: {"error": str, "code": int}（随后 done 收尾）。
    中断：POST /chat/stop {"session_id"} 置停止标记。
    """
    forced = {**(body or {}), "stream": True}
    return await dialog_send(forced)


async def _stream_response(engine, lock, sid: str, message: str,
                           history: list, knowledge_text: str,
                           messages: list,
                           images: list, refs: list,
                           temperature: float, max_new_tokens: int,
                           max_ctx: int):
    """构造 SSE 流式响应；生成结束后落库并释放功能锁。"""

    async def event_gen():
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        collected: list[str] = []
        error_holder: list[str] = []

        def _produce():
            try:
                for token in engine.chat_stream(
                        messages, images=images or None,
                        temperature=temperature,
                        max_new_tokens=max_new_tokens,
                        stop_check=lambda: sid in _stop_flags):
                    collected.append(token)
                    loop.call_soon_threadsafe(
                        queue.put_nowait, ("token", token))
            except Exception as exc:  # noqa: BLE001 - 汇聚为错误事件
                log.exception("对话流式推理失败")
                error_holder.append(str(exc))
                loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        producer = loop.run_in_executor(None, _produce)
        try:
            yield _sse({"session_id": sid})
            if refs:
                yield _sse({"knowledge_refs": refs})
            while True:
                kind, payload = await queue.get()
                if kind == "token":
                    yield _sse({"token": payload})
                elif kind == "error":
                    yield _sse({"error": payload, "code": 30004})
                elif kind == "done":
                    break
            await producer
            # 被动补全（R2-B01）：首轮回复含不确定性标记时，快速搜索
            # 重推理一次，改进回复作为追加 token 继续推送（[DONE] 之前）
            if collected and not error_holder:
                reply0 = "".join(collected)
                new_reply, passive = await _maybe_passive_completion(
                    engine, message, reply0, history, knowledge_text, sid,
                    images, temperature, max_new_tokens, max_ctx)
                if passive:
                    yield _sse({"passive_completion": passive})
                if passive and passive.get("supplemented") \
                        and new_reply.strip() and new_reply != reply0:
                    collected.clear()
                    collected.append(new_reply)
                    yield _sse({"token": "\n\n" + new_reply})
        finally:
            _stop_flags.discard(sid)
            reply = "".join(collected)
            if reply:
                _save_message(sid, "assistant", reply,
                              model_used=engine.model_name)
            await lock.release("dialog")
        if not error_holder:
            yield _sse({"meta": {
                "model": engine.model_name,
                "first_token_ms": round(engine.last_first_token_ms, 1),
            }})
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


# ── 历史 / 会话 ─────────────────────────────────────────────────────

@router.get("/dialog/history")
def dialog_history(session_id: str = Query(..., description="会话ID"),
                   limit: int = Query(50, ge=1, le=500, description="返回条数上限")):
    """获取指定会话的历史消息（最近 limit 条，时间升序）。"""
    db = get_db_safe()
    if db is not None:
        try:
            rows = db.query(
                "SELECT id, session_id, role, content, attachments,"
                " model_used, rating, favorite, timestamp"
                " FROM dialog_messages WHERE session_id=? "
                "ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit),
            )
            rows.reverse()
            msgs = [_row_to_message(r) for r in rows]
            total = db.count("dialog_messages", "session_id=?", (session_id,))
            return ok({"session_id": session_id, "messages": msgs,
                       "total": total})
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库查询失败，降级内存存储: %s", exc)

    msgs = _mock_messages.get(session_id, [])
    recent = msgs[-limit:] if limit < len(msgs) else list(msgs)
    return ok({"session_id": session_id, "messages": recent,
               "total": len(msgs)})


@router.get("/chat/history")
def chat_history(session_id: str = Query("", description="会话ID（可选）"),
                 page: int = Query(1, ge=1),
                 page_size: int = Query(50, ge=1, le=200)):
    """分页对话历史（文档 TASK-004 /v1/chat/history）。

    指定 session_id 时返回该会话消息；否则返回跨会话最近消息。
    """
    offset = (page - 1) * page_size
    db = get_db_safe()
    if db is not None:
        try:
            if session_id:
                total = db.count("dialog_messages", "session_id=?",
                                 (session_id,))
                rows = db.query(
                    "SELECT id, session_id, role, content, attachments,"
                    " model_used, rating, favorite, timestamp"
                    " FROM dialog_messages WHERE session_id=? "
                    "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                    (session_id, page_size, offset))
            else:
                total = db.count("dialog_messages")
                rows = db.query(
                    "SELECT id, session_id, role, content, attachments,"
                    " model_used, rating, favorite, timestamp"
                    " FROM dialog_messages "
                    "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                    (page_size, offset))
            rows.reverse()
            return ok({"messages": [_row_to_message(r) for r in rows],
                       "total": total, "page": page, "page_size": page_size})
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库查询失败，降级内存: %s", exc)

    all_msgs = ([m for m in _mock_messages.get(session_id, [])]
                if session_id else
                [m for msgs in _mock_messages.values() for m in msgs])
    all_msgs.sort(key=lambda m: m.get("timestamp", 0))
    page_items = all_msgs[max(0, len(all_msgs) - offset - page_size):
                          len(all_msgs) - offset if offset else None]
    return ok({"messages": page_items, "total": len(all_msgs),
               "page": page, "page_size": page_size})


@router.post("/chat/clear")
def chat_clear(body: dict = Body(default_factory=dict)):
    """清空对话历史（文档 TASK-004）。

    body: {"session_id"?: str}；缺省时清空所有会话消息。
    """
    sid = str(body.get("session_id") or "").strip()
    db = get_db_safe()
    if db is not None:
        try:
            if sid:
                db.delete("dialog_messages", "session_id=?", (sid,))
            else:
                db.delete("dialog_messages", "1=1")
            return ok({"cleared": sid or "all"})
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库删除失败，降级内存: %s", exc)
    if sid:
        _mock_messages.pop(sid, None)
    else:
        _mock_messages.clear()
    return ok({"cleared": sid or "all"})


@router.get("/dialog/sessions")
def dialog_sessions():
    """获取会话列表（按最后更新时间倒序）。"""
    db = get_db_safe()
    if db is not None:
        try:
            rows = db.query(
                "SELECT id, title, model, pinned, mode, created_at, updated_at"
                " FROM dialog_sessions ORDER BY updated_at DESC")
            items = [_row_to_session(r) for r in rows]
            return ok({"items": items, "total": len(items)})
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库查询失败，降级内存存储: %s", exc)

    items = sorted(_mock_sessions.values(),
                   key=lambda s: s.get("updated_at", 0), reverse=True)
    return ok({"items": items, "total": len(items)})


@router.post("/dialog/sessions")
def dialog_create_session(req: DialogSessionCreate):
    """创建新会话。"""
    sid = uuid.uuid4().hex
    now = _now()
    session = {
        "id": sid,
        "title": req.title or "新对话",
        "model": req.model or "",
        "created_at": now,
        "updated_at": now,
    }
    db = get_db_safe()
    if db is not None:
        try:
            db.insert("dialog_sessions", session)
            return ok({"session": session}, message="会话已创建")
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库写入失败，降级内存存储: %s", exc)

    _mock_sessions[sid] = session
    _mock_messages[sid] = []
    return ok({"session": session}, message="会话已创建")


@router.delete("/dialog/sessions/{session_id}")
def dialog_delete_session(session_id: str):
    """删除会话及其历史消息。"""
    db = get_db_safe()
    if db is not None:
        try:
            sess = db.query_one(
                "SELECT id FROM dialog_sessions WHERE id=?", (session_id,))
            if sess is None:
                raise ApiError(40005, "会话不存在",
                               detail={"session_id": session_id})
            db.delete("dialog_messages", "session_id=?", (session_id,))
            db.delete("dialog_sessions", "id=?", (session_id,))
            return ok({"deleted": session_id})
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("数据库删除失败，降级内存存储: %s", exc)

    if session_id not in _mock_sessions:
        raise ApiError(40005, "会话不存在", detail={"session_id": session_id})
    _mock_sessions.pop(session_id, None)
    _mock_messages.pop(session_id, None)
    return ok({"deleted": session_id})


@router.get("/dialog/status")
def dialog_status():
    """对话引擎状态（模型可用性 / 显存 / 首 token 统计）。"""
    return ok(get_dialog_engine().get_status())


# ═══════════════════════════════════════════════════════════════════
#  /chat/* 端点族（规格 §4.1，前端 dialogApi 契约）
# ═══════════════════════════════════════════════════════════════════

def _get_session_row(sid: str) -> dict | None:
    """取会话原始行（db 优先，内存兜底）。"""
    db = get_db_safe()
    if db is not None:
        try:
            return db.query_one(
                "SELECT id, title, model, pinned, mode, created_at, updated_at"
                " FROM dialog_sessions WHERE id=?", (sid,))
        except Exception as exc:  # noqa: BLE001
            log.warning("会话查询失败，降级内存: %s", exc)
    return _mock_sessions.get(sid)


def _session_messages(sid: str) -> list[dict]:
    """取会话全部消息（时间升序，统一 _row_to_message 形状）。"""
    db = get_db_safe()
    if db is not None:
        try:
            rows = db.query(
                "SELECT id, session_id, role, content, attachments,"
                " model_used, rating, favorite, timestamp"
                " FROM dialog_messages WHERE session_id=? "
                "ORDER BY timestamp ASC", (sid,))
            return [_row_to_message(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            log.warning("消息查询失败，降级内存: %s", exc)
    return [_row_to_message(m) for m in _mock_messages.get(sid, [])]


def _enrich_session(row: dict) -> dict:
    """会话行 → 前端 DialogSession 契约（附加 last_message/message_count）。"""
    sess = _row_to_session(row)
    msgs = _session_messages(sess["id"])
    sess["message_count"] = len(msgs)
    sess["last_message"] = msgs[-1]["content"][:80] if msgs else ""
    return sess


@router.get("/chat/sessions")
def chat_list_sessions(keyword: str = Query(""),
                       page: int = Query(1, ge=1),
                       page_size: int = Query(50, ge=1, le=200)):
    """会话列表（置顶优先，再按更新时间倒序；支持标题/内容关键词搜索）。"""
    db = get_db_safe()
    rows: list[dict] = []
    if db is not None:
        try:
            rows = db.query(
                "SELECT id, title, model, pinned, mode, created_at, updated_at"
                " FROM dialog_sessions")
        except Exception as exc:  # noqa: BLE001
            log.warning("会话列表查询失败，降级内存: %s", exc)
            rows = []
    if not rows and db is None:
        rows = list(_mock_sessions.values())

    kw = keyword.strip()
    if kw:
        rows = [r for r in rows
                if kw in (r.get("title") or "")
                or any(kw in m.get("content", "")
                       for m in _session_messages(r["id"]))]
    rows.sort(key=lambda r: (bool(r.get("pinned", 0)),
                             r.get("updated_at", 0)), reverse=True)
    total = len(rows)
    start = (page - 1) * page_size
    items = [_enrich_session(r) for r in rows[start:start + page_size]]
    return ok({"items": items, "total": total,
               "page": page, "page_size": page_size})


@router.post("/chat/sessions")
def chat_create_session(body: dict = Body(default_factory=dict)):
    """创建新会话（{title?, mode?} → DialogSession）。"""
    sid = uuid.uuid4().hex
    now = _now()
    session = {
        "id": sid,
        "title": (body.get("title") or "新对话")[:50],
        "model": body.get("model") or "",
        "pinned": 1 if body.get("pinned") else 0,
        "mode": body.get("mode") or "",
        "created_at": now,
        "updated_at": now,
    }
    db = get_db_safe()
    if db is not None:
        try:
            db.insert("dialog_sessions", session)
            return ok(_enrich_session(session), message="会话已创建")
        except Exception as exc:  # noqa: BLE001
            log.warning("会话落库失败，降级内存: %s", exc)
    _mock_sessions[sid] = session
    _mock_messages[sid] = []
    return ok(_enrich_session(session), message="会话已创建")


@router.get("/chat/sessions/{session_id}")
def chat_get_session(session_id: str):
    """会话详情 + 全部消息（时间升序）。"""
    row = _get_session_row(session_id)
    if row is None:
        raise ApiError(40005, "会话不存在", detail={"session_id": session_id})
    data = _enrich_session(row)
    data["messages"] = _session_messages(session_id)
    return ok(data)


@router.get("/chat/sessions/{session_id}/messages")
def chat_list_messages(session_id: str,
                       page: int = Query(1, ge=1),
                       page_size: int = Query(200, ge=1, le=1000)):
    """会话消息列表（分页，时间升序）。"""
    if _get_session_row(session_id) is None:
        raise ApiError(40005, "会话不存在", detail={"session_id": session_id})
    msgs = _session_messages(session_id)
    total = len(msgs)
    start = (page - 1) * page_size
    return ok({"items": msgs[start:start + page_size], "total": total,
               "page": page, "page_size": page_size})


@router.put("/chat/sessions/{session_id}")
def chat_update_session(session_id: str, body: dict = Body(default_factory=dict)):
    """更新会话（重命名 title / 置顶 pinned / 模式 mode）。"""
    row = _get_session_row(session_id)
    if row is None:
        raise ApiError(40005, "会话不存在", detail={"session_id": session_id})
    patch: dict = {}
    if "title" in body:
        patch["title"] = str(body["title"] or "新对话")[:50]
    if "pinned" in body:
        patch["pinned"] = 1 if body["pinned"] else 0
    if "mode" in body:
        patch["mode"] = str(body["mode"] or "")
    if not patch:
        return ok(_enrich_session(row))
    patch["updated_at"] = _now()
    db = get_db_safe()
    if db is not None:
        try:
            db.update("dialog_sessions", patch, "id=?", (session_id,))
            row.update(patch)
            return ok(_enrich_session(row))
        except Exception as exc:  # noqa: BLE001
            log.warning("会话更新失败，降级内存: %s", exc)
    if session_id in _mock_sessions:
        _mock_sessions[session_id].update(patch)
        row = _mock_sessions[session_id]
    return ok(_enrich_session(row))


@router.delete("/chat/sessions/{session_id}")
def chat_delete_session(session_id: str):
    """删除会话及其消息（复用 /dialog/sessions 删除语义）。"""
    return dialog_delete_session(session_id)


@router.delete("/chat/sessions/{session_id}/messages")
def chat_clear_messages(session_id: str):
    """清空会话消息但保留会话（规格 §4.1）。"""
    if _get_session_row(session_id) is None:
        raise ApiError(40005, "会话不存在", detail={"session_id": session_id})
    db = get_db_safe()
    if db is not None:
        try:
            db.delete("dialog_messages", "session_id=?", (session_id,))
        except Exception as exc:  # noqa: BLE001
            log.warning("消息清空失败，降级内存: %s", exc)
    _mock_messages[session_id] = []
    return ok({"cleared": session_id}, message="历史已清空")


@router.post("/chat/sessions/{session_id}/messages/{message_id}/rating")
def chat_rate_message(session_id: str, message_id: str,
                      body: dict = Body(default_factory=dict)):
    """消息评分（1 赞 / -1 踩 / 0 取消，DIALOG-024）。"""
    try:
        rating = int(body.get("rating", 0))
    except (TypeError, ValueError):
        rating = 0
    rating = max(-1, min(1, rating))
    return _update_message_flag(session_id, message_id, "rating", rating)


@router.post("/chat/sessions/{session_id}/messages/{message_id}/favorite")
def chat_favorite_message(session_id: str, message_id: str):
    """收藏切换（DIALOG-046）：favorite 取反。"""
    return _update_message_flag(session_id, message_id, "favorite", None)


def _update_message_flag(sid: str, mid: str, field: str, value):
    """更新消息 rating/favorite 标志（db 优先，内存兜底），返回更新后消息。"""
    db = get_db_safe()
    if db is not None:
        try:
            row = db.query_one(
                "SELECT id, session_id, role, content, attachments,"
                " model_used, rating, favorite, timestamp"
                " FROM dialog_messages WHERE id=? AND session_id=?",
                (mid, sid))
            if row is None:
                raise ApiError(40005, "消息不存在",
                               detail={"message_id": mid})
            new_val = value if value is not None else (0 if row.get("favorite") else 1)
            db.update("dialog_messages", {field: new_val}, "id=?", (mid,))
            row[field] = new_val
            return ok(_row_to_message(row))
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("消息更新失败，降级内存: %s", exc)
    for m in _mock_messages.get(sid, []):
        if m["id"] == mid:
            m[field] = value if value is not None else (0 if m.get("favorite") else 1)
            return ok(_row_to_message(m))
    raise ApiError(40005, "消息不存在", detail={"message_id": mid})


@router.get("/chat/favorites")
def chat_list_favorites(page: int = Query(1, ge=1),
                        page_size: int = Query(50, ge=1, le=200)):
    """收藏夹列表（全部收藏消息，时间倒序）。"""
    db = get_db_safe()
    items: list[dict] = []
    if db is not None:
        try:
            rows = db.query(
                "SELECT id, session_id, role, content, attachments,"
                " model_used, rating, favorite, timestamp"
                " FROM dialog_messages WHERE favorite=1 "
                "ORDER BY timestamp DESC")
            items = [_row_to_message(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            log.warning("收藏查询失败，降级内存: %s", exc)
            items = []
    if not items and db is None:
        items = [_row_to_message(m)
                 for msgs in _mock_messages.values() for m in msgs
                 if m.get("favorite")]
        items.sort(key=lambda m: m.get("timestamp", 0), reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return ok({"items": items[start:start + page_size], "total": total,
               "page": page, "page_size": page_size})


@router.post("/chat/stop")
def chat_stop(body: dict = Body(default_factory=dict)):
    """停止指定会话进行中的流式生成（置停止标记，流式循环感知后收尾）。"""
    sid = str(body.get("session_id") or "").strip()
    if not sid:
        raise ApiError("SYSTEM_PARAM_INVALID", "session_id 不能为空")
    _stop_flags.add(sid)
    return ok({"stopped": sid})


# ═══════════════════════════════════════════════════════════════════
#  WebSocket 对话流（规格 §4.2 / §5.3，前端 ws.ts + useDialogStore 契约）
# ═══════════════════════════════════════════════════════════════════
#
# DEPRECATED（F-011）：新前端请改用 POST /v1/chat/stream（SSE）。
# 本 WS 端点仅为兼容现有前端 ws.ts 保留，协议与行为不变，后续版本将移除。
#
# 协议（与前端严格对齐）：
#   接收: {"type": "message", "data": {"content": str, "images"?: [...], "mode"?: str}}
#   发送: {"type": "token", "data": {"token": str}}               —— 逐 token
#         {"type": "meta",  "data": {"engine", "message_id", "first_token_ms"}}
#         {"type": "error", "data": {"code": int, "message": str}} —— 出错即终
#         {"type": "done",  "data": {}}                            —— 正常收尾

async def handle_dialog_stream(websocket: WebSocket, session_id: str) -> None:
    """对话流 WebSocket 处理器（真实引擎推理，main.py 挂载点调用）。

    .. deprecated:: F-011
        新前端改用 POST /v1/chat/stream（SSE）；本端点保留兼容。
    """
    await websocket.accept()
    log.info("对话流 WebSocket 已连接: session=%s", session_id)
    try:
        while True:
            msg = await websocket.receive_json()
            if not isinstance(msg, dict) or msg.get("type") != "message":
                continue  # 非业务帧（如 ping）忽略
            data = msg.get("data")
            if not isinstance(data, dict):
                data = {"content": data}
            await _ws_handle_message(websocket, session_id, data)
    except WebSocketDisconnect:
        log.info("对话流 WebSocket 断开: session=%s", session_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("对话流 WebSocket 异常: %s", exc)


async def _ws_send_error(websocket: WebSocket, code: int, message: str) -> None:
    await websocket.send_json({
        "type": "error",
        "data": {"code": code, "message": message},
    })


async def _ws_handle_message(websocket: WebSocket, sid: str, data: dict) -> None:
    """处理一条用户消息：校验 → 引擎就绪 → RAG → 流式推理 → 落库。"""
    content = str(data.get("content") or "").strip()
    if not content:
        await _ws_send_error(websocket, 30001, "消息不能为空")
        return
    if len(content) > DIALOG_MAX_INPUT_CHARS:
        await _ws_send_error(
            websocket, 40002,
            f"输入内容过长（{len(content)} 字符），"
            f"单条消息上限 {DIALOG_MAX_INPUT_CHARS} 字符，请缩短后重试")
        return

    images = _decode_images(data.get("images"))
    engine = get_dialog_engine()

    # 功能互斥锁（规格 §6.1）：被占用时如实返回 40007
    try:
        lock = await acquire_or_raise("dialog", task_id=sid)
    except ApiError as exc:
        await _ws_send_error(websocket, exc.code, exc.message)
        return

    try:
        # 审计 R1-04：同 dialog_send，ensure_loaded 阻塞调用放线程池
        if not engine.is_ready and not await asyncio.to_thread(
                engine.ensure_loaded, None):
            status = engine.get_status()
            code = 30004 if status["state"] == "error" else 30003
            await _ws_send_error(
                websocket, code,
                status["last_error"] or "对话模型未就绪，请稍后再试")
            return

        # RAG 注入 + 上下文组装
        knowledge_text, _refs = _rag_enhance(content)
        history = _load_history(sid)
        messages = engine.build_context(
            content, history=history, knowledge_text=knowledge_text,
            system_prompt=DEFAULT_SYSTEM_PROMPT, images=images or None,
            max_tokens=8192,
        )

        _ensure_session(sid, content, engine.model_name)
        _save_message(sid, "user", content,
                      attachments=data.get("images"))

        # 流式推理：同步生成器放线程执行，token 经队列回事件循环转发
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        collected: list[str] = []
        error_holder: list[str] = []

        def _produce():
            try:
                for token in engine.chat_stream(
                        messages, images=images or None,
                        stop_check=lambda: sid in _stop_flags):
                    collected.append(token)
                    loop.call_soon_threadsafe(
                        queue.put_nowait, ("token", token))
            except Exception as exc:  # noqa: BLE001
                log.exception("WS 对话流式推理失败")
                error_holder.append(str(exc))
                loop.call_soon_threadsafe(
                    queue.put_nowait, ("error", str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        producer = loop.run_in_executor(None, _produce)
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "token":
                    await websocket.send_json(
                        {"type": "token", "data": {"token": payload}})
                elif kind == "error":
                    await _ws_send_error(websocket, 30004, payload)
                elif kind == "done":
                    break
            await producer
        finally:
            _stop_flags.discard(sid)

        reply = "".join(collected)
        # 被动补全（R2-B01）：首轮回复含不确定性标记时，快速搜索重推理
        # 一次，改进回复作为追加 token 推送后再落库（meta 如实标注）
        passive = None
        if reply and not error_holder:
            new_reply, passive = await _maybe_passive_completion(
                engine, content, reply, history, knowledge_text, sid,
                images, 0.7, 1024, 8192)
            if passive and passive.get("supplemented") \
                    and new_reply.strip() and new_reply != reply:
                await websocket.send_json(
                    {"type": "token",
                     "data": {"token": "\n\n" + new_reply}})
                reply = new_reply
        saved = (_save_message(sid, "assistant", reply,
                              model_used=engine.model_name) if reply else None)
        if not error_holder:
            meta: dict = {
                "engine": engine.model_name,
                "message_id": saved["id"] if saved else "",
                "first_token_ms": round(engine.last_first_token_ms, 1),
            }
            if passive:
                meta["passive_completion"] = passive
            await websocket.send_json({"type": "meta", "data": meta})
        await websocket.send_json({"type": "done", "data": {}})
    finally:
        await lock.release("dialog")
