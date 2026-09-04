"""OmniSpace AI v2.3.1 统一响应与语义化错误码（文档B §9.1 / 文档D §4.2.5 修正条款）。

【权威契约——四文档一致，禁止偏移】
成功响应：
    {"success": true, "data": {...}, "error": null,
     "meta": {"request_id": "...", "timestamp": "...", "duration_ms": 42}}
失败响应：
    {"success": false, "data": null,
     "error": {"code": "MODEL_NOT_LOADED", "message": "...", "detail": "...",
               "suggestion": "..."},
     "meta": {"request_id": "...", "timestamp": "...", "duration_ms": 15}}

错误码体系（文档D 修正版：字符串语义化，按模块 6 类）：
  - 模型   MODEL_*  / VRAM_INSUFFICIENT
  - 知识   KNOWLEDGE_* / LEARN_*
  - 浏览器 BROWSER_*
  - 训练   TRAINING_* / STYLE_*（视频风格训练）
  - 系统   SYSTEM_* / HARDWARE_* / FEATURE_* / FILE_* / PROJECT_*
  - 前端   FRONTEND_*（前端 api.ts 构造，后端不下发）

【历史数字码兼容层（零信任重构登记 ADR-02）】
既有路由大量调用 error(40004, ...) 数字码。为不在契约迁移期引入回归，
本模块保留数字码→语义码映射（_LEGACY_CODE_MAP），线上输出一律为语义字符串。
新代码必须直接使用语义码，如 raise ApiError("MODEL_NOT_LOADED", ...)。
旧码段划分（40xxx通用/50xxx绘画/60xxx视频/61xxx学习/70xxx漫剧/72xxx浏览器/
80xxx系统+风格）仅作历史参照，不再对前端暴露。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi.responses import JSONResponse

from .request_context import current_duration_ms, current_request_id, utc_now_iso

# ═══════════════════════════════════════════════════════════════════
#  语义化错误码目录（文档D 6类体系；含文档明列 24 码 + 本项目扩展码）
# ═══════════════════════════════════════════════════════════════════

#: 语义码 -> 默认中文文案。文档明列码注释标注 [文档]。
SEMANTIC_CODES: dict[str, str] = {
    # ── 模型类（1xxx）────────────────────────────────────────────
    "MODEL_NOT_LOADED": "模型未加载到显存",                    # [文档]
    "MODEL_LOAD_FAILED": "模型加载失败，请检查显存是否充足",     # [文档]
    "VRAM_INSUFFICIENT": "显存不足",                           # [文档]
    "MODEL_INFERENCE_FAILED": "模型推理失败",                  # [文档]
    "MODEL_NOT_DOWNLOADED": "模型未下载",
    "MODEL_FILE_NOT_FOUND": "模型文件未找到，请导入模型",
    "MODEL_FILE_CORRUPTED": "模型文件损坏，请重新获取",
    "MODEL_LOADING": "模型加载中，请稍候",
    "MODEL_TYPE_MISMATCH": "模型类型不匹配，请选择正确的模型文件",
    "MODEL_VERSION_INCOMPATIBLE": "模型版本不兼容",
    "MODEL_NO_EVICTABLE": "显存不足且无可驱逐模型",
    # ── 知识类（2xxx）────────────────────────────────────────────
    "KNOWLEDGE_PARSE_FAILED": "知识解析失败",                  # [文档]
    "KNOWLEDGE_VECTOR_TIMEOUT": "知识向量化超时",              # [文档]
    "KNOWLEDGE_TRAINING_EMPTY": "训练数据为空",                # [文档]
    "KNOWLEDGE_RAG_FAILED": "知识检索失败",                    # [文档]
    "KNOWLEDGE_NOT_FOUND": "知识不存在",
    "KNOWLEDGE_PROCESS_FAILED": "知识处理失败",
    "LEARN_TOPIC_NOT_FOUND": "学习主题不存在",
    "LEARN_SESSION_NOT_FOUND": "学习会话不存在",
    "LEARN_SESSION_STATE_INVALID": "当前学习会话状态不允许此操作",
    "LEARN_RESOURCE_FORBIDDEN": "当前资源状态不允许联网学习",
    "LEARN_DAILY_QUOTA_EXCEEDED": "今日学习流量已达上限",
    "LEARN_TOPIC_LIMIT": "学习主题数量已达上限",
    "LEARN_SESSION_CONFLICT": "已有学习会话进行中",
    # ── 浏览器类（3xxx）──────────────────────────────────────────
    "BROWSER_CRASHED": "浏览器进程崩溃",                       # [文档]
    "BROWSER_POOL_EXHAUSTED": "无可用浏览器实例",              # [文档]
    "BROWSER_PAGE_TIMEOUT": "页面操作超时",                    # [文档]
    "BROWSER_NAVIGATION_FAILED": "页面导航失败",               # [文档]
    "BROWSER_UNAVAILABLE": "浏览器组件不可用",
    "BROWSER_NOT_RUNNING": "浏览器未运行",
    "BROWSER_PROTOCOL_FORBIDDEN": "仅允许访问 HTTP/HTTPS 协议地址",
    "BROWSER_DOMAIN_BLOCKED": "目标域名在黑名单中，已阻止访问",
    "BROWSER_OPERATION_REJECTED": "页面操作失败或被安全策略拒绝",
    "BROWSER_USER_TAKEOVER": "用户已接管浏览器，AI控制已暂停",
    # ── 训练类（4xxx）────────────────────────────────────────────
    "TRAINING_OOM": "训练显存溢出",                            # [文档]
    "TRAINING_LOSS_NAN": "训练 Loss 出现 NaN",                 # [文档]
    "TRAINING_CHECKPOINT_FAILED": "训练检查点保存失败",         # [文档]
    "TRAINING_DATA_CORRUPTED": "训练数据损坏",                 # [文档]
    # 审计 R3-BE1：训练任务取消的状态语义码
    "TRAINING_TASK_STATE_INVALID": "当前训练任务状态不允许此操作",
    "STYLE_BASE_NOT_READY": "风格训练基座未就绪（LTX-2 权重未下载）",
    "STYLE_DATASET_INVALID": "风格训练数据集无效或样本不足",
    "STYLE_VERSION_NOT_FOUND": "风格 LoRA 版本不存在",
    "STYLE_PREVIEW_UNAVAILABLE": "风格预览不可用（推理引擎或版本未就绪）",
    "STYLE_TASK_NOT_FOUND": "风格训练任务不存在",
    "STYLE_ASSET_FORMAT": "风格素材格式不支持",
    "STYLE_FRAME_EXTRACT_FAILED": "视频抽帧失败（FFmpeg 不可用或文件损坏）",
    # ── 系统类（5xxx）────────────────────────────────────────────
    "SYSTEM_PARAM_INVALID": "参数校验失败",                    # [文档]
    "SYSTEM_UNAUTHORIZED": "未授权操作",                       # [文档]
    "SYSTEM_RESOURCE_NOT_FOUND": "资源不存在",                 # [文档]
    "SYSTEM_INTERNAL_ERROR": "内部错误，请查看服务端日志",      # [文档]
    "SYSTEM_DISK_FULL": "磁盘空间不足，请清理后重试",           # [文档]
    "SYSTEM_RATE_LIMITED": "请求过于频繁，请稍后再试",
    "SYSTEM_DB_UNAVAILABLE": "数据库连接失败",
    "SYSTEM_DB_DEGRADED": "数据库不可用，已降级到内存模式",
    "SYSTEM_BACKUP_FAILED": "备份失败，请检查磁盘空间",
    "HARDWARE_NO_GPU": "未检测到独立显卡，部分功能可能受限",
    "HARDWARE_DRIVER_OUTDATED": "显卡驱动版本过低，请更新到最新版本",
    "HARDWARE_THERMAL_THROTTLE": "GPU温度过高，已降低运行速度",
    "HARDWARE_RAM_EXHAUSTED": "内存不足，建议关闭其他程序",
    "FEATURE_MUTEX_LOCKED": "功能互斥，当前已有其他AI功能运行中",
    "OPERATION_LIMIT_EXCEEDED": "操作超出上限",
    "UNSUPPORTED_FORMAT": "不支持的格式或枚举值",
    "FILE_NOT_FOUND": "文件未找到",
    "FILE_PARSE_FAILED": "文件解析失败",
    "PROJECT_FILE_CORRUPTED": "项目文件损坏",
    "INPUT_TOO_LONG": "输入内容过长，请缩短后重试",
    "CONTEXT_LIMIT_REACHED": "对话上下文已达上限，将自动截断早期内容",
    "DIALOG_NOT_READY": "对话服务未就绪，请等待模型加载完成",
    # ── 绘画/视频/漫剧/音色（按模块归系统扩展段）──────────────────
    "PAINT_GENERATION_FAILED": "生成失败，请检查提示词是否为空",
    "PAINT_SIZE_OUT_OF_RANGE": "图片尺寸超出范围（512~2688）",
    "CONTROLNET_CONDITION_INVALID": "ControlNet条件图格式错误",
    "LORA_LOAD_FAILED": "LoRA文件加载失败",
    "VIDEO_GENERATION_FAILED": "视频生成失败，请检查输入参数",
    "VIDEO_DURATION_EXCEEDED": "音画同步模式最长支持10秒",
    "VIDEO_RESOLUTION_DEGRADED": "当前显存不支持所选分辨率，已自动降级",
    "VIDEO_ENCODE_FAILED": "FFmpeg编码失败",
    "STORYBOARD_ROW_LIMIT": "分镜表已达50行上限",
    "SCRIPT_FORMAT_UNSUPPORTED": "剧本文件格式不支持",
    "SCREENSHOT_CAMERA_MISMATCH": "4合1截图需要恰好4个机位",
    "ASSET_NOT_READY": "请先完成资产生成再进行视频生成",
    "VOICE_FILE_MISSING": "音色文件缺失",
    "VOICE_PRESET_READONLY": "预置音色不可删除",
    # 修复任务清单 批 1：漫剧模块新增语义码
    "COMIC_PROJECT_NAME_DUPLICATED": "项目名称已存在，请更换名称",
    "COMIC_DSL_FORMAT_INVALID": "DSL 剧本格式无效（缺少 shot: 分镜标记）",
    "VOICE_CLONE_UNAVAILABLE": "音色克隆不可用（GPT-SoVITS 推理代码包未随包）",
    "PAINT_ENGINE_NOT_READY": "绘画引擎未就绪，请稍候或检查模型加载",
    # ── 前端类（6xxx，前端构造，后端不下发）──────────────────────
    "FRONTEND_RENDER_ERROR": "前端渲染错误",                   # [文档]
    "FRONTEND_NETWORK_ERROR": "前端网络错误",                  # [文档]
    "FRONTEND_STATE_INVALID": "前端状态无效",                  # [文档]
    "FRONTEND_WEBSOCKET_DISCONNECTED": "WebSocket 连接断开",   # [文档]
    "FRONTEND_PARSE_ERROR": "响应解析失败",
    "FRONTEND_REQUEST_ABORTED": "请求已取消",
}

# ═══════════════════════════════════════════════════════════════════
#  历史数字码 → 语义码映射（兼容层，新代码禁止新增数字码）
# ═══════════════════════════════════════════════════════════════════

_LEGACY_CODE_MAP: dict[int, str] = {
    0: "OK",
    10001: "SYSTEM_PARAM_INVALID",
    10002: "SYSTEM_RATE_LIMITED",
    10003: "SYSTEM_INTERNAL_ERROR",
    99999: "SYSTEM_INTERNAL_ERROR",
    # 硬件 20xxx
    20001: "HARDWARE_NO_GPU",
    20002: "HARDWARE_DRIVER_OUTDATED",
    20003: "VRAM_INSUFFICIENT",
    20004: "HARDWARE_THERMAL_THROTTLE",
    20005: "HARDWARE_RAM_EXHAUSTED",
    20006: "SYSTEM_DISK_FULL",
    20010: "MODEL_LOAD_FAILED",
    20011: "MODEL_NOT_DOWNLOADED",
    20012: "MODEL_NOT_LOADED",
    20013: "MODEL_NO_EVICTABLE",
    20014: "FEATURE_MUTEX_LOCKED",
    # 模型 30xxx
    30001: "MODEL_FILE_NOT_FOUND",
    30002: "MODEL_FILE_CORRUPTED",
    30003: "MODEL_LOADING",
    30004: "MODEL_LOAD_FAILED",
    30005: "MODEL_TYPE_MISMATCH",
    30006: "MODEL_VERSION_INCOMPATIBLE",
    # 对话/API 40xxx
    40001: "DIALOG_NOT_READY",
    40002: "INPUT_TOO_LONG",
    40003: "CONTEXT_LIMIT_REACHED",
    40004: "SYSTEM_PARAM_INVALID",
    40005: "SYSTEM_RESOURCE_NOT_FOUND",
    40006: "SYSTEM_INTERNAL_ERROR",
    40007: "FEATURE_MUTEX_LOCKED",
    40008: "SYSTEM_PARAM_INVALID",
    40009: "OPERATION_LIMIT_EXCEEDED",
    40010: "UNSUPPORTED_FORMAT",
    40011: "SYSTEM_DB_DEGRADED",
    40012: "SYSTEM_RATE_LIMITED",
    40013: "SYSTEM_UNAUTHORIZED",
    40014: "FILE_NOT_FOUND",
    40015: "FILE_PARSE_FAILED",
    # 绘画 50xxx
    50001: "PAINT_GENERATION_FAILED",
    50002: "PAINT_SIZE_OUT_OF_RANGE",
    50003: "CONTROLNET_CONDITION_INVALID",
    50004: "LORA_LOAD_FAILED",
    # 视频 60xxx
    60001: "VIDEO_GENERATION_FAILED",
    60002: "VIDEO_DURATION_EXCEEDED",
    60003: "VIDEO_RESOLUTION_DEGRADED",
    60004: "VIDEO_ENCODE_FAILED",
    # 学习 61xxx
    61001: "LEARN_TOPIC_NOT_FOUND",
    61002: "LEARN_SESSION_NOT_FOUND",
    61003: "LEARN_SESSION_STATE_INVALID",
    61004: "BROWSER_UNAVAILABLE",
    61005: "LEARN_RESOURCE_FORBIDDEN",
    61006: "LEARN_DAILY_QUOTA_EXCEEDED",
    61007: "LEARN_TOPIC_LIMIT",
    61008: "LEARN_SESSION_CONFLICT",
    61009: "KNOWLEDGE_NOT_FOUND",
    # 漫剧 70xxx / 音色 71xxx / 浏览器 72xxx
    70001: "STORYBOARD_ROW_LIMIT",
    70002: "SCRIPT_FORMAT_UNSUPPORTED",
    70003: "SCREENSHOT_CAMERA_MISMATCH",
    70004: "ASSET_NOT_READY",
    70005: "STORYBOARD_SPLIT_EXPIRED",
    71001: "VOICE_FILE_MISSING",
    71002: "VOICE_PRESET_READONLY",
    72001: "BROWSER_UNAVAILABLE",
    72002: "BROWSER_NOT_RUNNING",
    72003: "BROWSER_POOL_EXHAUSTED",
    72004: "BROWSER_PROTOCOL_FORBIDDEN",
    72005: "BROWSER_DOMAIN_BLOCKED",
    72006: "BROWSER_OPERATION_REJECTED",
    72007: "BROWSER_USER_TAKEOVER",
    72008: "BROWSER_PAGE_TIMEOUT",
    # 系统 80xxx（历史）/ 风格 8001x
    80001: "SYSTEM_DB_UNAVAILABLE",
    80002: "SYSTEM_BACKUP_FAILED",
    80003: "PROJECT_FILE_CORRUPTED",
    80010: "STYLE_BASE_NOT_READY",
    80011: "STYLE_DATASET_INVALID",
    80012: "STYLE_VERSION_NOT_FOUND",
    80013: "STYLE_PREVIEW_UNAVAILABLE",
    80014: "STYLE_TASK_NOT_FOUND",
    80015: "STYLE_ASSET_FORMAT",
    80016: "STYLE_FRAME_EXTRACT_FAILED",
}

#: 数字码默认文案（与历史 ERROR_CODES 一致，经映射后供语义码查默认文案之外的备用）
ERROR_CODES: dict[int, str] = {code: SEMANTIC_CODES[sem] for code, sem in _LEGACY_CODE_MAP.items() if sem in SEMANTIC_CODES}


def _to_semantic(code: int | str) -> str:
    """数字码/语义码统一归一为语义码。未知输入兜底 SYSTEM_INTERNAL_ERROR。"""
    if isinstance(code, str):
        return code if code in SEMANTIC_CODES else code  # 允许新语义码直接使用
    return _LEGACY_CODE_MAP.get(code, "SYSTEM_INTERNAL_ERROR")


def _default_message(code: int | str) -> str:
    sem = _to_semantic(code)
    return SEMANTIC_CODES.get(sem, "未知错误")


def _meta() -> dict[str, Any]:
    """统一 meta 三元组（文档D：request_id / timestamp / duration_ms）。"""
    return {
        "request_id": current_request_id(),
        "timestamp": utc_now_iso(),
        "duration_ms": current_duration_ms(),
    }


class ApiError(Exception):
    """业务异常：被异常处理器捕获后转为文档D统一失败信封。

    code 接受语义字符串（新代码必须）或历史数字码（兼容层自动映射）。

    审计 R3-BE5：http_status 保留字段已移除——统一 200 信封设计
    （ADR-01）下全局异常处理器恒走 error() 返回 200，该参数无读取方；
    仅中间件层（403/429 限流/CORS）例外使用真实 HTTP 状态码。
    """

    def __init__(self, code: int | str, message: str = "",
                 detail: Any = None, suggestion: str = "") -> None:
        self.code = _to_semantic(code)
        self.message = message or _default_message(code)
        self.detail = detail
        self.suggestion = suggestion
        super().__init__(self.message)


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    """成功响应（文档D）：{success:true, data, error:null, meta:{...}}"""
    return {"success": True, "data": data, "error": None, "meta": _meta()}


def error(code: int | str, message: str = "", detail: Any = None,
          suggestion: str = "") -> JSONResponse:
    """失败响应（文档D）：{success:false, data:null, error:{...}, meta:{...}}

    detail 按文档定义为字符串；传入 dict/list 时序列化为 JSON 字符串
    （保留排障信息且不破坏前端 Zod schema 校验）。
    """
    sem = _to_semantic(code)
    msg = message or SEMANTIC_CODES.get(sem, "未知错误")
    if detail is None:
        detail_str = ""
    elif isinstance(detail, str):
        detail_str = detail
    else:
        detail_str = json.dumps(detail, ensure_ascii=False)
    err_obj: dict[str, Any] = {
        "code": sem,
        "message": msg,
        "detail": detail_str,
    }
    if suggestion:
        err_obj["suggestion"] = suggestion
    body: dict[str, Any] = {
        "success": False,
        "data": None,
        "error": err_obj,
        "meta": _meta(),
    }
    return JSONResponse(status_code=200, content=body)
