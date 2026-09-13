"""知识处理 + 行为学习 API 路由（TASK-033/034/035/045/051/055）。

端点清单（main.py 以 /v1 前缀挂载后生效）：
- GET    /knowledge/stats              知识库统计（条数/磁盘大小/主题分布）
- GET    /knowledge/list               知识列表（分页 + topic 过滤 + keyword 搜索）
- GET    /knowledge/graph              知识图谱（TASK-055）
- GET    /knowledge/{kid}              知识详情
- DELETE /knowledge/{kid}              删除知识（级联删向量与元数据）
- POST   /knowledge/import-document    导入文档（multipart: pdf/docx/txt/md）
- POST   /knowledge/process-text       直接处理文本（测试用）
- POST   /behavior/event               记录行为事件
- GET    /behavior/stats               行为学习统计
- GET    /behavior/training-pairs      LoRA 训练数据预览
- POST   /behavior/clear               清空行为日志

契约别名（规格 §7.1.2 /v1/learn 路由详情）：
- GET    /learn/knowledge/list         = /knowledge/list
- GET    /learn/knowledge/search       语义检索（混合检索，TASK-051）
- DELETE /learn/knowledge/delete       删除知识（单条 {id} / 批量 {ids[]}）
- GET    /learn/knowledge/export       导出知识库（format=json|csv）
- GET    /learn/knowledge/graph        = /knowledge/graph
- GET    /learn/behavior/stats         = /behavior/stats
- POST   /learn/behavior/reset         = /behavior/clear
- POST   /learn/import/document        = /knowledge/import-document

约定：router 不带 prefix（与 api/ 下现有模块一致，由 main.py 统一挂 /v1）；
成功 ok(data)；错误抛 ApiError（语义码：KNOWLEDGE_*/LEARN_*/SYSTEM_*，
见 middleware/error_handler.py SEMANTIC_CODES）。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from ..config import DATA_DIR
from ..data.database import Database
from ..middleware import upload_guard
from ..middleware.error_handler import ApiError, ok
from ..services import knowledge_quality_gate
from ..services.behavior_service import get_behavior_service
from ..services.injection_service import get_injection_service
from ..services.knowledge_service import Knowledge, get_knowledge_service

router = APIRouter()
log = logging.getLogger("omnispace.api.knowledge")

# ── 错误码约定 ──────────────────────────────────────────────
# 审计 R1-01：原自定义 60xxx 数字码与 _LEGACY_CODE_MAP 视频段碰撞
# （60001→VIDEO_GENERATION_FAILED 等语义错位），已全部改用语义码：
#   知识处理失败   KNOWLEDGE_PROCESS_FAILED
#   知识不存在     KNOWLEDGE_NOT_FOUND
#   文档格式不支持 UNSUPPORTED_FORMAT
#   文档解析失败   KNOWLEDGE_PARSE_FAILED
#   参数错误       SYSTEM_PARAM_INVALID
#   行为记录/清理  LEARN_BEHAVIOR_RECORD_FAILED / LEARN_BEHAVIOR_CLEAR_FAILED

_SUPPORTED_EXTS = {".pdf", ".docx", ".txt", ".md"}

# 审计 P1-4：上传大小上限 50MB（与 learn.py 数据集上传一致）
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


# ── 请求模型 ─────────────────────────────────────────────────

class ProcessTextRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    content: str = Field(..., validation_alias=AliasChoices("content", "text"),
                         description="待处理文本/HTML（兼容别名字段 text）")
    topic: str = Field(..., description="学习主题")
    source_url: str = Field("", description="来源 URL（可选）")


class BehaviorEventRequest(BaseModel):
    event_type: str = Field(..., description="事件类型")
    content: str = Field("", description="事件内容")
    context: str = Field("", description="上下文")
    before: str = Field("", description="修改前")
    after: str = Field("", description="修改后")
    feature: str = Field("", description="所属功能模块")
    timestamp: float = Field(0.0, description="时间戳（0=服务端时间）")


# ── 文档解析 ─────────────────────────────────────────────────

def _parse_document(filename: str, data: bytes) -> str:
    """解析上传文档为纯文本。pdf→pymupdf，docx→python-docx，
    txt/md→多编码解码；缺库/失败时降级纯文本并记日志。"""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext not in _SUPPORTED_EXTS:
        raise ApiError("UNSUPPORTED_FORMAT",
                       f"不支持的文档格式: {ext or '(无扩展名)'}，"
                       f"支持 {sorted(_SUPPORTED_EXTS)}",
                       suggestion="请上传 pdf/docx/txt/md 文件")
    if ext in (".txt", ".md"):
        return _decode_text(data)
    if ext == ".pdf":
        try:
            import fitz  # type: ignore  # pymupdf
            parts: list[str] = []
            with fitz.open(stream=data, filetype="pdf") as doc:
                for page in doc:
                    parts.append(page.get_text("text"))
            text = "\n".join(parts).strip()
            if text:
                return text
            log.warning("pymupdf 未提取到文本，降级纯文本解码")
        except ImportError:
            log.warning("pymupdf 不可用，PDF 降级纯文本解码")
        except Exception as exc:  # noqa: BLE001
            log.warning("PDF 解析失败，降级纯文本解码: %s", exc)
        return _decode_text(data)
    # .docx
    try:
        import io

        import docx  # type: ignore  # python-docx
        doc = docx.Document(io.BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        if text.strip():
            return text
        log.warning("python-docx 未提取到文本，降级纯文本解码")
    except ImportError:
        log.warning("python-docx 不可用，DOCX 降级纯文本解码")
    except Exception as exc:  # noqa: BLE001
        log.warning("DOCX 解析失败，降级纯文本解码: %s", exc)
    return _decode_text(data)


def _decode_text(data: bytes) -> str:
    """多编码文本解码回退（utf-8 → gbk → utf-8/ignore）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return data.decode("utf-8", errors="ignore")


# ═══════════════════════════════════════════════════════════════════
#  知识库管理
# ═══════════════════════════════════════════════════════════════════

@router.get("/knowledge/stats")
def knowledge_stats() -> dict[str, Any]:
    """知识库统计：条数 / 磁盘大小 / 主题分布 / 后端状态。"""
    svc = get_knowledge_service()
    return ok(svc.stats())


@router.get("/knowledge/list")
def knowledge_list(page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=200),
                   topic: str = Query(""),
                   keyword: str = Query(""),
                   type: str = Query(""),
                   min_score: float = Query(0.0, ge=0.0)) -> dict[str, Any]:
    """知识列表：分页 + topic 过滤 + keyword 搜索（匹配 content/title）。

    LEARN-040：type 精确过滤、min_score 质量分下限过滤。"""
    svc = get_knowledge_service()
    return ok(svc.list_knowledge(page=page, page_size=page_size,
                                 topic=topic, keyword=keyword,
                                 type=type, min_score=min_score))


@router.get("/knowledge/graph")
def knowledge_graph(kid: str = Query(""),
                    max_nodes: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    """知识图谱（TASK-055）：kid 为空返回全局 Top-N 图谱，
    否则返回该知识直接关系 + 一跳邻居扩展。"""
    svc = get_knowledge_service()
    return ok(svc.knowledge_graph(kid=kid, max_nodes=max_nodes))


@router.get("/learn/knowledge/graph")
def learn_knowledge_graph(kid: str = Query(""),
                          max_nodes: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    """规格 §4 契约路径别名（/v1/learn/knowledge/graph，v2.3.1 新增）。"""
    return knowledge_graph(kid=kid, max_nodes=max_nodes)


@router.get("/knowledge/{kid}")
def knowledge_detail(kid: str) -> dict[str, Any]:
    """知识详情。"""
    svc = get_knowledge_service()
    row = svc.get_knowledge(kid)
    if row is None:
        raise ApiError("KNOWLEDGE_NOT_FOUND", "知识不存在",
                       detail={"id": kid})
    return ok(row)


@router.delete("/knowledge/{kid}")
def knowledge_delete(kid: str) -> dict[str, Any]:
    """删除知识：级联删向量与元数据。"""
    svc = get_knowledge_service()
    if not svc.delete_knowledge(kid):
        raise ApiError("KNOWLEDGE_NOT_FOUND", "知识不存在",
                       detail={"id": kid})
    return ok({"id": kid}, message="知识已删除")


@router.post("/knowledge/import-document")
async def knowledge_import_document(file: UploadFile = File(...),
                                    topic: str = Form(...),
                                    source_url: str = Form("")) -> dict[str, Any]:
    """导入文档（multipart 上传 pdf/docx/txt/md），走完整管线入话题。"""
    if not file or not file.filename:
        raise ApiError("SYSTEM_PARAM_INVALID", "未提供上传文件")
    if not topic or not topic.strip():
        raise ApiError("SYSTEM_PARAM_INVALID", "topic 不能为空")
    # 审计 R3-BE2：限量读取（上限+1 字节），先验大小再判空，
    # 避免超限文件被整体读入内存
    data = await file.read(_MAX_UPLOAD_BYTES + 1)
    if not data:
        raise ApiError("SYSTEM_PARAM_INVALID", "上传文件内容为空")
    # 审计 P1-4：50MB 上传上限
    if len(data) > _MAX_UPLOAD_BYTES:
        raise ApiError("OPERATION_LIMIT_EXCEEDED", "上传文件超过 50MB 上限",
                       detail={"size_bytes": len(data),
                               "limit_bytes": _MAX_UPLOAD_BYTES},
                       suggestion="请拆分文档后分批导入")
    # TASK-P0-03：后缀白名单 + 魔数嗅验（拦截伪装扩展名的可执行体）
    try:
        upload_guard.validate(file.filename, data, upload_guard.DOCUMENT_TABLE)
    except upload_guard.UploadRejected as exc:
        raise ApiError("UNSUPPORTED_FORMAT", str(exc),
                       detail={"filename": file.filename}) from exc
    try:
        text = _parse_document(file.filename, data)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ApiError("KNOWLEDGE_PARSE_FAILED", "文档解析失败",
                       detail={"filename": file.filename, "error": str(exc)}) from exc
    if not text.strip():
        raise ApiError("KNOWLEDGE_PARSE_FAILED", "文档中未提取到可用文本",
                       detail={"filename": file.filename})
    svc = get_knowledge_service()
    try:
        items = svc.process_page(text, topic.strip(),
                                 source_url=source_url or file.filename)
    except Exception as exc:
        raise ApiError("KNOWLEDGE_PROCESS_FAILED", "知识处理失败",
                       detail={"error": str(exc)}) from exc
    _msg = (f"已提取 {len(items)} 个知识点" if items else
            "已提取 0 个知识点（可能原因：对话模型未就绪导致提取为空，"
            "或内容被入库质检过滤；请确认对话模型已加载后重试）")
    return ok({"filename": file.filename, "topic": topic.strip(),
               "extracted": len(items),
               "items": [k.to_dict() for k in items]},
              message=_msg)


@router.post("/knowledge/process-text")
def knowledge_process_text(req: ProcessTextRequest) -> dict[str, Any]:
    """直接处理文本（测试用），走完整管线入话题。"""
    if not req.content or not req.content.strip():
        raise ApiError("SYSTEM_PARAM_INVALID", "content 不能为空")
    if not req.topic or not req.topic.strip():
        raise ApiError("SYSTEM_PARAM_INVALID", "topic 不能为空")
    svc = get_knowledge_service()
    try:
        items = svc.process_page(req.content, req.topic.strip(),
                                 source_url=req.source_url or None)
    except Exception as exc:
        raise ApiError("KNOWLEDGE_PROCESS_FAILED", "知识处理失败",
                       detail={"error": str(exc)}) from exc
    _msg = (f"已提取 {len(items)} 个知识点" if items else
            "已提取 0 个知识点（可能原因：对话模型未就绪导致提取为空，"
            "或内容被入库质检过滤；请确认对话模型已加载后重试）")
    return ok({"topic": req.topic.strip(), "extracted": len(items),
               "items": [k.to_dict() for k in items]},
              message=_msg)


# ═══════════════════════════════════════════════════════════════════
#  图片知识（UAT 2026-09-11 缺口：知识库支持图片知识）
#  设计：图片按 knowledge_images/{kid}.{ext} 约定存放（零 schema 迁
#  移），条目 type="image"、content=VLM 中文描述（可搜索/可向量化），
#  回读走 GET /knowledge/{kid}/image。
# ═══════════════════════════════════════════════════════════════════

_KB_IMG_DIR = DATA_DIR / "knowledge_images"
_KB_IMG_EXTS = (".png", ".jpg", ".jpeg")
_KB_IMG_MAX_BYTES = 10 * 1024 * 1024
_KB_DESCRIBE_PROMPT = (
    "请用中文详细描述这张图片的主要内容（人物/场景/物体/动作），"
    "如图中有文字请原样转录。描述将作为知识条目用于检索，"
    "请写成 2-4 个完整句子。")


def _kb_image_path(kid: str) -> Path | None:
    """按约定扩展名探测图片路径（kid 已在端点层校验为十六进制）。"""
    for ext in _KB_IMG_EXTS:
        p = _KB_IMG_DIR / f"{kid}{ext}"
        if p.is_file():
            return p
    return None


@router.post("/knowledge/import-image")
async def knowledge_import_image(file: UploadFile = File(...),
                                 topic: str = Form(...)) -> dict[str, Any]:
    """导入图片知识：VLM 中文描述入库（可搜索），图片本体按 id 存档。

    流程：类型嗅验（png/jpg/jpeg，10MB）→ 视觉模型描述（对话引擎
    未就绪时有界等待自动装载，失败诚实报错带出路）→ 质检闸 → 入库
    （type=image，content=VLM 描述）→ 图片落盘 knowledge_images/。
    """
    if not file or not file.filename:
        raise ApiError("SYSTEM_PARAM_INVALID", "未提供上传文件")
    if not topic or not topic.strip():
        raise ApiError("SYSTEM_PARAM_INVALID", "topic 不能为空")
    data = await file.read(_KB_IMG_MAX_BYTES + 1)
    if not data:
        raise ApiError("SYSTEM_PARAM_INVALID", "上传文件内容为空")
    if len(data) > _KB_IMG_MAX_BYTES:
        raise ApiError("OPERATION_LIMIT_EXCEEDED", "图片超过 10MB 上限",
                       detail={"size_bytes": len(data),
                               "limit_bytes": _KB_IMG_MAX_BYTES},
                       suggestion="请压缩图片后重试")
    try:
        upload_guard.validate(file.filename, data, upload_guard.MEDIA_TABLE)
    except upload_guard.UploadRejected as exc:
        raise ApiError("UNSUPPORTED_FORMAT", str(exc),
                       detail={"filename": file.filename}) from exc
    from PIL import Image as _PILImage

    try:
        pil_probe = _PILImage.open(io.BytesIO(data))
        pil_probe.verify()
        pil = _PILImage.open(io.BytesIO(data)).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - 解码失败即非法图片
        raise ApiError("UNSUPPORTED_FORMAT", "图片解码失败，请上传有效图片",
                       detail={"error": str(exc)[:120]}) from exc

    # 视觉模型就绪（有界等待自动装载，产品铁律：不许「再点一次」）；
    # 当前已就绪且为视觉模型（4B/8B-awq）则直接复用，不做无谓热切换
    from ..services.inference.dialog_engine import get_dialog_engine
    eng = get_dialog_engine()
    vl_ids = ("qwen3-vl-4b", "qwen3-vl-8b-awq")
    if not (eng.is_ready and eng.get_status().get("model") in vl_ids):
        loaded = False
        for vl in vl_ids:
            if eng.ensure_loaded(vl):
                loaded = True
                break
        if not loaded:
            raise ApiError("MODEL_NOT_READY",
                           "视觉模型装载失败，无法识别图片",
                           suggestion="请到「模型管理」确认 qwen3-vl-4b/8b "
                                      "在位后重试；或稍后等显存空闲再试")

    t0 = time.time()
    # 消息组装复用 engine.build_context（与对话页图片理解同链路）：
    # 由它按后端能力产出多模态部件列表/纯文本，保证占位符与图片对齐
    messages = eng.build_context(
        f"{_KB_DESCRIBE_PROMPT}\n\n（图片文件名：{file.filename}）",
        history=[], knowledge_text="", images=[pil],
        max_tokens=2048)
    # 走 chat_stream_ex（对话页图片理解同链路，已实弹验证）；join
    # 正文 token（思考段由 enable_thinking=False 抑制）；error 事件
    # 捕获透出（此前被静默丢弃，0.4s 空回复无从排查）
    parts: list[str] = []
    err_text = ""
    try:
        for ev in eng.chat_stream_ex(
                messages, images=[pil], max_new_tokens=512,
                enable_thinking=False):
            if not isinstance(ev, dict):
                continue
            # chat_stream_ex 事件类型为 reasoning/content（非 token）
            if ev.get("type") == "content":
                parts.append(ev.get("text") or "")
            elif ev.get("type") == "error":
                err_text = ev.get("text") or ""
    except Exception as exc:  # noqa: BLE001 - 生成异常转诚实报错不 500
        err_text = f"{type(exc).__name__}: {str(exc)[:120]}"
    description = "".join(parts).strip()
    log.info("[图片知识] VLM 原始回复 %.1fs: %r（error=%r）",
             time.time() - t0, description[:200], err_text[:120])
    if not description and err_text:
        raise ApiError("KNOWLEDGE_PROCESS_FAILED", f"视觉描述失败：{err_text[:150]}",
                       suggestion="请重试；若反复出现请到「模型管理」检查视觉模型状态")
    if not description:
        raise ApiError("KNOWLEDGE_PROCESS_FAILED",
                       "视觉模型未能生成图片描述，请重试")
    log.info("图片知识描述完成: %s（%.1fs，%d 字）",
             file.filename, time.time() - t0, len(description))

    gate_ok, gate_reason = knowledge_quality_gate.check(description, "fact")
    if not gate_ok:
        raise ApiError("KNOWLEDGE_PROCESS_FAILED",
                       f"描述未通过入库质检（{gate_reason}），请重试")

    kid = uuid.uuid4().hex
    _KB_IMG_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "x.png").suffix.lower() or ".png"
    if ext not in _KB_IMG_EXTS:
        ext = ".png"
    (_KB_IMG_DIR / f"{kid}{ext}").write_bytes(data)

    svc = get_knowledge_service()
    k = Knowledge(id=kid, title=(file.filename or "图片知识").rsplit(".", 1)[0],
                  content=description, topic=topic.strip(), type="image",
                  source_url=f"knowledge_images/{kid}{ext}",
                  quality_score=0.9)
    svc.vectorize_and_store(k)
    return ok({"id": kid, "topic": topic.strip(),
               "description": description[:120],
               "chars": len(description)},
              message="图片知识已入库（含视觉描述，支持搜索）")


@router.get("/knowledge/{kid}/image")
def knowledge_image(kid: str) -> FileResponse:
    """回读图片知识原图（前端缩略图/放大预览）。"""
    import re as _re
    if not _re.fullmatch(r"[0-9a-f]{32}", kid):
        raise ApiError("SYSTEM_PARAM_INVALID", "非法知识 id")
    p = _kb_image_path(kid)
    if p is None:
        raise ApiError("KNOWLEDGE_NOT_FOUND", "图片不存在",
                       detail={"id": kid})
    media = "image/jpeg" if p.suffix in (".jpg", ".jpeg") else "image/png"
    return FileResponse(p, media_type=media)


# ═══════════════════════════════════════════════════════════════════
#  契约别名（规格 §7.1.2 /v1/learn/knowledge/* 等）
# ═══════════════════════════════════════════════════════════════════

@router.get("/learn/knowledge/list")
def learn_knowledge_list(page: int = Query(1, ge=1),
                         page_size: int = Query(20, ge=1, le=200),
                         topic: str = Query(""),
                         keyword: str = Query(""),
                         type: str = Query(""),
                         min_score: float = Query(0.0, ge=0.0)) -> dict[str, Any]:
    """契约别名：= GET /knowledge/list（LEARN-040 支持 type/min_score）。"""
    return knowledge_list(page=page, page_size=page_size,
                          topic=topic, keyword=keyword,
                          type=type, min_score=min_score)


@router.put("/learn/knowledge/{kid}")
def learn_knowledge_update(kid: str, body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """编辑知识条目（LEARN-041）：{content?, topic?, type?}。

    content 变更会重算 simhash 与语言标记并同步全文索引。
    """
    body = body or {}
    content = body.get("content")
    topic = body.get("topic")
    ktype = body.get("type")
    if content is None and topic is None and ktype is None:
        raise ApiError("SYSTEM_PARAM_INVALID",
                       "至少提供一个待更新字段: content/topic/type")
    if content is not None and not str(content).strip():
        raise ApiError("SYSTEM_PARAM_INVALID", "content 不能为空串")
    if ktype is not None:
        allowed = ("concept", "qa", "fact", "methodology", "case")
        if str(ktype) not in allowed:
            raise ApiError("SYSTEM_PARAM_INVALID",
                           f"type 仅支持 {list(allowed)}")
    svc = get_knowledge_service()
    if svc.get_knowledge(kid) is None:
        raise ApiError("KNOWLEDGE_NOT_FOUND", "知识不存在",
                       detail={"id": kid})
    svc.update_knowledge(
        kid,
        content=str(content).strip() if content is not None else None,
        topic=str(topic).strip() if topic is not None else None,
        type=str(ktype) if ktype is not None else None)
    return ok(svc.get_knowledge(kid), message="知识已更新")


@router.get("/learn/knowledge/search")
def learn_knowledge_search(q: str = Query(..., min_length=1),
                           top_k: int = Query(5, ge=1, le=50)) -> dict[str, Any]:
    """知识语义检索（规格 §7.1.2）：混合检索（向量+FTS5 关键词，RRF 融合）。"""
    svc = get_injection_service()
    items = svc.retrieve(q, top_k=top_k)
    return ok({"items": items, "total": len(items), "query": q,
               "latency_ms": round(svc.last_latency_ms, 2)})


@router.delete("/learn/knowledge/delete")
def learn_knowledge_delete(body: dict = Body(default_factory=dict)) -> dict[str, Any]:
    """删除知识（契约路径）：单条 {"id": "..."} 或批量 {"ids": [...]}。"""
    kid = str((body or {}).get("id", "") or "").strip()
    ids_raw = (body or {}).get("ids")
    ids: list[str] = []
    if isinstance(ids_raw, list):
        ids = [str(x).strip() for x in ids_raw if str(x or "").strip()]
    if kid and kid not in ids:
        ids.insert(0, kid)
    if not ids:
        raise ApiError("SYSTEM_PARAM_INVALID", "缺少必填参数: id 或 ids")
    if len(ids) > 500:
        raise ApiError("SYSTEM_PARAM_INVALID", "单次批量删除上限 500 条",
                       detail={"count": len(ids)})
    svc = get_knowledge_service()
    deleted, missing = 0, []
    for k in ids:
        if svc.delete_knowledge(k):
            deleted += 1
        else:
            missing.append(k)
    return ok({"deleted": deleted, "missing": missing},
              message=f"已删除 {deleted} 条知识")


@router.get("/learn/knowledge/export")
def learn_knowledge_export(format: str = Query("json"),
                           topic: str = Query("")) -> dict[str, Any]:
    """导出知识库（规格 §7.1.2 / T53）：format=json|csv，可按 topic 过滤。

    CSV 内容内嵌 ok() 信封的 data.content（与 /manga/storyboard export 一致）。
    """
    fmt = (format or "json").strip().lower()
    if fmt not in ("json", "csv"):
        raise ApiError("SYSTEM_PARAM_INVALID", "format 必须是 json 或 csv",
                       detail={"format": format})
    svc = get_knowledge_service()
    # 分页拉全量（上限 100000，与库容量一致）
    items: list[dict] = []
    page = 1
    while page <= 500:
        batch = svc.list_knowledge(page=page, page_size=200, topic=topic)
        rows = batch.get("items", [])
        items.extend(rows)
        total = int(batch.get("total", 0) or 0)
        if not rows or len(items) >= total:
            break
        page += 1

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "type", "title", "topic", "content",
                         "source_url", "quality_score", "lifecycle",
                         "created_at"])
        for r in items:
            writer.writerow([r.get("id", ""), r.get("type", ""),
                             r.get("title", ""), r.get("topic", ""),
                             r.get("content", ""), r.get("source_url", ""),
                             r.get("quality_score", 0), r.get("lifecycle", ""),
                             r.get("created_at", 0)])
        return ok({"format": "csv", "content": buf.getvalue(),
                   "total": len(items)})
    return ok({"format": "json", "items": items, "total": len(items)})


# ═══════════════════════════════════════════════════════════════════
#  批 3：知识导入 / 图谱导出 / 学习分析（LEARN-046/032/049~052）
# ═══════════════════════════════════════════════════════════════════

_KNOWLEDGE_IMPORT_LIMIT = 5000     # 单次导入条数上限
_MERGE_STRATEGIES = ("append", "overwrite", "dedup")


def _import_rows_from_payload(filename: str, data: bytes) -> list[dict]:
    """解析导入文件为知识行列表。json=导出格式（{"items":[...]} 或数组），
    csv=导出列头（id,type,title,topic,content,source_url,quality_score,
    lifecycle,created_at）。"""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    text = data.decode("utf-8-sig", errors="replace")
    if ext == ".json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ApiError("KNOWLEDGE_PARSE_FAILED",
                           f"JSON 解析失败: {exc}") from exc
        rows = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ApiError("KNOWLEDGE_PARSE_FAILED",
                           "JSON 必须是数组或含 items 数组的对象")
        return [r for r in rows if isinstance(r, dict)]
    if ext == ".csv":
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "content" not in reader.fieldnames:
            raise ApiError("KNOWLEDGE_PARSE_FAILED",
                           "CSV 缺少必需的 content 列")
        return [dict(r) for r in reader]
    raise ApiError("UNSUPPORTED_FORMAT",
                   f"导入仅支持 .json/.csv 文件（收到: {ext or '无扩展名'}）")


@router.post("/learn/knowledge/import")
async def learn_knowledge_import(
        file: UploadFile = File(...),
        merge_strategy: str = Form("dedup"),
        topic: str = Form("")) -> dict[str, Any]:
    """导入知识条目（LEARN-046）：multipart 上传 .json/.csv。

    merge_strategy:
    - append    全部作为新条目插入（原 id 弃用）
    - overwrite 同 id 覆盖既有条目
    - dedup     simhash 与库内既有条目相同则跳过（默认）
    topic（可选）：统一覆盖导入条目的主题归属。
    """
    if merge_strategy not in _MERGE_STRATEGIES:
        raise ApiError("SYSTEM_PARAM_INVALID",
                       f"merge_strategy 仅支持 {list(_MERGE_STRATEGIES)}")
    if not file or not file.filename:
        raise ApiError("SYSTEM_PARAM_INVALID", "未提供上传文件")
    data = await file.read(_MAX_UPLOAD_BYTES + 1)
    if not data:
        raise ApiError("SYSTEM_PARAM_INVALID", "上传文件内容为空")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise ApiError("OPERATION_LIMIT_EXCEEDED", "上传文件超过 50MB 上限",
                       detail={"size_bytes": len(data),
                               "limit_bytes": _MAX_UPLOAD_BYTES})
    rows = _import_rows_from_payload(file.filename, data)
    if not rows:
        raise ApiError("KNOWLEDGE_PARSE_FAILED", "文件中没有可导入的知识条目")
    if len(rows) > _KNOWLEDGE_IMPORT_LIMIT:
        raise ApiError("OPERATION_LIMIT_EXCEEDED",
                       f"单次导入上限 {_KNOWLEDGE_IMPORT_LIMIT} 条",
                       detail={"count": len(rows)})

    from ..services.knowledge_service import simhash64
    svc = get_knowledge_service()
    imported, skipped, overwritten, failed, rejected = 0, 0, 0, 0, 0
    existing_simhashes: set[str] = set()
    if merge_strategy == "dedup":
        for r in svc._iter_simhashes():  # noqa: SLF001 - 同服务协作
            if r.get("simhash"):
                existing_simhashes.add(r["simhash"])
    for r in rows:
        content = str(r.get("content") or "").strip()
        if not content:
            failed += 1
            continue
        # 入库质检闸（升级批2）：导航残渣/无答案QA/超短拒收
        gate_ok, gate_reason = knowledge_quality_gate.check(
            content, str(r.get("type") or "concept"))
        if not gate_ok:
            log.info("知识导入质检拒收(%s): %s", gate_reason, content[:40])
            rejected += 1
            continue
        k = Knowledge(
            id=str(r.get("id") or "").strip() or uuid.uuid4().hex,
            type=str(r.get("type") or "concept"),
            content=content,
            topic=str(topic or r.get("topic") or "").strip(),
            source_url=str(r.get("source_url") or ""),
            title=str(r.get("title") or ""),
            quality_score=float(r.get("quality_score") or 0.0),
        )
        k.simhash = format(simhash64(k.content), "016x")
        if merge_strategy == "dedup" and k.simhash in existing_simhashes:
            skipped += 1
            continue
        replace_id = k.id if merge_strategy == "overwrite" else None
        if merge_strategy == "append":
            k.id = uuid.uuid4().hex   # 原 id 弃用，强制新条目
        try:
            svc.vectorize_and_store(k, replace_id=replace_id)
            existing_simhashes.add(k.simhash)
            if merge_strategy == "overwrite" and replace_id:
                overwritten += 1
            else:
                imported += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("知识导入条目失败: %s", exc)
            failed += 1
    return ok({"imported": imported, "overwritten": overwritten,
               "skipped_duplicates": skipped, "failed": failed,
               "rejected": rejected,
               "total_rows": len(rows), "merge_strategy": merge_strategy},
              message=f"导入完成：新增 {imported}，覆盖 {overwritten}，"
                      f"去重跳过 {skipped}，质检拒收 {rejected}，失败 {failed}")


@router.get("/learn/knowledge/graph/export")
def learn_knowledge_graph_export(format: str = Query("gexf"),
                                 max_nodes: int = Query(500, ge=1, le=2000)) -> dict[str, Any]:
    """知识图谱导出（LEARN-032）：format=gexf|graphml（networkx 序列化，
    内容内嵌 ok() 信封 data.content，与知识库导出一致）。"""
    fmt = (format or "gexf").strip().lower()
    if fmt not in ("gexf", "graphml"):
        raise ApiError("SYSTEM_PARAM_INVALID",
                       "format 必须是 gexf 或 graphml",
                       detail={"format": format})
    try:
        import networkx as nx
    except ImportError as exc:
        raise ApiError("SYSTEM_DEPENDENCY_MISSING",
                       "networkx 不可用，无法导出图谱") from exc
    svc = get_knowledge_service()
    graph = svc.knowledge_graph(kid="", max_nodes=max_nodes)
    g = nx.Graph()
    for n in graph.get("nodes", []):
        g.add_node(str(n.get("id")),
                   label=str(n.get("label", ""))[:200],
                   kind=str(n.get("kind", "")))
    for e in graph.get("edges", []):
        src, dst = str(e.get("source", "")), str(e.get("target", ""))
        if src in g and dst in g:
            g.add_edge(src, dst, relation=str(e.get("relation", ""))[:100],
                       weight=float(e.get("weight", 1.0) or 1.0))
    buf = io.BytesIO()
    if fmt == "gexf":
        nx.write_gexf(g, buf)
    else:
        nx.write_graphml(g, buf)
    return ok({"format": fmt, "content": buf.getvalue().decode("utf-8"),
               "nodes": g.number_of_nodes(), "edges": g.number_of_edges()})


# ── 学习分析（LEARN-049~052，SQL 聚合）──────────────────────────────

def _analysis_db() -> Database:
    from ..data.database import get_db_safe
    db = get_db_safe()
    if db is None:
        raise ApiError("SYSTEM_DB_DEGRADED", "数据库不可用，分析功能暂不可用")
    from ..services.browser_agent_service import ensure_learning_tables
    ensure_learning_tables()
    return db


@router.get("/learn/analysis/efficiency")
def learn_analysis_efficiency(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """学习效率（LEARN-049）：近 N 天会话的提取率（知识点/页）与
    每小时提取量。"""
    db = _analysis_db()
    since = time.time() - days * 86400
    row = db.query_one(
        "SELECT COUNT(*) AS sessions,"
        " COALESCE(SUM(pages_visited),0) AS pages,"
        " COALESCE(SUM(knowledge_extracted),0) AS knowledge,"
        " COALESCE(SUM(ended_at-started_at),0) AS seconds"
        " FROM learning_sessions WHERE created_at >= ?", (since,))
    sessions = int(row["sessions"] or 0)
    pages = int(row["pages"] or 0)
    knowledge = int(row["knowledge"] or 0)
    hours = float(row["seconds"] or 0.0) / 3600.0
    return ok({
        "days": days, "sessions": sessions,
        "pages_visited": pages, "knowledge_extracted": knowledge,
        "extraction_rate": round(knowledge / pages, 3) if pages else 0.0,
        "per_hour": round(knowledge / hours, 2) if hours > 0 else 0.0,
        "hours": round(hours, 2)})


@router.get("/learn/analysis/sources")
def learn_analysis_sources(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """来源分析（LEARN-050）：知识条目按来源域名聚合（条数 + 平均质量分，
    平均质量分作为可信度代理指标）。"""
    db = _analysis_db()
    rows = db.query(
        "SELECT source_url, COUNT(*) AS c,"
        " AVG(quality_score) AS avg_score FROM knowledge_meta"
        " WHERE source_url != '' GROUP BY source_url"
        " ORDER BY c DESC LIMIT ?", (limit * 3,))
    domains: dict[str, dict] = {}
    from urllib.parse import urlparse
    for r in rows:
        host = urlparse(r["source_url"]).netloc or r["source_url"]
        d = domains.setdefault(host, {"domain": host, "count": 0,
                                      "_score_sum": 0.0})
        d["count"] += int(r["c"] or 0)
        d["_score_sum"] += float(r["avg_score"] or 0.0) * int(r["c"] or 0)
    items = sorted(domains.values(), key=lambda d: d["count"],
                   reverse=True)[:limit]
    for d in items:
        d["avg_quality_score"] = round(d.pop("_score_sum")
                                       / max(d["count"], 1), 3)
    return ok({"items": items, "total": len(items)})


@router.get("/learn/analysis/trend")
def learn_analysis_trend(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """学习趋势（LEARN-051）：近 N 天知识条目按日聚合（新增数/均分）。"""
    db = _analysis_db()
    since = time.time() - days * 86400
    rows = db.query(
        "SELECT created_at, quality_score FROM knowledge_meta"
        " WHERE created_at >= ? ORDER BY created_at ASC", (since,))
    by_day: dict[str, dict] = {}
    for r in rows:
        day = time.strftime("%Y-%m-%d", time.localtime(r["created_at"]))
        d = by_day.setdefault(day, {"date": day, "count": 0,
                                    "_score_sum": 0.0})
        d["count"] += 1
        d["_score_sum"] += float(r["quality_score"] or 0.0)
    items = list(by_day.values())
    for d in items:
        d["avg_quality_score"] = round(d.pop("_score_sum")
                                       / max(d["count"], 1), 3)
    return ok({"days": days, "items": items,
               "total": sum(d["count"] for d in items)})


@router.get("/learn/analysis/topic-compare")
def learn_analysis_topic_compare(topics: str = Query("")) -> dict[str, Any]:
    """主题对比（LEARN-052）：?topics=主题A,主题B（缺省对比全部主题，
    上限 10 个）——条数/平均质量分/最近学习时间。"""
    db = _analysis_db()
    names = [t.strip() for t in topics.split(",") if t.strip()][:10]
    if not names:
        rows = db.query(
            "SELECT topic, COUNT(*) AS c FROM knowledge_meta"
            " WHERE topic != '' GROUP BY topic ORDER BY c DESC LIMIT 10")
        names = [r["topic"] for r in rows]
    items = []
    for name in names:
        row = db.query_one(
            "SELECT COUNT(*) AS c, AVG(quality_score) AS avg_score,"
            " MAX(created_at) AS latest FROM knowledge_meta WHERE topic=?",
            (name,))
        items.append({
            "topic": name,
            "knowledge_count": int(row["c"] or 0),
            "avg_quality_score": round(float(row["avg_score"] or 0.0), 3),
            "latest_at": float(row["latest"] or 0.0)})
    return ok({"items": items, "total": len(items)})


@router.get("/learn/behavior/stats")
def learn_behavior_stats() -> dict[str, Any]:
    """契约别名：= GET /behavior/stats。"""
    return behavior_stats()


@router.post("/learn/import/document")
async def learn_import_document(file: UploadFile = File(...),
                                topic: str = Form(...),
                                source_url: str = Form("")) -> dict[str, Any]:
    """契约别名：= POST /knowledge/import-document。"""
    return await knowledge_import_document(file=file, topic=topic,
                                           source_url=source_url)


# ═══════════════════════════════════════════════════════════════════
#  行为学习
# ═══════════════════════════════════════════════════════════════════

@router.post("/behavior/event")
def behavior_event(req: BehaviorEventRequest) -> dict[str, Any]:
    """记录行为事件（异步落库，<10ms 返回）。"""
    if not req.event_type:
        raise ApiError("SYSTEM_PARAM_INVALID", "event_type 不能为空")
    svc = get_behavior_service()
    try:
        event_id = svc.record_event(req.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise ApiError("LEARN_BEHAVIOR_RECORD_FAILED", "行为事件记录失败",
                       detail={"error": str(exc)}) from exc
    return ok({"event_id": event_id}, message="已记录")


@router.get("/behavior/stats")
def behavior_stats() -> dict[str, Any]:
    """行为学习统计：操作数 / 偏好模型状态 / 下次微调时间 / 最近学习摘要。"""
    svc = get_behavior_service()
    return ok(svc.stats())


@router.get("/behavior/training-pairs")
def behavior_training_pairs(limit: int = Query(0, ge=0, le=10000)) -> dict[str, Any]:
    """LoRA 训练数据预览（instruction/input/output 格式）。"""
    svc = get_behavior_service()
    pairs = svc.build_training_pairs()
    total = len(pairs)
    if limit:
        pairs = pairs[:limit]
    return ok({"items": pairs, "total": total,
               "should_trigger_finetune": svc.should_trigger_finetune()})


@router.post("/behavior/clear")
def behavior_clear() -> dict[str, Any]:
    """清空行为学习数据（用户手动清除）。"""
    svc = get_behavior_service()
    try:
        deleted = svc.clear()
    except Exception as exc:  # noqa: BLE001
        raise ApiError("LEARN_BEHAVIOR_CLEAR_FAILED", "行为数据清理失败",
                       detail={"error": str(exc)}) from exc
    return ok({"deleted": deleted}, message="行为学习数据已清空")
# 本项目仅供学习使用，商业授权请+Q 3559331368
