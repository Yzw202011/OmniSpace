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
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import (
    APIRouter,
    Body,
    File,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse

from ..config import DIALOG_MAX_INPUT_CHARS
from ..data.crypto import decrypt_text, encrypt_text
from ..data.database import get_db_safe, parse_json
from ..data.models import SessionBatchDelete
from ..middleware.error_handler import ApiError, ok
from ..middleware.feature_lock import FeatureLockManager, acquire_or_raise
from ..services.inference.dialog_engine import (
    DEFAULT_SYSTEM_PROMPT,
    THINKING_SYSTEM_SUFFIX,
    DialogEngine,
    get_dialog_engine,
    strip_think_tags,
)
from ..services.offload import run_blocking, sync_core

if TYPE_CHECKING:
    from ..services.flow_trace import Flow

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
        # 深度思考过程（2026-08-22）：同链路加密/解密，空值不产生密文
        "reasoning": decrypt_text(row.get("reasoning", "") or "")
        if row.get("reasoning") else "",
        "attachments": parse_json(row.get("attachments"), None),
        "images": parse_json(row.get("attachments"), None) or [],
        "model_used": row.get("model_used", ""),
        "engine": row.get("model_used", ""),
        "rating": int(row.get("rating", 0) or 0),
        "favorite": bool(row.get("favorite", 0)),
        "created_at": row.get("timestamp", 0),
        "timestamp": row.get("timestamp", 0),
    }


def _sse(payload: Any) -> str:
    """格式化一条 SSE 事件。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── 多模态图片解码 ──────────────────────────────────────────────────

# 上传图片最长边上限（Qwen-VL 官方推荐 1280；超过则等比压缩——
# 视觉 token 随面积线性增长，大图是 prefill 超时的首要诱因）
_MAX_IMAGE_EDGE = 1280

def _decode_images(images: list | None) -> list:
    """把 base64 图片列表解码为 PIL.Image；坏数据跳过。

    大图统一等比缩放（2026-08-21 prefill 超时事故）：Qwen-VL 视觉编码
    按 patch 计费，一张 2560×1440 截图产生数千视觉 token，prefill 实测
    可慢至 12.5 tok/s → 首字延迟超 180s 读超时（前端「生成失败」）。
    最长边压到 1280（Qwen-VL 官方推荐上限）后视觉 token 数量级下降，
    对话理解质量不受影响。
    """
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
            img = img.convert("RGB")
            if max(img.size) > _MAX_IMAGE_EDGE:
                img.thumbnail((_MAX_IMAGE_EDGE, _MAX_IMAGE_EDGE),
                              Image.LANCZOS)
                log.info("图片过大已等比压缩: -> %dx%d", *img.size)
            out.append(img)
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

# 对话排队等待上限（2026-09-08 落地）：绘画/视频/训练持锁时对话
# 消息排队等锁（覆盖单镜关键帧 ~100s 与多数绘画/视频批次；超时
# 如实报错建议稍后再试）
_DIALOG_QUEUE_WAIT_S = 300.0

# 排队位次登记表（A6 决策#8 位次广播，2026-09-12）：sid → 进入排队
# monotonic。位次 = 1 + 更早等待者数（信息性；锁释放由 asyncio 竞争决定）。
_DIALOG_LOCK_WAITERS: dict[str, float] = {}
_QUEUEABLE_BLOCKERS = ("paint", "video_gen", "training")
_BLOCKER_ZH = {"paint": "AI绘画", "video_gen": "视频生成", "training": "训练"}


def _dialog_queue_position(sid: str) -> int:
    mine = _DIALOG_LOCK_WAITERS.get(sid)
    if mine is None:
        return 1
    return 1 + sum(1 for t in _DIALOG_LOCK_WAITERS.values() if t < mine)


def _dialog_queue_enter(sid: str) -> int:
    now = time.monotonic()
    # Windows monotonic 粒度 ~15.6ms：两位用户同 tick 进队会拿到相等
    # 时间戳，位次判据 t < mine（严格小于）随即并列——FIFO 语义破缺
    # （批0 2026-09-12：强制严格递增，并列竞态根修，测试随之定稳）
    if _DIALOG_LOCK_WAITERS:
        now = max(now, max(_DIALOG_LOCK_WAITERS.values()) + 1e-6)
    _DIALOG_LOCK_WAITERS[sid] = now
    return _dialog_queue_position(sid)


def _dialog_queue_exit(sid: str) -> None:
    _DIALOG_LOCK_WAITERS.pop(sid, None)

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
        from ..services.learning_scheduler import TRIGGER_DIALOG_GAP, get_learning_scheduler
        get_learning_scheduler().fire_trigger(TRIGGER_DIALOG_GAP, {
            "session_id": sid,
            "message": message[:200],
            "keywords": keywords,
        })
    except Exception as exc:  # noqa: BLE001 - 触发器失败不影响对话主链
        log.debug("dialog_gap 触发器触发失败（忽略）: %s", exc)


async def _web_search_augment(message: str,
                              knowledge_text: str) -> tuple[str, list[dict]]:
    """联网搜索 v1 编排（架构升级计划 B-阶段一，默认关；免费双通道）。

    enabled 且意图判定命中（时效词/显式"搜索:"前缀）→ 线程池执行
    级联搜索（主力 Provider 失败回退浏览器兜底）→【联网资料】块
    追加到 knowledge_text。任何失败静默返回原值——搜索是增益能力，
    绝不阻断对话主链。

    Returns:
        (新 knowledge_text, web_refs 字典列表——供来源卡片透出)。
    """
    try:
        from ..services.web_search import (
            build_web_block,
            detect_web_needed,
            get_web_search_settings,
            run_web_search,
        )
        cfg = get_web_search_settings()
        if not cfg.get("enabled") or not detect_web_needed(
                message, str(cfg.get("trigger", "auto"))):
            return knowledge_text, []
        results = await run_blocking(run_web_search, message, cfg)
        if not results:
            return knowledge_text, []
        block = build_web_block(results)
        log.info("联网搜索命中 %d 条（注入资料块 %d 字）",
                 len(results), len(block))
        merged = (knowledge_text + "\n\n" + block) if knowledge_text \
            else block
        return merged, [r.to_dict() for r in results]
    except Exception as exc:  # noqa: BLE001 - 搜索失败不阻断对话主链
        log.warning("联网搜索编排失败（跳过）: %s", exc)
        return knowledge_text, []


@sync_core
def _quick_search_supplement(keywords: list[str]) -> str:
    """快速搜索 1~3 页并截取正文（P1-06：自调度 async，直接 await）。

    复用浏览器池（headless Chromium）：导航搜索页 → 提取结果链接 →
    依序读内容页正文。离线/浏览器不可用/超时均返回 ""。
    全程不填写任何表单（仅直接导航搜索 URL，TC-S-012 约束）。
    """
    if not keywords:
        return ""
    try:
        from ..services.browser_agent_service import _is_content_link, build_search_url
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
            log.debug("_quick_search_supplement: 降级忽略", exc_info=True)
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
            log.debug("_quick_search_supplement: 降级忽略", exc_info=True)

    combined = "\n\n".join(t.strip() for t in texts if t.strip())
    combined = re.sub(r"[ \t]+", " ", combined)
    return combined[:_PASSIVE_TOTAL_CHARS]


@sync_core
def _passive_reinfer(engine: DialogEngine, message: str, history: list[dict],
                     knowledge_text: str, supplement: str,
                     images: list, temperature: float,
                     max_new_tokens: int, max_ctx: int) -> str:
    """补充上下文后重推理一次（P1-06：自调度 async，直接 await）。"""
    augmented = (knowledge_text or "")
    augmented += ("\n\n[联网补充资料]\n" + supplement) if supplement else ""
    messages = engine.build_context(
        message, history=history, knowledge_text=augmented,
        system_prompt=DEFAULT_SYSTEM_PROMPT, images=images or None,
        max_tokens=max_ctx,
    )
    return engine.chat(messages, images or None, temperature, max_new_tokens)


async def _maybe_passive_completion(
        engine: DialogEngine, message: str, reply: str, history: list[dict],
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
        supplement = await _quick_search_supplement(keywords)
    except Exception as exc:  # noqa: BLE001
        log.debug("被动补全搜索失败（回退原回复）: %s", exc)
    if not supplement.strip():
        return reply, {"triggered": True, "keywords": keywords,
                       "supplemented": False}
    try:
        new_reply = await _passive_reinfer(
            engine, message, history, knowledge_text,
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
                  model_used: str = "", attachments: Any = None,
                  reasoning: str = "",
                  rag_refs: list | None = None) -> dict:
    msg = {
        "id": uuid.uuid4().hex,
        "session_id": sid,
        "role": role,
        "content": content,
        "attachments": attachments,
        "model_used": model_used,
        "reasoning": reasoning,
        "rag_refs": json.dumps(rag_refs or [], ensure_ascii=False),
        "timestamp": _now(),
    }
    db = get_db_safe()
    if db is not None:
        try:
            # 要求#36：落库前加密 content/reasoning；返回值保持明文供调用方使用
            stored = dict(msg)
            stored["content"] = encrypt_text(content)
            if reasoning:
                stored["reasoning"] = encrypt_text(reasoning)
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

@router.get("/dialog/models")
def dialog_list_models() -> dict[str, Any]:
    """对话可用模型清单（2026-08-20：模型选择 + 档位选择前端数据源）。

    返回每个本地就绪模型：model_id / 显示名 / 参数量档位 / 预估显存 /
    本机物理显存可承载判定（不可承载前端置灰）/ 是否当前已加载。
    """
    engine = get_dialog_engine()
    items: list[dict] = []
    try:
        from ..services.inference.dialog_engine import (
            _cuda_total_gb,
            _effective_candidates,
            _estimated_load_gb,
            _resolve_candidate_dir,
            discover_dialog_models,
            imported_dialog_models,
        )
        total_vram = _cuda_total_gb()
        # 候选表 + 动态发现合并（保序去重）。物理装不下的候选（如 16GB
        # 卡上的 8b bf16 16.3GB）也要展示——前端置灰标注"超本机显存"，
        # 让用户知道该档位存在而非凭空消失
        entries: list[tuple[str, str]] = [
            (mid, rel) for mid, rel, _v in _effective_candidates()]
        from ..services.inference.dialog_engine import (
            _HIGH_TIER_DIALOG_CANDIDATE as _HI_CAND,
        )
        from ..services.inference.dialog_engine import (
            DIALOG_MODEL_CANDIDATES as _BASE_CANDS,
        )
        extra = [(_HI_CAND[0], _HI_CAND[1])] + [c[:2] for c in _BASE_CANDS]
        for mid, rel in extra:
            if mid not in [m for m, _r in entries]:
                entries.append((mid, rel))
        for mid in discover_dialog_models():
            if mid not in [m for m, _r in entries]:
                entries.append((mid, mid))
        for mid, rel in entries:
            path = _resolve_candidate_dir(rel)
            if path is None:
                continue  # 磁盘不存在不入清单
            # 语音合成等非对话模型不入清单（动态发现误收，如 qwen3-tts）
            if any(k in mid.lower() for k in ("tts", "voice", "speech",
                                              "asr", "audio")):
                continue
            est = _estimated_load_gb(path, "vl")
            fits = total_vram <= 0 or est <= total_vram * 0.98
            items.append({
                "model_id": mid,
                "name": _dialog_model_display_name(mid),
                "size_label": _dialog_model_size_label(mid),
                "est_vram_gb": round(est, 1),
                "fits_local": fits,
                "loaded": engine.is_ready and engine.model_name == mid,
                "vision": _dialog_model_supports_vision(mid),
            })
        # 用户导入的外部路径对话模型（登记表兜底，2026-09-01 完整接入）：
        # 类别已过 dialog/language/omni 闸门，不重复套 tts/voice 关键词过滤
        listed_ids = {m["model_id"] for m in items}
        for mid, info in imported_dialog_models().items():
            if mid in listed_ids:
                continue
            est = _estimated_load_gb(Path(info["path"]), info["backend"])
            fits = total_vram <= 0 or est <= total_vram * 0.98
            items.append({
                "model_id": mid,
                "name": _dialog_model_display_name(mid),
                "size_label": _dialog_model_size_label(mid),
                "est_vram_gb": round(est, 1),
                "fits_local": fits,
                "loaded": engine.is_ready and engine.model_name == mid,
                "vision": _dialog_model_supports_vision(
                    mid, backend=info.get("backend", "")),
            })
        items.sort(key=lambda x: (not x["fits_local"],))
        # 模块级选型配置（模型管理 → 功能模块模型配置）：
        # 白名单过滤 + 默认模型下发。allowed 为空 = 不限制（兼容存量）。
        try:
            from ..api.models import get_module_model_scope
            allowed, default_model = get_module_model_scope("dialog")
            if allowed is not None:
                items = [m for m in items if m["model_id"] in allowed]
        except Exception as exc:  # noqa: BLE001 - 配置读取失败不阻断清单
            log.warning("模块白名单过滤跳过: %s", exc)
            default_model = ""
        return ok({"models": items, "total_vram_gb": round(total_vram, 1),
                   "default_model": default_model or ""})
    except Exception as exc:  # noqa: BLE001 - 清单失败不阻断对话主流程
        log.warning("对话模型清单构建失败: %s", exc)
        return ok({"models": [], "total_vram_gb": 0})


def _dialog_model_display_name(mid: str) -> str:
    """模型 id → 中文显示名（档位描述对齐规格 §6.2.1）。"""
    m = mid.lower()
    if "qwen35-9b" in m:
        return "Qwen3.5 9B · 旗舰"
    if "qwen3-vl-8b" in m:
        return "Qwen3-VL 8B · 旗舰"
    if "qwen3-vl-4b" in m:
        return "Qwen3-VL 4B · 均衡"
    if "qwen3-vl-2b" in m:
        return "Qwen3-VL 2B · 轻量"
    if "qwen2-vl-2b" in m:
        return "Qwen2-VL 2B · 轻量"
    if "qwen3-32b" in m:
        return "Qwen3 32B · 文本旗舰"
    return mid


def _dialog_model_size_label(mid: str) -> str:
    """模型 id → 参数量档位标签（8B/4B/2B/…）。"""
    import re as _re
    m = _re.search(r"(\d+(?:\.\d+)?)\s*b\b", mid.lower())
    return f"{m.group(1).upper()}B" if m else ""


def _dialog_model_supports_vision(mid: str, backend: str = "") -> bool:
    """模型是否支持图片理解（多模态）。

    2026-09-07 按模型能力开放对话附件：纯文本模型（如 qwen35-9b）
    前端彻底隐藏图片上传；判定源 = id 含 vl 系列（qwen3-vl/qwen2-vl）
    或导入模型 backend 为 vl（多模态 transformers 路径）。
    """
    m = (mid or "").lower()
    if "vl" in m:  # qwen3-vl-* / qwen2-vl-* / *-vl-*
        return True
    return (backend or "").lower() == "vl"


# 对话文档附件解析上限：20MB（对话场景小于知识库导入 50MB）；
# 解析文本截断 200k 字符（防超长文档撑爆 token 窗口，前端拼装时
# 还有 DIALOG_MAX_INPUT_CHARS 单条钳制兜底）
_DIALOG_DOC_MAX_BYTES = 20 * 1024 * 1024
_DIALOG_DOC_MAX_CHARS = 200_000


@router.post("/dialog/parse-document")
async def dialog_parse_document(
        file: UploadFile = File(...)) -> dict[str, Any]:
    """对话文档附件解析（2026-09-07 按模型能力开放附件）。

    txt/md → 多编码直读；docx → python-docx；pdf → pymupdf。
    解析复用知识库导入的同一条管线（knowledge._parse_document，
    含魔数嗅验），.doc 老格式无解析库支撑 → 诚实拒绝并建议转存
    docx（静默乱码比明确报错更伤用户）。

    Returns:
        {name, text, chars, kind}
    """
    if not file or not file.filename:
        raise ApiError("SYSTEM_PARAM_INVALID", "未提供上传文件")
    ext = ("." + file.filename.rsplit(".", 1)[-1].lower()) \
        if "." in file.filename else ""
    if ext == ".doc":
        raise ApiError(
            "UNSUPPORTED_FORMAT",
            "暂不支持 .doc 老格式（Word 97-2003）",
            detail={"filename": file.filename},
            suggestion="请在 Word/WPS 中另存为 .docx 后再上传")
    data = await file.read(_DIALOG_DOC_MAX_BYTES + 1)
    if not data:
        raise ApiError("SYSTEM_PARAM_INVALID", "上传文件内容为空")
    if len(data) > _DIALOG_DOC_MAX_BYTES:
        raise ApiError(
            "OPERATION_LIMIT_EXCEEDED", "文档超过 20MB 上限",
            detail={"size_bytes": len(data),
                    "limit_bytes": _DIALOG_DOC_MAX_BYTES},
            suggestion="请拆分文档后分批上传")
    # 复用知识库导入的解析管线与上传守卫（白名单+魔数嗅验）
    from .knowledge import _parse_document, upload_guard
    try:
        upload_guard.validate(file.filename, data,
                              upload_guard.DOCUMENT_TABLE)
    except upload_guard.UploadRejected as exc:
        raise ApiError("UNSUPPORTED_FORMAT", str(exc),
                       detail={"filename": file.filename}) from exc
    try:
        text = await run_blocking(_parse_document, file.filename, data)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ApiError("SYSTEM_INTERNAL_ERROR", "文档解析失败",
                       detail={"filename": file.filename,
                               "error": str(exc)}) from exc
    if not text.strip():
        raise ApiError("SYSTEM_PARAM_INVALID",
                       "文档中未提取到可用文本",
                       detail={"filename": file.filename})
    truncated = len(text) > _DIALOG_DOC_MAX_CHARS
    if truncated:
        text = text[:_DIALOG_DOC_MAX_CHARS]
    return ok({"name": file.filename, "text": text, "chars": len(text),
               "kind": ext.lstrip("."), "truncated": truncated},
              message="文档解析成功")


@router.post("/dialog/send")
@router.post("/chat/send")
async def dialog_send(request: Request,
                      body: dict = Body(default_factory=dict)) -> Any:
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
    thinking = bool(body.get("thinking", False))
    try:
        temperature = float(body.get("temperature", 0.7))
    except (TypeError, ValueError):
        temperature = 0.7
    try:
        max_new_tokens = int(body.get("max_new_tokens", 1024))
    except (TypeError, ValueError):
        max_new_tokens = 1024
    if thinking:
        max_new_tokens = max(max_new_tokens, 2048)  # 思考通道独立预算

    images = _decode_images(body.get("images") or body.get("attachments"))

    engine = get_dialog_engine()
    lock = None
    try:
        lock = await acquire_or_raise("dialog", task_id=sid)
    except ApiError as exc:
        # A6 对话排队（决策#8=A）：主路径（SSE/REST）与 WS 路径同款
        # 有界排队——被绘画/视频/训练持锁时不再秒拒。排队阶段发生在
        # SSE 响应头之前，客户端表现为连接等待（前端 loading 占位）。
        from ..middleware.feature_lock import get_feature_lock
        blocker = get_feature_lock().active_feature
        if exc.code not in (40007, "FEATURE_MUTEX_LOCKED") \
                or blocker not in _QUEUEABLE_BLOCKERS:
            raise  # 热保护/未知互斥方：秒拒（等待无意义）
        blocker_zh = _BLOCKER_ZH.get(blocker, "其他AI功能")
        pos = _dialog_queue_enter(sid)
        log.info("对话进入排队等待：blocker=%s sid=%s 位次=%s",
                 blocker, sid, pos)
        _deadline = time.monotonic() + _DIALOG_QUEUE_WAIT_S
        try:
            while True:
                if request is not None:
                    try:
                        if await request.is_disconnected():
                            raise ApiError("FRONTEND_REQUEST_ABORTED",
                                           "请求已取消（页面关闭或停止）")
                    except Exception:  # noqa: BLE001 - 探测失败不放弃排队
                        log.debug("dialog_send: 降级忽略", exc_info=True)
                try:
                    lock = await acquire_or_raise("dialog", task_id=sid)
                    break
                except ApiError:
                    if time.monotonic() >= _deadline:
                        raise ApiError(
                            "FEATURE_MUTEX_LOCKED",
                            f"排队超时（{_DIALOG_QUEUE_WAIT_S:.0f} 秒）："
                            f"{blocker_zh} 仍在进行中",
                            suggestion=f"等 {blocker_zh} 结束后再发消息，"
                                       "或先停止它") from None
                    await asyncio.sleep(2.0)
        finally:
            _dialog_queue_exit(sid)
    lock_handed_off = False  # 流式路径下锁移交给 SSE 生成器
    # 执行流程追踪（2026-08-23）：触发=用户发送消息，节点链
    # 模型加载→上下文组装→流式/一次性生成→消息落库
    from ..services.flow_trace import start_flow
    flow = start_flow(
        "dialog", "chat",
        f"AI 对话：{message[:20]}{'…' if len(message) > 20 else ''}",
        trigger="用户发送消息",
        input_summary=f"{len(message)} 字"
                      + (f" +{len(images)} 图" if images else "")
                      + (" · 深度思考" if thinking else "")
                      + (" · 流式" if stream else ""),
        detail=f"session={sid} model={model_req or 'auto'}")
    try:
        # 新一轮发送开始时丢弃陈旧停止标记：/chat/stop 在空闲会话上
        # 置标记后无活跃流触发 finally 清理，若不清除会使下一次流式
        # 生成被立即停止（0 token）。停止进行中的流不受影响——标记在
        # 本次 send 之后设置，生成循环正常感知。
        _stop_flags.discard(sid)
        # 引擎未加载/目标变化时尝试装载；失败 → 30xxx 段友好错误。
        # 审计 R1-04：ensure_loaded 为 15s 级阻塞调用，经 run_blocking
        # 卸载执行，避免卡住事件循环。
        # 云端解绑兜底（实弹 11:14 定位）：引擎 ready 时 dialog 曾直接
        # 跳过 ensure_loaded——解绑 remote 后旧云端继续服役。ensure_loaded
        # 内部对「ready 且目标匹配」是零成本直返，这里无条件过闸最安全。
        if True:
            with flow.node("模型加载", input_summary=f"model={model_req or 'auto'}",
                           friendly="加载对话模型") as n:
                if not await run_blocking(engine.ensure_loaded, model_req):
                    # 装载已触发但 15s 内未完成（看门狗空闲卸载/重启后的
                    # 首条消息场景）——铁律：排队等装载完自动续跑，绝不
                    # 弄丢消息。有界轮询（≤5 分钟）至就绪；仅 error/
                    # unavailable/超时才诚实报错（带出路）。
                    deadline = time.monotonic() + 300.0
                    st = engine.get_status()
                    while not engine.is_ready:
                        if st["state"] in ("error", "unavailable"):
                            raise ApiError(
                                30004,
                                st["last_error"] or "对话模型加载失败，请重试",
                                detail={"engine": st})
                        if time.monotonic() >= deadline:
                            raise ApiError(
                                30003,
                                "对话模型装载超时（已等待 5 分钟），请稍后重试；"
                                "持续失败请到「模型管理」页查看",
                                detail={"engine": st})
                        await asyncio.sleep(2)
                        st = engine.get_status()
                n.output(f"模型就绪: {engine.model_name}")

        # 节点：上下文组装（RAG 检索 + 历史 + build_context）
        with flow.node("上下文组装", friendly="检索知识库并组装上下文") as n:
            # RAG 注入
            knowledge_text, refs = _rag_enhance(message)
            # 联网搜索 v1（默认关；意图判定命中才搜，资料块并入上下文）
            knowledge_text, web_refs = await _web_search_augment(
                message, knowledge_text)
            # 组装上下文（系统 Prompt + 注入 + 历史 + 当前输入）；
            # 深度思考模式追加四步框架引导（THINKING_SYSTEM_SUFFIX）。
            # 2026-09-10 用户报「你好→一大段无关」根修：vLLM 后端
            # （qwen3 reasoning parser）是原生思考模型——<think> 块由
            # vLLM 剥离进 reasoning 通道，若再叠加「展示四步思考」指令
            # 会双思考打架（模型把思考当正文二次输出+裸写 </think> 标签
            # 泄漏进答案）。原生思考后端不再注入可见思考框架。
            _backend_name = getattr(engine, "_backend_name", "") or ""
            if thinking and _backend_name not in ("vllm", "gguf", "llama"):
                sys_prompt = DEFAULT_SYSTEM_PROMPT + THINKING_SYSTEM_SUFFIX
            else:
                sys_prompt = DEFAULT_SYSTEM_PROMPT
            history = _load_history(sid)
            max_ctx = min(int(body.get("context_length", 8192) or 8192), 8192)
            messages = engine.build_context(
                message, history=history, knowledge_text=knowledge_text,
                system_prompt=sys_prompt, images=images or None,
                max_tokens=max_ctx,
            )
            n.output(f"历史 {len(history)} 条"
                     + (f"，知识库引用 {len(refs)} 条" if refs else ""))

        _ensure_session(sid, message, model_req or engine.model_name)
        _save_message(sid, "user", message,
                      attachments=body.get("attachments"))

        if stream:
            resp = await _stream_response(
                engine, lock, sid, message, history, knowledge_text,
                messages, images, refs,
                temperature, max_new_tokens, max_ctx, thinking, flow,
                web_refs=web_refs, request=request)
            lock_handed_off = True  # flow 同样移交给 SSE 生成器收尾
            return resp

        # 非流式：节点=一次性生成
        with flow.node("生成回复", input_summary=f"max_tokens={max_new_tokens}",
                       friendly="AI 生成回复") as n:
            try:
                reply = await run_blocking(
                    engine.chat, messages, images or None,
                    temperature, max_new_tokens)
            except RuntimeError as exc:
                raise ApiError(30004, str(exc) or "对话推理失败") from exc
            n.output(f"首字 {engine.last_first_token_ms:.0f}ms "
                     f"总 {engine.last_total_ms:.0f}ms")
        # 深度思考：整段产出切分 reasoning/content（与流式同语义）
        reply_reasoning = ""
        if thinking and "</think>" in reply:
            parts = reply.split("</think>", 1)
            reply_reasoning = parts[0].replace("<think>", "", 1).strip()
            reply = parts[1]
        # 被动补全（文档B §7.1.6.1 步骤4，R2-B01）：
        # 不确定性回复 → 快速搜索 1~3 页 → 补充上下文重推理一次
        # （检测只作用于 content 段；重推理走非思考 prompt）
        reply, passive = await _maybe_passive_completion(
            engine, message, reply, history, knowledge_text, sid,
            images, temperature, max_new_tokens, max_ctx)
        with flow.node("消息落库", friendly="保存对话记录") as n:
            msg = _save_message(sid, "assistant", reply,
                                model_used=engine.model_name,
                                reasoning=reply_reasoning,
                                rag_refs=refs)
            n.output(f"回复 {len(reply)} 字")
        flow.end("success",
                 output_summary=f"回复 {len(reply)} 字"
                                f" 首字 {engine.last_first_token_ms:.0f}ms")
        return ok({
            "session_id": sid,
            "message": msg,
            "knowledge_refs": refs,
            "web_refs": web_refs,
            "passive_completion": passive,
            "first_token_ms": round(engine.last_first_token_ms, 1),
        })
    except ApiError as exc:
        flow.end("error", error_code=str(exc.code),
                 error_detail=str(getattr(exc, "message", exc))[:300])
        raise
    except Exception as exc:  # noqa: BLE001
        flow.end("error", error_detail=str(exc)[:300])
        raise
    finally:
        # 流式路径下锁由 SSE 生成器持有至流结束；其余路径在此释放
        if not lock_handed_off:
            await lock.release("dialog")


@router.post("/chat/stream")
async def chat_stream(request: Request,
                      body: dict = Body(default_factory=dict)) -> Any:
    """SSE 流式对话（F-011 / 文档 §7.1.4 /v1/chat → /stream）。

    等价于 POST /dialog/send 且强制 stream=true：
    Content-Type: text/event-stream
    事件序列：data: {"session_id"} → data: {"knowledge_refs"}? →
              data: {"token": str}* → data: {"meta": {...}} → data: [DONE]
    错误事件：data: {"error": str, "code": int}（随后 done 收尾）。
    中断：POST /chat/stop {"session_id"} 置停止标记。
    """
    forced = {**(body or {}), "stream": True}
    return await dialog_send(body=forced, request=request)


async def _stream_response(engine: DialogEngine, lock: FeatureLockManager,
                           sid: str, message: str,
                           history: list, knowledge_text: str,
                           messages: list,
                           images: list, refs: list,
                           temperature: float, max_new_tokens: int,
                           max_ctx: int, thinking: bool = False,
                           flow: Flow | None = None,
                           web_refs: list | None = None,
                           request: Request | None = None) -> StreamingResponse:
    """构造 SSE 流式响应；生成结束后落库并释放功能锁。

    深度思考模式（2026-08-22）：思考段以 {"reasoning": str} 事件推送
    （与 {"token": str} 正文事件分离，前端按通道渲染）。
    执行流程追踪（2026-08-23）：flow 由 dialog_send 移交，token 流
    节点心跳续命，流结束（成功/错误/客户端断开）统一收尾。
    """
    from ..services.flow_trace import NULL_FLOW
    flow = flow or NULL_FLOW

    async def event_gen() -> AsyncGenerator[str, None]:
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        collected: list[str] = []
        reasoning_parts: list[str] = []
        error_holder: list[str] = []

        def _produce() -> None:
            # 推理节点：executor 线程内执行，token 片段作追踪心跳
            with flow.node(
                    "流式生成",
                    input_summary=f"max_tokens={max_new_tokens}"
                                  + (" · 深度思考" if thinking else ""),
                    friendly="AI 正在流式生成回复") as gen_node:
                produced = 0
                try:
                    for event in engine.chat_stream_ex(
                            messages, images=images or None,
                            temperature=temperature,
                            max_new_tokens=max_new_tokens,
                            enable_thinking=thinking,
                            stop_check=lambda: sid in _stop_flags):
                        kind = event["type"]
                        text = event["text"]
                        if not text:
                            continue
                        produced += 1
                        if produced % 16 == 0:  # 心跳节流：16 片段一次
                            gen_node.progress(f"已生成 {produced} 片段")
                        if kind == "reasoning":
                            reasoning_parts.append(text)
                            loop.call_soon_threadsafe(
                                queue.put_nowait, ("reasoning", text))
                        else:
                            collected.append(text)
                            loop.call_soon_threadsafe(
                                queue.put_nowait, ("token", text))
                    gen_node.output(f"{produced} 片段 "
                                    f"首字 {engine.last_first_token_ms:.0f}ms")
                except Exception as exc:  # noqa: BLE001 - 汇聚为错误事件
                    log.exception("对话流式推理失败")
                    error_holder.append(str(exc))
                    loop.call_soon_threadsafe(
                        queue.put_nowait, ("error", str(exc)))
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        producer = loop.run_in_executor(None, _produce)

        # 客户端断连监视（UAT 2026-09-10 P1-18）：断开即入队哨兵事件，
        # 消费循环借此停止推理线程（经既有 stop_check 旗标），不再为
        # 已离开的用户烧完剩余 token（此前断连后生成照跑至自然结束）
        client_gone = False
        watcher: asyncio.Task | None = None
        if request is not None:
            async def _watch_disconnect() -> None:
                log.info("[P1-18] 断连监视器启动: %s", sid)
                while True:
                    try:
                        gone = await request.is_disconnected()
                    except Exception as exc:  # noqa: BLE001 - 探测异常退出
                        log.warning("[P1-18] 断连探测异常 %s: %s", sid, exc)
                        return
                    if gone:
                        log.warning("[P1-18] 断连确认，停止推理: %s", sid)
                        loop.call_soon_threadsafe(
                            queue.put_nowait, ("_client_gone", None))
                        return
                    await asyncio.sleep(1.0)
            watcher = asyncio.create_task(_watch_disconnect())
            log.debug("[P1-18] 监视任务已建: %s", sid)

        try:
            yield _sse({"session_id": sid})
            if refs:
                yield _sse({"knowledge_refs": refs})
            if web_refs:
                yield _sse({"web_refs": web_refs})
            while True:
                kind, payload = await queue.get()
                if kind == "token":
                    yield _sse({"token": payload})
                elif kind == "reasoning":
                    yield _sse({"reasoning": payload})
                elif kind == "_client_gone":
                    # P1-18：断连即停推理（经既有停止旗标→stop_check），
                    # 部分回复照常落库（finally 段），不再继续烧 GPU
                    client_gone = True
                    _stop_flags.add(sid)
                    log.warning("SSE 客户端断开，停止生成本会话剩余回复: %s", sid)
                    break
                elif kind == "error":
                    yield _sse({"error": payload, "code": 30004})
                elif kind == "done":
                    break
            await producer
            # 被动补全（R2-B01）：首轮回复含不确定性标记时，快速搜索
            # 重推理一次，改进回复作为追加 token 继续推送（[DONE] 之前）。
            # 客户端断开（P1-18）则跳过——人已走，不再烧推理
            if collected and not error_holder and not client_gone:
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
            if watcher is not None:
                watcher.cancel()
            _stop_flags.discard(sid)
            # 审计 P2-2（2026-09-12）：先放锁后落库——落库原在放锁前，
            # _save_message 一旦异常/阻塞会把对话功能锁卡死到重启；
            # 落库异常降级为日志，不阻断放锁
            await lock.release("dialog")
            reply = "".join(collected)
            reasoning_full = "".join(reasoning_parts)
            # 节点：消息落库
            with flow.node("消息落库", friendly="保存对话记录") as n:
                try:
                    if reply or reasoning_full:
                        # M-4 兜底：异常路径残留标签剥离，防污染后续轮上下文
                        _save_message(sid, "assistant", strip_think_tags(reply),
                                      model_used=engine.model_name,
                                      reasoning=reasoning_full,
                                      rag_refs=refs)
                        n.output(f"回复 {len(reply)} 字"
                                 + (f"，思考 {len(reasoning_full)} 字"
                                    if reasoning_full else ""))
                        n.output(f"回复 {len(reply)} 字"
                                 + (f"，思考 {len(reasoning_full)} 字"
                                    if reasoning_full else ""))
                except Exception as exc:  # noqa: BLE001 - 落库失败不阻断收尾
                    n.output(f"落库失败（已放锁）：{exc}")
                    log.error("对话回复落库失败 sid=%s: %s", sid, exc)
            # 流程收尾：错误事件优先；有产出（含用户停止后部分产出）算成功
            if error_holder:
                flow.end("error", error_code="STREAM_FAILED",
                         error_detail=error_holder[0][:300])
            else:
                flow.end("success",
                         output_summary=f"回复 {len(reply)} 字"
                                        + (f"，思考 {len(reasoning_full)} 字"
                                           if reasoning_full else ""))
        if not error_holder:
            meta = {
                "model": engine.model_name,
                "first_token_ms": round(engine.last_first_token_ms, 1),
            }
            # 与 WS meta 帧同源：降级时带 degraded_from（SSE 通道对齐）
            _degraded_from = getattr(engine, "_degraded_from", "") or ""
            if _degraded_from:
                meta["degraded_from"] = _degraded_from
            if thinking:
                meta["thinking_used"] = True
                meta["first_content_ms"] = round(
                    engine.last_first_content_ms, 1)
            yield _sse({"meta": meta})
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


# ── 历史 / 会话 ─────────────────────────────────────────────────────

@router.get("/chat/history")
def chat_history(session_id: str = Query("", description="会话ID（可选）"),
                 page: int = Query(1, ge=1),
                 page_size: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
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
                    " model_used, rating, favorite, reasoning, timestamp"
                    " FROM dialog_messages WHERE session_id=? "
                    "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                    (session_id, page_size, offset))
            else:
                total = db.count("dialog_messages")
                rows = db.query(
                    "SELECT id, session_id, role, content, attachments,"
                    " model_used, rating, favorite, reasoning, timestamp"
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
def chat_clear(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
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


@router.post("/chat/sessions/batch-delete")
def chat_batch_delete_sessions(body: SessionBatchDelete) -> dict[str, Any]:
    """批量删除会话及消息（2026-08-20：单批 ≤100，前端超量分批）。

    逐 ID 复用单删清理（messages + sessions 级联，内存兜底同步），
    不存在的 ID 汇入 missing_ids 不报错（幂等，前端按结果过滤本地态）。
    """
    deleted: list[str] = []
    missing: list[str] = []
    db = get_db_safe()
    for sid in body.session_ids:
        try:
            if db is not None:
                row = db.query_one(
                    "SELECT id FROM dialog_sessions WHERE id=?", (sid,))
                if row is None:
                    if sid not in _mock_sessions:
                        missing.append(sid)
                        continue
                else:
                    db.delete("dialog_messages", "session_id=?", (sid,))
                    db.delete("dialog_sessions", "id=?", (sid,))
                    deleted.append(sid)
                    continue
            else:
                if sid not in _mock_sessions:
                    missing.append(sid)
                    continue
            _mock_sessions.pop(sid, None)
            _mock_messages.pop(sid, None)
            deleted.append(sid)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断整批
            log.warning("会话批量删除单条失败 %s: %s", sid, exc)
            missing.append(sid)
    return ok({"deleted": len(deleted), "deleted_ids": deleted,
               "missing_ids": missing})


@router.get("/dialog/status")
def dialog_status() -> dict[str, Any]:
    """对话引擎状态（模型可用性 / 显存 / 首 token 统计）。"""
    return ok(get_dialog_engine().get_status())


@router.post("/dialog/prewarm")
async def dialog_prewarm(request: Request) -> dict[str, Any]:
    """对话页预热（2026-08-22 性能优化）：后台线程加载默认模型。

    前端 DialogPage 挂载时调用——用户进入对话页到发出第一条消息之间
    的打字时间（通常 >10s）足以覆盖 4B transformers 加载（~10s），
    首条消息不再等待冷加载。fire-and-forget，立即返回不阻塞。

    model_id 透传 + inflight 共享（2026-08-23 幽灵热切换修复）：
    此前与 /models/warmup 双入口并发且目标不一致——本入口按引擎
    默认（显存紧时选 GGUF Qwen2-0.5B），warmup 按用户选择（4b/
    8b-awq），后到者把先加载的模型热切换掉（实测 4b 载好被 0.5B
    换载再换回，台账抖动 + 双倍加载耗时）。现与 /models/warmup
    共享 _warmup_inflight 去重、同样以用户选择为目标。
    """
    import threading

    try:  # body 可选（旧调用方不传）；Request.json 为异步方法须 await
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    want_model = str((body or {}).get("model_id") or "").strip() or None
    # 模块级选型配置生效（模型管理 → 功能模块模型配置）：
    # 未显式指定时预热模块默认模型（默认配置的模型提前驻留）
    if want_model is None:
        try:
            from .models import module_default_model
            want_model = module_default_model("dialog") or None
        except Exception as exc:  # noqa: BLE001 - 配置读取失败不阻断
            log.warning("对话模块默认模型读取失败（跳过）: %s", exc)

    engine = get_dialog_engine()
    status = engine.get_status()
    if status.get("state") == "ready":
        # 预热语义=「确保有模型可用」——引擎已就绪即达标，绝不为换成
        # 目标模型而热切换拆台（2026-09-09 02:39 反向互踩根修：用户刚
        # 手动装好 8B，进对话页预热默认 9B 把 8B 顶掉、9B 又装不下 →
        # 引擎变空卡死）。真要换模型走 /models/load 或发送带 model
        # （显式意图才允许热切换）。
        return ok({"state": "ready", "prewarmed": False,
                   "model": status.get("model", "")})

    # 与 /models/warmup 共享 inflight（同目标去重；inflight 中预热
    # 仍在跑时短路，避免双线程排队引擎锁引发目标错乱）
    from .models import _warmup_inflight
    if "dialog" in _warmup_inflight:
        return ok({"state": "loading", "prewarmed": False})
    _warmup_inflight.add("dialog")

    def _bg_prewarm() -> None:
        try:
            engine.ensure_loaded(want_model)
        except Exception as exc:  # noqa: BLE001 - 预热失败不抛出
            log.warning("对话模型后台预热失败: %s", exc)
        finally:
            _warmup_inflight.discard("dialog")

    threading.Thread(target=_bg_prewarm, daemon=True,
                     name="dialog-prewarm").start()
    try:  # 大白话事件：预热开始
        from ..services.event_log import log_event
        log_event("dialog", "model_prewarm",
                  "进入对话页，正在后台预热对话模型（发消息前会自动准备好）",
                  level="info")
    except Exception:  # noqa: BLE001
        log.debug("dialog_prewarm: 降级忽略", exc_info=True)
    return ok({"state": "loading", "prewarmed": True})


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
                " model_used, rating, favorite, reasoning, timestamp"
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
                       page_size: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
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
def chat_create_session(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
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
def chat_get_session(session_id: str) -> dict[str, Any]:
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
                       page_size: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    """会话消息列表（分页，时间升序）。"""
    if _get_session_row(session_id) is None:
        raise ApiError(40005, "会话不存在", detail={"session_id": session_id})
    msgs = _session_messages(session_id)
    total = len(msgs)
    start = (page - 1) * page_size
    return ok({"items": msgs[start:start + page_size], "total": total,
               "page": page, "page_size": page_size})


@router.put("/chat/sessions/{session_id}")
def chat_update_session(session_id: str, body: dict = Body(default_factory=dict)) -> dict[str, Any]:
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


def _delete_session_everywhere(session_id: str) -> dict[str, Any]:
    """删除会话及其历史消息（内部助手，B3 2026-09-13）。

    历史注记：原为 REST 端点 DELETE /dialog/sessions/{session_id}
    （前端零消费，B3 死端点普查摘除装饰器）；函数体保留——活端点
    /chat/sessions/{session_id} 的删除语义经此复用。"""
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


@router.delete("/chat/sessions/{session_id}")
def chat_delete_session(session_id: str) -> dict[str, Any]:
    """删除会话及其消息（复用 /dialog/sessions 删除语义）。"""
    return _delete_session_everywhere(session_id)


@router.delete("/chat/sessions/{session_id}/messages")
def chat_clear_messages(session_id: str) -> dict[str, Any]:
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
                      body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """消息评分（1 赞 / -1 踩 / 0 取消，DIALOG-024）。"""
    try:
        rating = int(body.get("rating", 0))
    except (TypeError, ValueError):
        rating = 0
    rating = max(-1, min(1, rating))
    result = _update_message_flag(session_id, message_id, "rating", rating)
    # RAG 反馈闭环（知识学习升级批5）：点踩 → 本次生成引用的知识条目
    # 质量分 -0.8（负分即不再注入）；点赞 → +0.2 小幅回升。失败不影响评分。
    if rating != 0:
        try:
            db = get_db_safe()
            if db is not None:
                row = db.query_one(
                    "SELECT rag_refs FROM dialog_messages"
                    " WHERE id=? AND session_id=?", (message_id, session_id))
                kids: list[str] = []
                if row is not None and row.get("rag_refs"):
                    try:
                        parsed = json.loads(row["rag_refs"])
                        for r in parsed:
                            if isinstance(r, dict) and isinstance(r.get("id"), str):
                                kids.append(r["id"])
                    except (json.JSONDecodeError, TypeError):
                        kids = []
                if kids:
                    from ..services.knowledge_service import get_knowledge_service
                    ksvc = get_knowledge_service()
                    delta = -0.8 if rating < 0 else 0.2
                    for kid in kids:
                        ksvc.adjust_quality_score(kid, delta)
                    log.info("知识反馈闭环: 消息 %s 评分 %d → 调整 %d 条引用知识质量分",
                             message_id[:8], rating, len(kids))
        except Exception as exc:  # noqa: BLE001 - 反馈降权失败不影响评分
            log.warning("知识反馈降权失败（评分已保存）: %s", exc)
    return result


@router.post("/chat/sessions/{session_id}/messages/{message_id}/favorite")
def chat_favorite_message(session_id: str, message_id: str) -> dict[str, Any]:
    """收藏切换（DIALOG-046）：favorite 取反。"""
    return _update_message_flag(session_id, message_id, "favorite", None)


def _update_message_flag(sid: str, mid: str, field: str,
                         value: int | None) -> dict[str, Any]:
    """更新消息 rating/favorite 标志（db 优先，内存兜底），返回更新后消息。"""
    db = get_db_safe()
    if db is not None:
        try:
            row = db.query_one(
                "SELECT id, session_id, role, content, attachments,"
                " model_used, rating, favorite, reasoning, timestamp"
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
                        page_size: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    """收藏夹列表（全部收藏消息，时间倒序）。"""
    db = get_db_safe()
    items: list[dict] = []
    if db is not None:
        try:
            rows = db.query(
                "SELECT id, session_id, role, content, attachments,"
                " model_used, rating, favorite, reasoning, timestamp"
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
def chat_stop(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
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


async def _wait_vllm_booting(engine: DialogEngine, websocket: WebSocket,
                             sid: str, timeout_s: float = 200.0) -> bool:
    """vLLM 冷启动等待（2026-09-02 P1 修复）。

    ensure_loaded 因显存被 booting 预分配拒绝时进入宽限轮询：就绪
    立即返回 True；期间捕获「消息先到、vLLM 一秒后才点火」的竞态
    （实测 12:24:51 消息 / 12:24:52 booting——首版仅在已 booting 时
    等待，恰好错过）。等待期间向 WS 推送 status 帧维持前端占位。
    返回 True=已就绪可重试；False=超时/WS 断开。
    """
    try:
        await websocket.send_json({
            "type": "status",
            "data": {"phase": "model_loading",
                     "message": "对话模型冷启动中，本条消息已自动排队，"
                                "就绪后立即回复"},
        })
    except Exception:  # noqa: BLE001 - 推送失败不阻断等待
        log.debug("_wait_vllm_booting: 降级忽略", exc_info=True)
    import asyncio
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(2.0)
        try:
            if engine.get_status().get("state") == "ready":
                log.info("冷启动等待命中：vLLM 就绪，继续处理消息 sid=%s", sid)
                return True
        except Exception:  # noqa: BLE001 - 状态探测失败继续等
            continue
        # WS 断开检测：客户端关页即放弃
        try:
            if websocket.client_state == 1:  # DISCONNECTED
                return False
        except Exception:  # noqa: BLE001
            log.debug("_wait_vllm_booting: 降级忽略", exc_info=True)
    log.warning("冷启动等待超时 %.0fs sid=%s", timeout_s, sid)
    return False


# WS 模型参数旧标签 → 完整 model_id 映射（前端规格档位兼容；
# 2026-08-20 模型选择接线：完整 model_id 直传不经此表）
_WS_MODEL_LABEL_MAP = {
    "8b": "qwen3-vl-8b", "4b": "qwen3-vl-4b",
    "2b": "qwen2-vl-2b", "qwen3-vl-2b": "qwen3-vl-2b",
}


def _resolve_ws_model(raw: Any) -> str | None:
    """把前端 model 参数（旧档位标签或完整 model_id）归一为完整 id。

    None / 未知名 → None（引擎自动路由，行为与旧版一致）。
    """
    if not raw or not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    if key in _WS_MODEL_LABEL_MAP:
        return _WS_MODEL_LABEL_MAP[key]
    return raw.strip() or None


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

    # 功能互斥锁（规格 §6.1）＋ 对话排队（2026-09-08 落地）：
    # 被绘画/视频/训练持锁时不再秒拒「绘画进行中，其他AI功能暂不可用」
    # ——消息进入排队等待（上限 _DIALOG_QUEUE_WAIT_S），期间每 2s 推
    # queued 状态（前端气泡显示「排队中…结束后自动继续」），锁释放后
    # 自动继续生成。WS 断开（用户点停止/关页）即退出排队；超时如实
    # 报错。热保护（20004）与未知互斥方照旧秒拒（等待无意义）。
    lock = None
    try:
        lock = await acquire_or_raise("dialog", task_id=sid)
    except ApiError as exc:
        from ..middleware.feature_lock import get_feature_lock
        _blocker = get_feature_lock().active_feature
        _blocker_zh = {"paint": "AI绘画", "video_gen": "视频生成",
                       "training": "训练"}.get(_blocker or "", "其他AI功能")
        log.info("对话锁被占：code=%s blocker=%s sid=%s",
                 exc.code, _blocker, sid)
        # ApiError 构造时把 int 码转成语义串（error_handler._to_semantic
        # ：40007 → "FEATURE_MUTEX_LOCKED"）——两种形态都认
        if exc.code not in (40007, "FEATURE_MUTEX_LOCKED") \
                or _blocker not in ("paint", "video_gen", "training"):
            log.warning("对话秒拒（不可排队）：code=%s blocker=%s",
                        exc.code, _blocker)
            await _ws_send_error(websocket, exc.code, exc.message)
            return
        log.info("对话进入排队等待：blocker=%s sid=%s", _blocker, sid)
        _pos = _dialog_queue_enter(sid)
        # 审计 P2-1（2026-09-12）：排队全程 try/finally 兜底出队——
        # 此前 asyncio.sleep 被取消（客户端断开）等异常路径会泄漏
        # 位次表条目（_dialog_queue_exit 幂等，显式出口移除防重复）
        try:
            _deadline = time.monotonic() + _DIALOG_QUEUE_WAIT_S
            _waited = 0
            while True:
                _waited = int(_DIALOG_QUEUE_WAIT_S - (_deadline - time.monotonic()))
                _pos = _dialog_queue_position(sid)
                try:
                    await websocket.send_json({
                        "type": "status",
                        "data": {
                            "phase": "queued",
                            "blocking": _blocker,
                            "position": _pos,
                            "waited_s": max(0, _waited),
                            "message": f"排队中：{_blocker_zh}进行中，"
                                       f"你排在第 {_pos} 位，"
                                       "结束后自动继续"
                                       f"（已等待 {max(0, _waited)} 秒，"
                                       "可点停止退出排队）",
                        },
                    })
                except Exception:  # noqa: BLE001 - WS 断开即退出排队
                    return
                try:
                    lock = await acquire_or_raise("dialog", task_id=sid)
                    break
                except ApiError as retry_exc:
                    if time.monotonic() >= _deadline:
                        await _ws_send_error(
                            websocket, retry_exc.code,
                            f"排队超时（{_DIALOG_QUEUE_WAIT_S:.0f} 秒）："
                            f"{retry_exc.message}")
                        return
                await asyncio.sleep(2.0)
        finally:
            _dialog_queue_exit(sid)

    try:
        # 模型选择接线（2026-08-20）：前端 model 参数（旧档位标签或完整
        # model_id）归一后交给 ensure_loaded——已加载且请求不同模型时
        # 引擎自动热切换；物理显存装不下由引擎闸门拒绝并回错误
        model_req = _resolve_ws_model(data.get("model"))
        # 模块级选型配置生效（模型管理 → 功能模块模型配置）：
        # ① 显式点名模型不在白名单 → 如实拒绝（精细化管控落地）
        # ② 未指定模型（None = 系统默认）且配置了模块默认 → 采用默认
        try:
            from .models import get_module_model_scope as _dlg_scope
            _allowed, _default = _dlg_scope("dialog")
            # cloud:: 虚拟模型（批1 云端API）绕过白名单：云端模型来自
            # 设置页用户显式配置的服务商连接，不属本地模型注册表，
            # 白名单（本地模型精细化管控语义）不适用——2026-09-06
            # 真浏览器走查抓出的拦截 bug（进程内直调不经过此层）
            if model_req and not str(model_req).startswith("cloud::") \
                    and _allowed is not None \
                    and model_req not in _allowed:
                await _ws_send_error(
                    websocket, 40004,
                    f"模型 {model_req} 不在 AI 对话模块的可用范围内，"
                    "请在模型管理中调整配置")
                return
            if model_req is None and _default:
                model_req = _default
        except Exception as exc:  # noqa: BLE001 - 配置读取失败不阻断对话
            log.warning("对话模块选型配置读取失败（跳过）: %s", exc)
        # 深度思考模式（2026-08-22 思考过程展示）：前端 thinking 参数
        # 开启时 system prompt 追加四步框架引导，模型自输出 <think> 块。
        # 2026-09-10 同 :822 根修——vLLM 原生思考后端不注入可见思考框架
        # （防双思考打架 + </think> 标签泄漏进答案）
        thinking = bool(data.get("thinking"))
        _backend_name = getattr(engine, "_backend_name", "") or ""
        if thinking and _backend_name not in ("vllm", "gguf", "llama"):
            sys_prompt = DEFAULT_SYSTEM_PROMPT + THINKING_SYSTEM_SUFFIX
        else:
            sys_prompt = DEFAULT_SYSTEM_PROMPT
        # 思考通道有独立 token 预算需求（思考 500-2000 token 常态）
        max_new_tokens = 2048 if thinking else 1024
        # 温度 / 上下文长度接线（2026-08-31 补全）：前端早已随消息下发
        # （useDialogStore.sendMessage），此前后端未消费——温度恒走引擎
        # 默认 0.7、上下文恒写死 8192。钳制防野值：温度 0~2（对齐滑杆
        # 范围），上下文 2048~8192（对齐选择器；vLLM 启动 max-model-len
        # 即 8192，超了会直接撞墙）
        try:
            temperature = min(2.0, max(0.0, float(data.get("temperature", 0.7))))
        except (TypeError, ValueError):
            temperature = 0.7
        try:
            ctx_tokens = int(data.get("context_tokens") or 8192)
        except (TypeError, ValueError):
            ctx_tokens = 8192
        ctx_tokens = min(8192, max(2048, ctx_tokens))
        # 冷启动窗口状态推送（2026-08-22 思考过长事故）：模型未就绪时
        # 先告知前端加载阶段——vLLM 冷启动 ~157s 全程零消息，用户只
        # 见"思考中"无任何反馈（含被模块切换杀掉后二次冷启动场景）
        try:
            if engine.get_status().get("state") != "ready":
                await websocket.send_json({
                    "type": "status",
                    "data": {
                        "phase": "model_loading",
                        "message": "对话模型冷启动中（首次加载约 2-3 分钟），"
                                   "请稍候，期间请勿切换页面",
                    },
                })
        except Exception:  # noqa: BLE001 - 状态推送失败不阻断对话
            log.debug("_ws_handle_message: 降级忽略", exc_info=True)
        # 审计 R1-04：同 dialog_send，ensure_loaded 阻塞调用经 run_blocking 卸载
        if not await run_blocking(engine.ensure_loaded, model_req):
            # P1 修复（2026-09-02 冷启动实测）：vLLM 正在 booting 时
            # model_manager 因显存被预分配而拒绝（可用 0.0GB）——但服务
            # 就绪后即可用。产品铁律「未加载必须全自动」：消息应等待
            # 冷启动完成重试一次（最长 200s），而非立即拒绝。
            if await _wait_vllm_booting(engine, websocket, sid):
                if not await run_blocking(engine.ensure_loaded, model_req):
                    status = engine.get_status()
                    await _ws_send_error(
                        websocket, 30003,
                        status["last_error"] or "对话模型未就绪，请稍后再试")
                    return
            else:
                status = engine.get_status()
                code = 30004 if status["state"] == "error" else 30003
                await _ws_send_error(
                    websocket, code,
                    status["last_error"] or "对话模型未就绪，请稍后再试")
                return

        # RAG 注入 + 上下文组装（max_tokens=前端上下文长度选择，钳制后）
        knowledge_text, _refs = _rag_enhance(content)
        # 联网搜索 v1（默认关；命中意图时资料块并入上下文，来源经
        # web_refs 事件透出供前端来源卡片渲染）
        knowledge_text, web_refs = await _web_search_augment(
            content, knowledge_text)
        history = _load_history(sid)
        messages = engine.build_context(
            content, history=history, knowledge_text=knowledge_text,
            system_prompt=sys_prompt, images=images or None,
            max_tokens=ctx_tokens,
        )

        _ensure_session(sid, content, engine.model_name)
        _save_message(sid, "user", content,
                      attachments=data.get("images"))

        # 流式推理：同步生成器放线程执行，token 经队列回事件循环转发。
        # 深度思考模式走 chat_stream_ex 双通道（reasoning/content 分离）
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        collected: list[str] = []
        reasoning_parts: list[str] = []
        error_holder: list[str] = []

        def _produce() -> None:
            try:
                for event in engine.chat_stream_ex(
                        messages, images=images or None,
                        temperature=temperature,
                        max_new_tokens=max_new_tokens,
                        enable_thinking=thinking,
                        stop_check=lambda: sid in _stop_flags):
                    kind = event["type"]
                    text = event["text"]
                    if not text:
                        continue
                    if kind == "reasoning":
                        reasoning_parts.append(text)
                        loop.call_soon_threadsafe(
                            queue.put_nowait, ("reasoning", text))
                    else:
                        collected.append(text)
                        loop.call_soon_threadsafe(
                            queue.put_nowait, ("token", text))
            except Exception as exc:  # noqa: BLE001
                log.exception("WS 对话流式推理失败")
                error_holder.append(str(exc))
                loop.call_soon_threadsafe(
                    queue.put_nowait, ("error", str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

        producer = loop.run_in_executor(None, _produce)
        try:
            if web_refs:
                await websocket.send_json(
                    {"type": "web_refs", "data": {"refs": web_refs}})
            while True:
                kind, payload = await queue.get()
                if kind == "token":
                    await websocket.send_json(
                        {"type": "token", "data": {"token": payload}})
                elif kind == "reasoning":
                    await websocket.send_json(
                        {"type": "reasoning", "data": {"text": payload}})
                elif kind == "error":
                    await _ws_send_error(websocket, 30004, payload)
                elif kind == "done":
                    break
            await producer
        finally:
            _stop_flags.discard(sid)

        reply = "".join(collected)
        reasoning_full = "".join(reasoning_parts)
        # 被动补全（R2-B01）：首轮回复含不确定性标记时，快速搜索重推理
        # 一次，改进回复作为追加 token 推送后再落库（meta 如实标注）。
        # 检测只作用于 content 段（思考文本天然含"不确定/可能"会误触发）
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
        # M-4 兜底：解析器异常路径残留标签剥离，防污染后续轮上下文
        reply = strip_think_tags(reply) if reply else reply
        saved = (_save_message(sid, "assistant", reply,
                              model_used=engine.model_name,
                              reasoning=reasoning_full)
                 if (reply or reasoning_full) else None)
        if not error_holder:
            meta: dict = {
                "engine": engine.model_name,
                "message_id": saved["id"] if saved else "",
                "first_token_ms": round(engine.last_first_token_ms, 1),
            }
            # 降级真身标注（2026-09-08 用户报「AI 跟选择的不符」零感知）：
            # 引擎显存装不下被自动降级时，meta 帧带降级来源供前端当场
            # 提示；未降级不带该字段
            _degraded_from = getattr(engine, "_degraded_from", "") or ""
            if _degraded_from:
                meta["degraded_from"] = _degraded_from
            if thinking:
                meta["thinking_used"] = True
                meta["first_content_ms"] = round(
                    engine.last_first_content_ms, 1)
            if passive:
                meta["passive_completion"] = passive
            await websocket.send_json({"type": "meta", "data": meta})
        await websocket.send_json({"type": "done", "data": {}})
    finally:
        await lock.release("dialog")
