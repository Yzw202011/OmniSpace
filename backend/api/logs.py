"""系统日志 API（2026-08-21 日志可视化裁定：统一大白话 + 30 天保留）。

端点清单（/api/v1 前缀由 main.py 挂载）：
- GET  /logs/events      重要事件分页查询（级别/模块/关键词过滤，新→旧）
- GET  /logs/flows       功能执行流程聚合（流程可视化面板，2026-08-22）
- GET  /logs/flows/trace 执行流程追踪列表（精确 flow_id 链路，2026-08-23）
- GET  /logs/flows/trace/{flow_id} 单流程全明细（节点链/输入输出/资源/卡住分析）
- GET  /logs/flows/trace-stats 追踪筛选项与状态计数（含 stalled 卡住）
- GET  /logs/stats       统计（级别分布/模块分布/24h 趋势/保留策略）
- GET  /logs/modules     已产生事件的模块列表（筛选项）
- GET  /logs/files       原始日志文件清单（名称/大小/修改时间）
- GET  /logs/raw         原始日志尾部（tail N 行，技术诊断）
- POST /logs/cleanup     手动触发过期清理（30 天规则）

事件来源：backend/services/event_log.py（log_event 统一入口，
各重要模块埋点：模型加载/热切换/vLLM 生命周期/模块资源释放/训练/异常）。
流程追踪来源：backend/services/flow_trace.py（flow_id 贯穿 API→调度→
引擎全链路，节点级输入输出/状态/耗时/资源快照）。
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Query

from ..config import LOGS_DIR
from ..middleware.error_handler import ApiError, ok
from ..services import flow_trace
from ..services.event_log import (
    RETENTION_DAYS,
    cleanup_expired,
    event_stats,
    list_modules,
    query_events,
    query_flows,
)

log = logging.getLogger("omnispace.api.logs")

router = APIRouter()

# 原始日志白名单（tail 仅允许项目自有日志，防任意文件读取）
_RAW_LOG_FILES = {
    "backend.log": LOGS_DIR / "backend.log",
    "error.log": LOGS_DIR / "error.log",
    "vllm-server.log": LOGS_DIR / "vllm-server.log",
}


@router.get("/logs/events")
def logs_events(
    limit: int = Query(200, ge=1, le=1000, description="每页条数"),
    offset: int = Query(0, ge=0, description="跳过条数（新→旧游标）"),
    days: int = Query(7, ge=1, le=30, description="回溯天数"),
    level: str | None = Query(None, description="级别过滤 info/success/warning/error"),
    module: str | None = Query(None, description="模块过滤"),
    keyword: str = Query("", description="关键词（大白话/详情/事件名）"),
):
    """重要事件分页查询（新→旧，大白话时间线）。"""
    if level and level not in ("info", "success", "warning", "error"):
        raise ApiError("SYSTEM_PARAM_INVALID", f"非法级别: {level}")
    data = query_events(limit=limit, offset=offset, days=days,
                        level=level, module=module, keyword=keyword)
    return ok(data)


@router.get("/logs/flows")
def logs_flows(
    days: int = Query(1, ge=1, le=30, description="回溯天数（流程面板默认看最近 1 天）"),
    level: str | None = Query(None, description="级别过滤（流程含该级别事件）"),
    module: str | None = Query(None, description="模块过滤（流程涉及该模块）"),
    keyword: str = Query("", description="关键词（流程内任一事件命中）"),
    limit: int = Query(50, ge=1, le=200, description="返回流程数上限"),
):
    """功能执行流程聚合（执行流程可视化面板数据源）。

    把事件日志按时间邻近性聚合成「一次功能执行的完整链路」（跨模块
    交互、每步状态/耗时），兼容无 trace_id 的全部历史数据。
    """
    if level and level not in ("info", "success", "warning", "error"):
        raise ApiError("SYSTEM_PARAM_INVALID", f"非法级别: {level}")
    data = query_flows(days=days, level=level, module=module,
                       keyword=keyword, limit=limit)
    return ok(data)


@router.get("/logs/flows/trace")
def logs_flow_traces(
    module: str | None = Query(None, description="模块过滤（paint/dialog/video/learn/...）"),
    feature: str | None = Query(None, description="功能过滤（txt2img/chat/i2v/session...）"),
    status: str | None = Query(
        None,
        description="状态过滤 running/success/error/cancelled/stalled/orphan；"
                    "stalled=卡住（running 且无进度心跳超阈值）"),
    keyword: str = Query("", description="关键词（功能描述/输入输出/错误详情）"),
    start: float | None = Query(None, description="起始 Unix 时间戳（秒）"),
    end: float | None = Query(None, description="结束 Unix 时间戳（秒）"),
    limit: int = Query(50, ge=1, le=200, description="返回流程数上限"),
    offset: int = Query(0, ge=0, description="跳过条数（新→旧游标）"),
):
    """执行流程追踪列表（精确 flow_id 链路，2026-08-23 流程记录机制优化）。

    与 /logs/flows（事件启发式聚合）互补：本端点读 flow_trace.db 精确
    记录——每次功能执行唯一 flow_id，含触发时间/模块链/输入输出摘要/
    节点状态耗时/资源快照/错误码。卡住（stalled）流程附最后活跃节点。
    """
    if status and status not in ("running", "success", "error",
                                 "cancelled", "stalled", "orphan"):
        raise ApiError("SYSTEM_PARAM_INVALID", f"非法状态: {status}")
    data = flow_trace.query_flow_traces(
        module=module, feature=feature, status=status, keyword=keyword,
        start_ts=start, end_ts=end, limit=limit, offset=offset)
    return ok(data)


@router.get("/logs/flows/trace/{flow_id}")
def logs_flow_trace_detail(flow_id: str):
    """单流程全明细：完整节点链（输入/输出/状态/耗时/资源/异常）。

    卡住定位：running/stalled 流程附 stall_analysis——卡在哪个环节、
    该环节输入与最后输出、已执行时长、排查提示。
    """
    data = flow_trace.get_flow_trace(flow_id)
    if data is None:
        raise ApiError("SYSTEM_NOT_FOUND", f"流程不存在: {flow_id}")
    return ok(data)


@router.get("/logs/flows/trace-stats")
def logs_flow_trace_stats():
    """追踪面板筛选项与状态计数（模块/功能清单 + 卡住数量）。"""
    return ok(flow_trace.flow_trace_stats())


@router.get("/logs/stats")
def logs_stats(days: int = Query(7, ge=1, le=30, description="统计回溯天数")):
    """统计：级别分布 + 模块分布 + 24h 逐小时趋势。"""
    return ok(event_stats(days=days))


@router.get("/logs/modules")
def logs_modules():
    """已产生事件的模块列表（前端筛选项动态生成）。"""
    return ok({"modules": list_modules()})


@router.get("/logs/files")
def logs_files():
    """原始日志文件清单（名称/大小/修改时间/可否 tail）。"""
    files = []
    for name, path in _RAW_LOG_FILES.items():
        if path.is_file():
            st = path.stat()
            files.append({
                "name": name,
                "size_bytes": st.st_size,
                "size_mb": round(st.st_size / 1024 / 1024, 2),
                "modified": datetime.fromtimestamp(
                    st.st_mtime).isoformat(timespec="seconds"),
            })
    return ok({"files": files, "retention_days": RETENTION_DAYS})


@router.get("/logs/raw")
def logs_raw(
    name: str = Query("backend.log", description="日志文件名（白名单校验）"),
    lines: int = Query(200, ge=1, le=2000, description="尾部行数"),
):
    """原始日志尾部（技术诊断用；仅白名单文件）。"""
    path = _RAW_LOG_FILES.get(name)
    if path is None:
        raise ApiError("SYSTEM_PARAM_INVALID",
                       f"未知日志文件: {name}（可选: {sorted(_RAW_LOG_FILES)}）")
    if not path.is_file():
        return ok({"name": name, "lines": [], "total_lines": 0,
                   "exists": False})
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ApiError("SYSTEM_INTERNAL_ERROR", f"读取日志失败: {exc}") from exc
    all_lines = content.splitlines()
    return ok({
        "name": name,
        "exists": True,
        "total_lines": len(all_lines),
        "lines": all_lines[-lines:],
    })


@router.post("/logs/cleanup")
def logs_cleanup():
    """手动触发过期日志清理（30 天规则，与自动巡检同一实现）。"""
    result = cleanup_expired()
    return ok({
        "deleted_count": len(result["deleted"]),
        "deleted_files": result["deleted"][:50],
        "freed_mb": round(result["freed_bytes"] / 1024 / 1024, 2),
        "retention_days": RETENTION_DAYS,
    }, message=f"清理完成：删除 {len(result['deleted'])} 个过期文件")
