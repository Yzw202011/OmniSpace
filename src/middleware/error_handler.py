"""OmniSpace AI v2.5.0 统一响应与语义化错误码（文档B §9.1 / 文档D §4.2.5 修正条款）。

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

【默认出路（批1 错误出路默认化，2026-09-11 自愈与横切内建方案）】
失败信封恒带 suggestion：调用方未传时按语义码查 SUGGESTION_DEFAULTS
自动补默认出路，未登记码兜底 SUGGESTION_FALLBACK。

【历史数字码兼容层（零信任重构登记 ADR-02）】
既有路由大量调用 error(40004, ...) 数字码。为不在契约迁移期引入回归，
本模块保留数字码→语义码映射（_LEGACY_CODE_MAP），线上输出一律为语义字符串。
新代码必须直接使用语义码，如 raise ApiError("MODEL_NOT_LOADED", ...)。
旧码段划分（40xxx通用/50xxx绘画/60xxx视频/61xxx学习/70xxx漫剧/72xxx浏览器/
80xxx系统+风格）仅作历史参照，不再对前端暴露。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import json
import re
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
    "MODEL_NOT_ALLOWED": "模型不在该功能可用范围内，请调整模型配置",
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
    "KNOWLEDGE_DELETE_FAILED": "知识删除失败（向量库不可用，知识已保留）",  # R2-H7
    "KNOWLEDGE_PROCESS_FAILED": "知识处理失败",
    "LEARN_TOPIC_NOT_FOUND": "学习主题不存在",
    "LEARN_SESSION_NOT_FOUND": "学习会话不存在",
    "LEARN_SESSION_STATE_INVALID": "当前学习会话状态不允许此操作",
    "LEARN_RESOURCE_FORBIDDEN": "当前资源状态不允许联网学习",
    "LEARN_DAILY_QUOTA_EXCEEDED": "今日学习流量已达上限",
    "LEARN_TOPIC_LIMIT": "学习主题数量已达上限",
    "LEARN_SESSION_CONFLICT": "已有学习会话进行中",
    "LEARN_BROWSER_UNAVAILABLE": "浏览器学习组件不可用",        # P1-08
    "LEARN_TRAFFIC_LIMIT": "学习流量预算耗尽",                  # P1-08
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
    "TRAINING_MAINTENANCE": "训练功能维护中，暂不接受新的训练任务",
    # ── 系统类（5xxx）────────────────────────────────────────────
    "SYSTEM_PARAM_INVALID": "参数校验失败",                    # [文档]
    "SYSTEM_UNAUTHORIZED": "未授权操作",                       # [文档]
    "SYSTEM_RESOURCE_NOT_FOUND": "资源不存在",                 # [文档]
    "SYSTEM_INTERNAL_ERROR": "内部错误，请查看服务端日志",      # [文档]
    "SYSTEM_DISK_FULL": "磁盘空间不足，请清理后重试",           # [文档]
    "SYSTEM_RATE_LIMITED": "请求过于频繁，请稍后再试",
    "LICENSE_REQUIRED": "产品尚未激活",  # P2-03（2026-10-25）：补注册
    "SYSTEM_DB_UNAVAILABLE": "数据库连接失败",
    "SYSTEM_DB_BUSY": "数据库忙，请稍后重试",
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
    "VIDEO_TASK_DUPLICATE": "该分镜已有视频在生成中",
    "UPGRADE_IN_PROGRESS": "已有升级任务在进行中，请等它完成后再发起新升级",
    "VIDEO_DURATION_EXCEEDED": "音画同步模式最长支持10秒",
    "VIDEO_RESOLUTION_DEGRADED": "当前显存不支持所选分辨率，已自动降级",
    "VIDEO_QUALITY_TIER_INVALID": "所选分辨率与时长档不匹配",
    "VIDEO_ENCODE_FAILED": "FFmpeg编码失败",
    "VIDEO_NOT_READY": "视频文件不存在或尚未生成完成",
    # ComfyUI 域语义码（round-2026-09-20 R3-P1 根修：此前 20+ 类故障
    # 共用 VIDEO_RESOLUTION_DEGRADED，出路文案宣称「已自动降级」而降级
    # 并未发生=对用户撒谎，且按码聚合的监控/自愈失真）
    "COMFY_NOT_READY": "ComfyUI 绘画/视频管线未就绪（程序或权重缺失）",
    "COMFY_PROC_EXITED": "ComfyUI 子进程异常退出",
    "COMFY_START_TIMEOUT": "ComfyUI 启动超时",
    "COMFY_UNREACHABLE": "ComfyUI 不可达（提交失败）",
    "COMFY_WORKFLOW_INVALID": "ComfyUI 工作流校验失败",
    "COMFY_FEATURE_UNSUPPORTED": "当前底座不支持该功能",
    "COMFY_VRAM_INSUFFICIENT": "ComfyUI 任务需要更多显存",
    "COMFY_NO_OUTPUT": "ComfyUI 执行完成但无产物",
    "COMFY_EXEC_FAILED": "ComfyUI 执行失败",
    "COMFY_TASK_TIMEOUT": "ComfyUI 任务超时",
    "STORYBOARD_ROW_LIMIT": "分镜表已达50行上限",
    "SCRIPT_FORMAT_UNSUPPORTED": "剧本文件格式不支持",
    "SCREENSHOT_CAMERA_MISMATCH": "4合1截图需要恰好4个机位",
    "ASSET_NOT_READY": "请先完成资产生成再进行视频生成",
    "VOICE_FILE_MISSING": "音色文件缺失",
    "VOICE_PRESET_READONLY": "预置音色不可删除",
    "VOICE_ASR_NOT_READY": "语音识别模型未就绪",
    "VOICE_AUDIO_PROCESS_FAILED": "音频处理失败",
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
    # ── 批1 错误出路默认化补登记（2026-09-11）────────────────────
    # 11 个已在业务代码 raise 但未入册的码（此前 message 落「未知错误」）
    # + 1 个 _LEGACY_CODE_MAP 遗留死码（70005），入册后 legacy 映射不再产出「未知错误」
    "MODEL_NOT_READY": "模型未就绪，请稍候或先加载模型",
    "MODEL_DOWNLOAD_OFFLINE": "离线单机定位：不提供在线模型下载",
    "LEARN_BEHAVIOR_RECORD_FAILED": "行为事件记录失败",
    "LEARN_BEHAVIOR_CLEAR_FAILED": "行为数据清理失败",
    "LEARN_TOPIC_NAME_DUPLICATED": "学习主题名称已存在",
    "NOVEL_PROJECT_NOT_FOUND": "小说项目不存在或已被删除",
    "NOVEL_CHAPTER_NOT_FOUND": "章节不存在或已被删除",
    "STYLE_TRAINING_LOCKED": "风格训练进行中，该操作被拒绝",
    "SYSTEM_DEPENDENCY_MISSING": "功能依赖的组件缺失",
    "SYSTEM_NOT_FOUND": "资源不存在",
    "COMIC_ART_STYLE_NAME_DUPLICATED": "画风名称已存在",
    "COMIC_ART_STYLE_NOT_FOUND": "画风不存在或已被删除",
    "ASSET_QUALITY_CHECK_FAILED": "资产图未通过质量校验",
    "STORYBOARD_SPLIT_EXPIRED": "分镜拆分结果已过期",
    # ── 散乱度根查 R1-A1 补登记（2026-09-20）──────────────────────
    # 此前 47 个在用码未入册（message 落「未知错误」、出路走兜底文案）；
    # 配套哨兵测试 test_error_code_registry.py 防再犯。
    "CHARACTER_LORA_KIND": "人物 LoRA 素材类型不支持",
    "CHARACTER_LORA_PARAM": "人物 LoRA 训练参数不合法",
    "CHARACTER_LORA_REJECTED": "人物 LoRA 任务被拒绝",
    "CHARACTER_LORA_ROLLBACK": "人物 LoRA 版本回退失败",
    "CHARACTER_LORA_TASK": "人物 LoRA 任务不存在或状态异常",
    "CLOUD_PROVIDER_NOT_FOUND": "云服务商配置不存在",
    "DOCX_BOMB_SUSPECTED": "文档疑似压缩炸弹，已拒绝解析",
    "H3_WORKFLOW_TEMPLATE_BROKEN": "H3 视频工作流模板损坏",
    "LICENSE_INVALID": "激活码无效",
    "LICENSE_LOCKED": "授权已被锁定",
    "MODEL_IMPORT_PATH_FORBIDDEN": "模型导入路径被安全闸拒绝",
    "NOVEL_CHAPTER_BUSY": "章节正在生成中，请稍候",
    "NOVEL_CHAPTER_EMPTY": "章节内容为空",
    "NOVEL_CHARACTER_NOT_FOUND": "角色不存在或已被删除",
    "NOVEL_CHAR_NAME_REQUIRED": "请填写角色名称",
    "NOVEL_EXPORT_FMT_INVALID": "不支持的导出格式",
    "NOVEL_FORESHADOW_DESC_REQUIRED": "请填写伏笔描述",
    "NOVEL_FORESHADOW_NOT_FOUND": "伏笔不存在或已被删除",
    "NOVEL_NAME_REQUIRED": "请填写名称",
    "NOVEL_NAME_TOO_LONG": "名称过长",
    "NOVEL_NO_CHAPTERS": "暂无章节可操作",
    "NOVEL_OUTLINE_NOT_FOUND": "大纲不存在或已被删除",
    "NOVEL_PROJECT_NAME_DUPLICATED": "小说项目名称已存在",
    "NOVEL_TASK_DUPLICATED": "同名任务已在队列中",
    "NOVEL_TASK_ID_REQUIRED": "缺少任务编号",
    "NOVEL_WORLDBUILD_TOO_SHORT": "世界观素材内容过短",
    "PARAM_INVALID": "参数不合法",
    "PLUGIN_ALREADY_REGISTERED": "插件已登记，无需重复安装",
    "PLUGIN_IMAGE_SANDBOX_UNSUPPORTED": "该插件图像能力不支持沙箱执行",
    "PLUGIN_INPUT_INVALID": "插件输入参数不合法",
    "PLUGIN_PACKAGE_INVALID": "插件包格式不合法",
    "PLUGIN_RAM_LOW": "内存不足，无法运行该插件",
    "PLUGIN_SKILL_NOT_FOUND": "插件技能不存在",
    "PLUGIN_SOURCE_CONFIRM_REQUIRED": "含源码插件需先确认授权安装",
    "PLUGIN_SOURCE_FORBIDDEN": "该来源的插件被禁止安装",
    "PLUGIN_SOURCE_INVALID": "插件来源不合法",
    "PLUGIN_SOURCE_REQUIRED": "缺少插件来源信息",
    "PLUGIN_SPEC_MISMATCH": "插件清单与包内容不符",
    "SYSTEM_IO_ERROR": "文件读写失败",
    "TRAIN_STYLE_LOCKED": "风格训练进行中，该操作被拒绝",
    "UNBIND_FAILED": "解绑失败",
    "UNBIND_NOT_CONFIRMED": "解绑需先确认",
    "UPGRADE_UPDATER_MISSING": "升级器组件缺失",
    "VLLM_NOT_READY": "vLLM 服务尚未就绪",
    "VLLM_TERMINATE_FAILED": "vLLM 服务停止失败",
    "VOICE_CLONE_FAILED": "音色克隆失败",
    "VOICE_CLONE_REF_TOO_SHORT": "参考音频过短",
    "VOICE_DB_WRITE": "音色数据写入失败",
}

# ═══════════════════════════════════════════════════════════════════
#  历史数字码 → 语义码映射（兼容层，新代码禁止新增数字码）
# ═══════════════════════════════════════════════════════════════════

# 批3（2026-09-18）数字码清偿：277 处 raise 已全量改语义串，本表退出运行时路径（_to_semantic 不再读它），
# 保留为历史对照只读档案——旧日志/外部脚本按数字码反查用；ERROR_CODES 随之冻结。
_LEGACY_CODE_MAP退休档案 = {
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
    71003: "VOICE_ASR_NOT_READY",
    71004: "VOICE_AUDIO_PROCESS_FAILED",
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


# 批3：ERROR_CODES 随 _LEGACY_CODE_MAP 一并冻结退役（历史对照）

# ═══════════════════════════════════════════════════════════════════
#  默认出路表（批1 错误出路默认化，2026-09-11 自愈与横切内建方案）
#  error() 在调用方未传 suggestion 时按语义码自动补；未登记码走
#  SUGGESTION_FALLBACK——从此任何错误信封恒带出路，「裸报错」灭绝。
#  原则：一句大白话 + 一条用户可执行出路；拿不准的给通用出路，不编具体步骤。
# ═══════════════════════════════════════════════════════════════════

#: 未登记码/未知码统一兜底出路（与方案书批1文案一致）
# R3-M17 修复（2026-10-24）：诊断包导出入口在「系统日志」页
# （LogSupportWidgets.ExportDiagnosticsDialog，/logs 路由）——原指路
# 「设置页」不存在该入口，全部未登记码的错误出路都指错方向
SUGGESTION_FALLBACK: str = "请稍后重试；若反复出现，请到系统日志页导出诊断包"

#: 语义码 -> 默认出路文案（覆盖 SEMANTIC_CODES 全量，含前端构造码）
SUGGESTION_DEFAULTS: dict[str, str] = {
    # ── 模型类 ───────────────────────────────────────────────────
    "MODEL_NOT_LOADED": "直接重试即可，系统会自动加载模型；若反复失败，请到「模型管理」页检查模型状态",
    "MODEL_LOAD_FAILED": "请先关闭其他正在运行的功能释放显存，再到「模型管理」页重试加载",
    "MODEL_NOT_ALLOWED": "请在「模型设置」或「模型管理」中改选可用模型，或清空选择改用智能路由",
    "VRAM_INSUFFICIENT": "请关闭其他 AI 功能（对话/绘画/视频同一时间只能跑一个）后重试；也可在「模型管理」页卸载暂不用的模型",
    "MODEL_INFERENCE_FAILED": "请重试一次；反复失败请到「日志」页查看原因，并在系统日志页导出诊断包",
    "MODEL_NOT_DOWNLOADED": "本软件为离线单机版，请把模型文件放入 models/ 目录后经「模型管理」页导入",
    "MODEL_FILE_NOT_FOUND": "模型文件可能被移动或删除，请到「模型管理」页确认，必要时重新导入",
    "MODEL_FILE_CORRUPTED": "该模型文件已损坏，请删除后重新获取并导入",
    "MODEL_LOADING": "模型正在加载，请等进度完成后再操作",
    "MODEL_TYPE_MISMATCH": "请确认文件类型正确（对话/绘画/视频模型不能混用）后重新选择",
    "MODEL_VERSION_INCOMPATIBLE": "请到「模型管理」页查看该模型说明，必要时换用兼容版本",
    "MODEL_NO_EVICTABLE": "请到「模型管理」页手动卸载暂不用的模型释放显存，再重试",
    "MODEL_NOT_READY": "模型还在准备中，请稍候重试；反复出现请到「模型管理」页检查",
    "MODEL_DOWNLOAD_OFFLINE": "本软件为离线单机版，请把模型文件放入 models/ 目录后经「模型管理」页导入",
    # ── 知识/学习类 ──────────────────────────────────────────────
    "KNOWLEDGE_PARSE_FAILED": "请确认文件完整且格式受支持（txt/md/pdf/docx 等），或换个文件重试",
    "KNOWLEDGE_VECTOR_TIMEOUT": "大文件处理较慢，请稍后重试；反复超时可把文件拆小后导入",
    "KNOWLEDGE_TRAINING_EMPTY": "请先导入知识资料，或放宽筛选条件后重试",
    "KNOWLEDGE_RAG_FAILED": "请重试；反复失败请到「日志」页查看原因",
    "KNOWLEDGE_NOT_FOUND": "该条目可能已被删除，请刷新列表后重试",
    "KNOWLEDGE_DELETE_FAILED": "向量库暂时不可用，知识已保留（与向量一致未删）；请稍后重试删除",
    "KNOWLEDGE_PROCESS_FAILED": "请重试一次；反复失败请换更小或更常见的文件",
    "LEARN_TOPIC_NOT_FOUND": "该主题可能已被删除，请刷新学习页列表",
    "LEARN_SESSION_NOT_FOUND": "该会话可能已结束或被清理，请重新发起学习",
    "LEARN_SESSION_STATE_INVALID": "请刷新页面查看会话最新状态后再操作",
    "LEARN_RESOURCE_FORBIDDEN": "该资源当前状态不允许此操作，请检查资源设置后重试",
    "LEARN_DAILY_QUOTA_EXCEEDED": "今日学习流量已用完，请明天再试，或改用本地资料",
    "LEARN_TOPIC_LIMIT": "请先删除不再需要的学习主题，再新建",
    "LEARN_SESSION_CONFLICT": "已有学习任务在进行，请等它完成或先停止它",
    "LEARN_BROWSER_UNAVAILABLE": "浏览器学习组件不可用，请重启软件；反复出现请导出诊断包",
    "LEARN_TRAFFIC_LIMIT": "学习流量预算已用完，请等当前会话结束或调整预算",
    "LEARN_BEHAVIOR_RECORD_FAILED": "行为记录暂未写入（不影响使用）；反复出现请导出诊断包",
    "LEARN_BEHAVIOR_CLEAR_FAILED": "清理未完成，请重试；反复失败请导出诊断包",
    "LEARN_TOPIC_NAME_DUPLICATED": "请换一个主题名称（同名主题已存在）",
    "NOVEL_PROJECT_NOT_FOUND": "该小说项目可能已被删除，请刷新列表",
    "NOVEL_CHAPTER_NOT_FOUND": "该章节可能已被删除，请刷新后重试",
    # ── 浏览器类 ─────────────────────────────────────────────────
    "BROWSER_CRASHED": "浏览器组件会自动重启，请重试刚才的操作",
    "BROWSER_POOL_EXHAUSTED": "多个浏览器任务在同时运行，请等当前任务完成后再试",
    "BROWSER_PAGE_TIMEOUT": "网页打开太慢或无响应，请检查网络后重试，或换一个网址",
    "BROWSER_NAVIGATION_FAILED": "请检查网址是否正确、网络是否可用后重试",
    "BROWSER_UNAVAILABLE": "浏览器组件启动失败，请重试；反复失败请导出诊断包",
    "BROWSER_NOT_RUNNING": "请重新发起任务，浏览器组件会自动启动",
    "BROWSER_PROTOCOL_FORBIDDEN": "请使用 http:// 或 https:// 开头的网址",
    "BROWSER_DOMAIN_BLOCKED": "该网址在安全黑名单中，请换其他网址",
    "BROWSER_OPERATION_REJECTED": "请重试；若页面有弹窗/验证码，建议手动完成该步操作",
    "BROWSER_USER_TAKEOVER": "您正在手动操作浏览器，AI 已暂停；需要 AI 继续时请重新发起任务",
    # ── 训练/风格类 ──────────────────────────────────────────────
    "TRAINING_OOM": "显存不够训练，请关闭其他 AI 功能，或按页面提示减小训练规模后重试",
    "TRAINING_LOSS_NAN": "训练素材可能有异常，请检查素材后重试",
    "TRAINING_CHECKPOINT_FAILED": "请检查磁盘剩余空间后重试",
    "TRAINING_DATA_CORRUPTED": "请检查训练素材是否完好，剔除损坏文件后重建数据集",
    "TRAINING_TASK_STATE_INVALID": "请刷新任务列表查看最新状态后再操作",
    "STYLE_BASE_NOT_READY": "请先到「模型管理」页准备风格训练基座模型",
    "STYLE_DATASET_INVALID": "风格素材不足或格式不对，请按页面提示补充素材后重试",
    "STYLE_VERSION_NOT_FOUND": "该版本可能已被删除，请刷新风格页列表",
    "STYLE_PREVIEW_UNAVAILABLE": "预览依赖的引擎未就绪，请稍候重试",
    "STYLE_TASK_NOT_FOUND": "该任务可能已被清理，请刷新列表",
    "STYLE_ASSET_FORMAT": "请改用支持的图片格式（jpg/png 等）重新上传",
    "STYLE_FRAME_EXTRACT_FAILED": "请确认视频文件完好且为常见格式（mp4/avi 等）后重试",
    "STYLE_TRAINING_LOCKED": "该画风正在训练中，请等训练完成后再操作",
    "TRAINING_MAINTENANCE": "训练功能维护中，等恢复开放后再试；已有任务与版本不受影响",
    # ── 系统类 ───────────────────────────────────────────────────
    "SYSTEM_PARAM_INVALID": "请按页面提示检查填写的内容后重试",
    "SYSTEM_UNAUTHORIZED": "请确认您有该操作的权限后重试",
    "SYSTEM_RESOURCE_NOT_FOUND": "该内容可能已被删除，请刷新后重试",
    "SYSTEM_INTERNAL_ERROR": "请稍后重试；若反复出现，请到系统日志页导出诊断包",
    "SYSTEM_DISK_FULL": "请清理磁盘空间（可删旧生成结果）后重试",
    "SYSTEM_RATE_LIMITED": "操作太频繁了，请歇几秒再试",
    "LICENSE_REQUIRED": "请把收到的激活码粘贴到激活窗口完成激活",  # P2-03
    "SYSTEM_DB_UNAVAILABLE": "数据库未响应，请重启软件；反复出现请导出诊断包",
    "SYSTEM_DB_BUSY": "系统正忙，请等几秒重试；反复出现请导出诊断包",
    "SYSTEM_DB_DEGRADED": "数据库暂时不可用，数据暂存内存；请先导出重要内容再重启软件",
    "SYSTEM_BACKUP_FAILED": "请检查磁盘剩余空间后重试备份",
    "SYSTEM_DEPENDENCY_MISSING": "该功能依赖的组件缺失，请到系统日志页导出诊断包反馈",
    "SYSTEM_NOT_FOUND": "该内容可能已被删除，请刷新后重试",
    "HARDWARE_NO_GPU": "未检测到独立显卡，AI 生成类功能受限；请确认显卡驱动安装正常",
    "HARDWARE_DRIVER_OUTDATED": "请到显卡官网更新驱动，更新后重启电脑",
    "HARDWARE_THERMAL_THROTTLE": "显卡温度过高已自动降速，请改善散热，温度回落会自动恢复",
    "HARDWARE_RAM_EXHAUSTED": "内存不足，请关闭其他大程序（浏览器多标签/游戏等）后重试",
    "FEATURE_MUTEX_LOCKED": "当前有其他 AI 功能在运行（对话/绘画/视频同一时间只能跑一个），请等它完成或先停止它",
    "OPERATION_LIMIT_EXCEEDED": "已达操作上限，请稍后再试或减少数量",
    "UNSUPPORTED_FORMAT": "请改用页面提示支持的格式后重试",
    "FILE_NOT_FOUND": "文件可能已被移动或删除，请重新选择或上传",
    "FILE_PARSE_FAILED": "文件内容无法识别，请确认文件完好且格式受支持后重试",
    "PROJECT_FILE_CORRUPTED": "项目文件损坏；若有备份请恢复备份，否则需重建该项目",
    "INPUT_TOO_LONG": "内容太长了，请删减后重试（或分段发送）",
    "CONTEXT_LIMIT_REACHED": "较早的对话会被自动截断，不影响继续使用；需要完整上下文可新建会话",
    "DIALOG_NOT_READY": "对话模型正在加载，请稍候；反复卡住请到「模型管理」页查看状态",
    # ── 绘画/视频/漫剧/语音类 ────────────────────────────────────
    "PAINT_GENERATION_FAILED": "请检查提示词后重试；反复失败请到「日志」页查看原因",
    "PAINT_SIZE_OUT_OF_RANGE": "请把图片尺寸调整到 512~2688 之间",
    "CONTROLNET_CONDITION_INVALID": "请重新上传符合要求的清晰条件图",
    "LORA_LOAD_FAILED": "该 LoRA 文件无法加载，请确认文件完好后在「模型管理」页重新导入",
    "VIDEO_GENERATION_FAILED": "请检查描述词和资产图后重试；反复失败请到「日志」页查看原因",
    "VIDEO_TASK_DUPLICATE": "该分镜已有视频在生成中，等它完成或取消后再试即可",
    "VIDEO_DURATION_EXCEEDED": "请把时长缩短到 10 秒以内",
    "VIDEO_RESOLUTION_DEGRADED": "显存不足以支撑所选分辨率，已自动降级；如需更高清可关闭其他功能后重试",
    "VIDEO_ENCODE_FAILED": "视频合成失败，请重试；反复失败请导出诊断包",
    "VIDEO_NOT_READY": "视频尚未生成完成，请在任务列表等待完成后再查看",
    "VIDEO_QUALITY_TIER_INVALID": "请从确认弹窗的分档下拉里选择该时长支持的分辨率",
    "COMFY_NOT_READY": "请确认 ComfyUI 便携版与模型权重完整（模型管理页可查缺失项）",
    "COMFY_PROC_EXITED": "ComfyUI 进程异常退出，稍后会自动重启；请重试一次，反复出现请导出诊断包",
    "COMFY_START_TIMEOUT": "ComfyUI 冷启动较慢，请稍等片刻后重试；反复超时请重启应用",
    "COMFY_UNREACHABLE": "ComfyUI 服务不可达，请稍后重试；反复出现请重启应用",
    "COMFY_WORKFLOW_INVALID": "工作流被 ComfyUI 拒绝（多为模型/节点缺失），请到模型管理页核对后重试",
    "COMFY_FEATURE_UNSUPPORTED": "当前所选底座不支持该功能，请按提示切换底座后重试",
    "COMFY_VRAM_INSUFFICIENT": "空闲显存不足，请关闭其他生成任务或切换更小的底座后重试",
    "COMFY_NO_OUTPUT": "任务执行完成但没有产物，请重试；反复出现请导出诊断包",
    "COMFY_EXEC_FAILED": "ComfyUI 执行失败，请重试；详情见「日志」页",
    "COMFY_TASK_TIMEOUT": "任务超时已自动叫停 ComfyUI；请重试或降低分辨率/步数",
    "STORYBOARD_ROW_LIMIT": "分镜表最多 50 行，请新建项目或精简分镜",
    "STORYBOARD_SPLIT_EXPIRED": "分镜拆分结果已过期，请重新执行 AI 拆分",
    "SCRIPT_FORMAT_UNSUPPORTED": "请改用支持的剧本格式（txt/md/docx）",
    "SCREENSHOT_CAMERA_MISMATCH": "4合1截图需要恰好 4 个机位，请调整机位数量后重试",
    "ASSET_NOT_READY": "请先生成并绑定该镜头的资产图，再发起视频生成",
    "ASSET_QUALITY_CHECK_FAILED": "资产图质量校验未通过，请重试生成；反复失败请到「日志」页查看原因",
    "VOICE_FILE_MISSING": "该音色的参考文件缺失，请重新上传或改用其他音色",
    "VOICE_PRESET_READONLY": "预置音色是系统自带的不能删除；您自建的音色可以删除",
    "VOICE_ASR_NOT_READY": "把 whisper 模型目录放入 models/ 后重试",
    "VOICE_AUDIO_PROCESS_FAILED": "请检查音频文件格式与 FFmpeg 是否可用",
    "VOICE_CLONE_UNAVAILABLE": "音色克隆组件未随包安装，请改用预置音色或标准语音合成",
    "PAINT_ENGINE_NOT_READY": "绘画引擎正在准备，请稍候；反复未就绪请到「模型管理」页检查绘画模型",
    "COMIC_PROJECT_NAME_DUPLICATED": "请换一个项目名称（同名项目已存在）",
    "COMIC_ART_STYLE_NAME_DUPLICATED": "请换一个画风名称（同名画风已存在）",
    "COMIC_ART_STYLE_NOT_FOUND": "该画风可能已被删除，请刷新画风列表",
    "COMIC_DSL_FORMAT_INVALID": "请检查剧本格式：每个分镜需以 shot: 开头，可参考页面示例",
    # ── 前端类（前端构造，后端不下发；入册保证全码覆盖）─────────
    "FRONTEND_RENDER_ERROR": "页面显示异常，请刷新页面（Ctrl+F5）；反复出现请导出诊断包",
    "FRONTEND_NETWORK_ERROR": "与服务断开连接，请确认软件正在运行后刷新页面",
    "FRONTEND_STATE_INVALID": "页面状态异常，请刷新页面重试",
    "FRONTEND_WEBSOCKET_DISCONNECTED": "实时连接断开，通常会自动恢复；长时间未恢复请刷新页面",
    "FRONTEND_PARSE_ERROR": "数据解析失败，请重试；反复出现请导出诊断包",
    "FRONTEND_REQUEST_ABORTED": "请求已取消，如需继续请重新操作",
    # ── 散乱度根查 R1-A1 补登记出路（2026-09-20，与 SEMANTIC_CODES 同批）──
    "CHARACTER_LORA_KIND": "请使用支持的角色立绘/参考图素材类型后重试",
    "CHARACTER_LORA_PARAM": "请检查训练参数范围后重新提交",
    "CHARACTER_LORA_REJECTED": "请查看任务详情中的拒绝原因，调整后重试",
    "CHARACTER_LORA_ROLLBACK": "请刷新版本列表后重试回退；反复失败请导出诊断包",
    "CHARACTER_LORA_TASK": "请刷新任务列表；任务可能已完成或被删除",
    "CLOUD_PROVIDER_NOT_FOUND": "请到「设置→云端 API」检查服务商配置",
    "DOCX_BOMB_SUSPECTED": "该文档展开后异常庞大，已拒绝对接；请检查文件来源",
    "H3_WORKFLOW_TEMPLATE_BROKEN": "请重启应用让模板自愈；仍失败请重新安装",
    "LICENSE_INVALID": "请核对激活码后重试；确认无误请联系发行方",
    "LICENSE_LOCKED": "该机器授权已锁定，请联系发行方解绑或更换激活码",
    "MODEL_IMPORT_PATH_FORBIDDEN": "请把模型放入 models/ 目录后从「模型管理」页导入",
    "NOVEL_CHAPTER_BUSY": "请等待当前生成完成后再操作该章节",
    "NOVEL_CHAPTER_EMPTY": "请先生成或编辑章节内容",
    "NOVEL_CHARACTER_NOT_FOUND": "请刷新角色列表；角色可能已被删除",
    "NOVEL_CHAR_NAME_REQUIRED": "请填写角色名称后再保存",
    "NOVEL_EXPORT_FMT_INVALID": "请选择支持的导出格式（txt/md）",
    "NOVEL_FORESHADOW_DESC_REQUIRED": "请补充伏笔描述后再保存",
    "NOVEL_FORESHADOW_NOT_FOUND": "请刷新伏笔列表；该伏笔可能已被删除",
    "NOVEL_NAME_REQUIRED": "请填写名称后再保存",
    "NOVEL_NAME_TOO_LONG": "请缩短名称后重试",
    "NOVEL_NO_CHAPTERS": "请先创建或生成章节",
    "NOVEL_OUTLINE_NOT_FOUND": "请先生成大纲",
    "NOVEL_PROJECT_NAME_DUPLICATED": "请换一个作品名称（同名作品已存在）",
    "NOVEL_TASK_DUPLICATED": "同名任务正在处理，请稍候",
    "NOVEL_TASK_ID_REQUIRED": "请从任务列表中选择任务后操作",
    "NOVEL_WORLDBUILD_TOO_SHORT": "世界观素材需不少于 20 字，请补充后重试",
    "PARAM_INVALID": "请检查输入内容后重试",
    "PLUGIN_ALREADY_REGISTERED": "如需更新请先卸载旧版本再安装",
    "PLUGIN_IMAGE_SANDBOX_UNSUPPORTED": "请在信任环境下运行该插件的图像能力",
    "PLUGIN_INPUT_INVALID": "请按插件说明检查输入后重试",
    "PLUGIN_PACKAGE_INVALID": "请重新下载或选择正确的 .CuteMamen 插件包",
    "PLUGIN_RAM_LOW": "请关闭其他占用内存的功能后重试",
    "PLUGIN_SKILL_NOT_FOUND": "请检查插件是否已启用、技能名是否正确",
    "PLUGIN_SOURCE_CONFIRM_REQUIRED": "请在确认弹窗中勾选信任该插件源后重试",
    "PLUGIN_SOURCE_FORBIDDEN": "该来源不在允许清单内，无法安装",
    "PLUGIN_SOURCE_INVALID": "请提供合法的插件来源（内置包或本地文件）",
    "PLUGIN_SOURCE_REQUIRED": "请补充插件来源信息后重试",
    "PLUGIN_SPEC_MISMATCH": "插件包与清单不一致，请重新获取完整包",
    "SYSTEM_IO_ERROR": "请检查磁盘空间与文件占用后重试",
    "TRAIN_STYLE_LOCKED": "请等待风格训练完成后再操作",
    "UNBIND_FAILED": "请稍后重试解绑；反复失败请导出诊断包",
    "UNBIND_NOT_CONFIRMED": "请在确认弹窗中完成解绑确认",
    "UPGRADE_UPDATER_MISSING": "升级器缺失，请重新安装完整版本",
    "UPGRADE_IN_PROGRESS": "升级正在进行中，请等它完成后再操作（可稍后刷新查看进度）",
    "VLLM_NOT_READY": "对话引擎正在启动，请稍候重试",
    "VLLM_TERMINATE_FAILED": "请运行 停止OmniSpace.bat 完整退出后重试",
    "VOICE_CLONE_FAILED": "请更换更清晰的 5-10 秒人声参考后重试",
    "VOICE_CLONE_REF_TOO_SHORT": "请上传 5-10 秒的清晰人声参考音频",
    "VOICE_DB_WRITE": "请重试；反复失败请导出诊断包",
}


def _to_semantic(code: int | str) -> str:
    """语义码归一（批3 数字码清偿后唯一形态=str）。

    int 输入=清偿后不应再出现的信号——保守兜底 SYSTEM_INTERNAL_ERROR
    （行为与旧翻译等价：未映射数字码本来就落到它）。"""
    if isinstance(code, str):
        return code
    return "SYSTEM_INTERNAL_ERROR"


def _default_message(code: int | str) -> str:
    sem = _to_semantic(code)
    return SEMANTIC_CODES.get(sem, "未知错误")


# B8-e（2026-09-14）：语义码→HTTP 状态映射（meta.http_status 用，信封
# HTTP 层仍恒 200——外部监控凭 meta 区分真实成功与语义失败）。
# 仅列已确认映射，未命中兜底 200（语义失败按信封语义处理）。
_SEMANTIC_HTTP: dict[str, int] = {
    "SYSTEM_PARAM_INVALID": 400,
    "SYSTEM_RESOURCE_NOT_FOUND": 404,
    "LICENSE_LOCKED": 429,
    "SYSTEM_RATE_LIMITED": 429,  # P2-03 修复（2026-10-25）：死键
    # RATE_LIMITED→SYSTEM_RATE_LIMITED（前者零使用）
    "FEATURE_MUTEX_LOCKED": 409,
    "TRAINING_MAINTENANCE": 503,
    "LICENSE_REQUIRED": 403,
    "SYSTEM_INTERNAL_ERROR": 500,
}


def _meta() -> dict[str, Any]:
    """统一 meta 三元组（文档D：request_id / timestamp / duration_ms）。"""
    return {
        "request_id": current_request_id(),
        "timestamp": utc_now_iso(),
        "duration_ms": current_duration_ms(),
    }


# R3-L12（202-24）：错误消息路径脱敏——str(exc) 常含
# E:\OmniSpace\src\... 或 C:\Users\...\Temp\... 内部路径直达前端
_PATH_RE = re.compile(
    r'[A-Za-z]:\\[^\s"\']{5,}|/(?:home|usr|tmp|var)/[^\s"\']{5,}')


def _sanitize_msg(msg: str) -> str:
    """剥离错误消息中的文件系统路径（防内部结构泄漏）。"""
    if not msg:
        return msg
    return _PATH_RE.sub('[路径]', msg[:500])


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
        self.message = _sanitize_msg(message or _default_message(code))
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
    批1 错误出路默认化：suggestion 未传时按语义码查 SUGGESTION_DEFAULTS
    自动补默认出路，未登记码兜底 SUGGESTION_FALLBACK——信封恒带出路。
    """
    sem = _to_semantic(code)
    msg = message or SEMANTIC_CODES.get(sem, "未知错误")
    sug = suggestion or SUGGESTION_DEFAULTS.get(sem, SUGGESTION_FALLBACK)
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
        "suggestion": sug,
    }
    body: dict[str, Any] = {
        "success": False,
        "data": None,
        "error": err_obj,
        "meta": {**_meta(), "http_status": _SEMANTIC_HTTP.get(sem, 200)},
    }
    # 批2 P31（2026-09-19）：失败信封喂舱壁账本——同舱连续 3 次失败
    # → degraded + 触发自动自愈（module_health 内部防抖；记账失败
    # 绝不影响错误响应本身）
    try:
        from ..services.module_health import record_failure
        record_failure(sem, msg)
    except Exception:  # noqa: BLE001 - 账本不可用不阻断错误链
        pass
    # B8-e（2026-09-14）：监控解盲——meta.http_status 携带语义码对应的
    # 真实 HTTP 状态（信封 HTTP 层仍恒 200，前端零改动；外部监控/网关
    # 可凭此字段区分真实成功与语义失败，消除恒 200 信封的监控盲区）。
    return JSONResponse(status_code=200, content=body)
