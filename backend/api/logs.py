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

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Body, Query

from ..config import LOGS_DIR
from ..middleware.error_handler import ApiError, ok
from ..services import flow_trace
from ..services.event_log import (
    RETENTION_DAYS,
    cleanup_expired,
    event_stats,
    list_modules,
    log_event,
    query_events,
    query_flows,
)

log = logging.getLogger("omnispace.api.logs")

router = APIRouter()

# 原始日志白名单（tail/导出仅允许项目自有日志，防任意文件读取）
_RAW_LOG_FILES = {
    "backend.log": LOGS_DIR / "backend.log",
    "error.log": LOGS_DIR / "error.log",
    "vllm-server.log": LOGS_DIR / "vllm-server.log",
    "boot.log": LOGS_DIR / "boot.log",
    "comfyui_boot.log": LOGS_DIR / "comfyui_boot.log",
    "launcher.log": LOGS_DIR / "launcher.log",
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
    keyword: str = Query("", description="关键词过滤（命中行±上下文）"),
    level: str | None = Query(
        None, description="级别过滤 DEBUG/INFO/WARNING/ERROR"),
):
    """原始日志尾部（技术诊断用；仅白名单文件）。

    过滤（2026-09-01 方案 C）：keyword 命中行带 ±3 行上下文返回；
    level 按行内级别标记过滤。扫描上限 20 万行防打爆。
    """
    path = _RAW_LOG_FILES.get(name)
    if path is None:
        raise ApiError("SYSTEM_PARAM_INVALID",
                       f"未知日志文件: {name}（可选: {sorted(_RAW_LOG_FILES)}）")
    if level and level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ApiError("SYSTEM_PARAM_INVALID", f"非法级别: {level}")
    if not path.is_file():
        return ok({"name": name, "lines": [], "total_lines": 0,
                   "exists": False})
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ApiError("SYSTEM_INTERNAL_ERROR", f"读取日志失败: {exc}") from exc
    all_lines = content.splitlines()[-200_000:]
    total = len(all_lines)

    def _match(line: str) -> bool:
        if keyword and keyword not in line:
            return False
        if level and f"[{level.upper()}]" not in line:
            return False
        return True

    if not keyword and not level:
        picked = list(range(max(0, total - lines), total))
    else:
        hits = [i for i, ln in enumerate(all_lines) if _match(ln)]
        ctx = 3 if keyword else 0
        picked_set: set[int] = set()
        for h in hits:
            for j in range(max(0, h - ctx), min(total, h + ctx + 1)):
                picked_set.add(j)
        picked = sorted(picked_set)[-lines * 2:]  # 命中+上下文，防超量
    return ok({
        "name": name,
        "exists": True,
        "total_lines": total,
        "keyword": keyword,
        "level": level,
        "lines": [all_lines[i] for i in picked],
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


# ═══════════════════════════════════════════════════════════════════
#  错误聚合 / 前端采集 / 导出诊断包（2026-09-01 日志机制方案 C）
# ═══════════════════════════════════════════════════════════════════

@router.get("/logs/errors/summary")
def logs_errors_summary(
    days: int = Query(7, ge=1, le=30, description="聚合回溯天数"),
    limit: int = Query(20, ge=1, le=100, description="返回分组上限"),
):
    """最近异常聚合（错误面板）：按 模块×事件类型 归并计数 + 代表文案。

    排障入口视图：一眼看出「哪类错最多、最近一次何时」。
    """
    items = query_events(limit=100_000, days=days,
                         level="error")["items"]
    groups: dict[tuple, dict] = {}
    for e in items:
        key = (str(e.get("module") or "?"), str(e.get("event") or "?"))
        g = groups.get(key)
        if g is None:
            groups[key] = {
                "module": key[0], "event": key[1], "count": 1,
                "first_ts": e.get("ts"), "last_ts": e.get("ts"),
                "example": str(e.get("friendly") or "")[:160],
            }
        else:
            g["count"] += 1
            g["last_ts"] = e.get("ts")
            if len(str(e.get("friendly") or "")) > len(g["example"]):
                g["example"] = str(e.get("friendly"))[:160]
    top = sorted(groups.values(), key=lambda g: -g["count"])[:limit]
    return ok({"groups": top, "total_errors": len(items), "days": days})


@router.post("/logs/frontend-event")
def logs_frontend_event(body: dict = Body(default_factory=dict)):
    """前端 console 错误采集入口（方案 C：window.onerror / unhandledrejection）。

    前端按节流策略上报（同 key 5s 一条），此处只做长度钳制后入事件库。
    """
    msg = str(body.get("message") or "").strip()[:200]
    if not msg:
        raise ApiError("SYSTEM_PARAM_INVALID", "message 不能为空")
    kind = str(body.get("kind") or "error")
    if kind not in ("error", "warning"):
        kind = "error"
    stack = str(body.get("stack") or "")[:1000]
    log_event("frontend", f"console_{kind}",
              f"前端{'异常' if kind == 'error' else '警告'}：{msg}",
              level=kind, detail=stack)
    return ok({"recorded": True})


# 导出脱敏：这些字段的值视为用户内容（提示词/描述词），打码
_SANITIZE_KEYS = {"prompt", "description", "content",
                  "input_summary", "output"}


def _mask_text(v: str) -> str:
    return (v[:4] + "***" + v[-4:]) if len(v) > 12 else "***"


def _sanitize_obj(obj):
    """递归脱敏 dict/list 中的用户内容字段（导出勾选「脱敏」时）。"""
    if isinstance(obj, dict):
        return {k: (_mask_text(v) if k in _SANITIZE_KEYS
                    and isinstance(v, str) and v.strip() else _sanitize_obj(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_obj(x) for x in obj]
    return obj


@router.get("/logs/export")
def logs_export(
    hours: int = Query(24, ge=1, le=720, description="导出时间范围（小时）"),
    include: str = Query("events,flows,raw,hardware",
                         description="包含项逗号分隔：events/flows/raw/hardware"),
    failed_flows: int = Query(10, ge=0, le=50,
                              description="附带最近 N 条失败流程全明细"),
    sanitize: bool = Query(False, description="脱敏：提示词/描述词打码"),
):
    """一键导出诊断包（方案 C）：zip = 事件 + 失败流程明细 + 原始日志 +
    硬件快照 + manifest。自用排障含提示词；发他人勾 sanitize。"""
    import csv
    import io
    import os
    import tempfile
    import zipfile
    from datetime import timedelta

    from starlette.background import BackgroundTask
    from starlette.responses import FileResponse

    from ..config import APP_VERSION
    from .hardware import _build_hardware_profile

    parts = {p.strip() for p in include.split(",") if p.strip()}
    now = datetime.now()
    cutoff = (now - timedelta(hours=hours)).isoformat(timespec="milliseconds")
    generated_at = now.isoformat(timespec="seconds")
    counts: dict[str, int] = {}
    crash_note = ""

    # ── 事件（时间窗过滤；CSV+JSON 双格式）──
    events: list[dict] = []
    if "events" in parts:
        days = max(1, min(30, -(-hours // 24)))
        events = [e for e in query_events(limit=100_000, days=days)["items"]
                  if str(e.get("ts", "")) >= cutoff]
        # 崩溃取证标记（heartbeat 检测写入的事件，manifest 里醒目提示）
        for e in events:
            if e.get("event") == "system_crash_detected":
                crash_note = f"注意：该时段内检测到异常退出（{e.get('friendly')}）"
                break
    if sanitize:
        events = _sanitize_obj(events)

    # ── 失败流程明细 ──
    failed_details: list[dict] = []
    if "flows" in parts and failed_flows > 0:
        try:
            tr = flow_trace.query_flow_traces(status="error",
                                              limit=failed_flows)
            for row in (tr.get("items") or []):
                fid = row.get("flow_id")
                detail = flow_trace.get_flow_trace(fid) if fid else None
                if detail:
                    failed_details.append(detail)
        except Exception as exc:  # noqa: BLE001 - 流程导出失败不阻断打包
            log.warning("失败流程导出失败（跳过）: %s", exc)
    if sanitize:
        failed_details = _sanitize_obj(failed_details)

    # ── 硬件快照 ──
    hardware: dict = {}
    if "hardware" in parts:
        hardware = dict(_build_hardware_profile())
        try:
            from ..services.resource_sampler import get_resource_sampler
            hardware["recent_resource_samples"] = \
                get_resource_sampler().get_samples(limit=120)
        except Exception:  # noqa: BLE001
            pass

    # ── 打包（全部 writestr，无中间文件句柄）──
    fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="omnidiag_")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if "events" in parts:
                zf.writestr("events.json",
                            json.dumps(events, ensure_ascii=False, indent=1))
                buf = io.StringIO()
                if events:
                    # 字段并集（事件可选字段多，取首条字段会漏列）
                    cols = sorted({k for e in events for k in e.keys()})
                    w = csv.DictWriter(buf, fieldnames=cols,
                                       extrasaction="ignore")
                    w.writeheader()
                    w.writerows(events)
                zf.writestr("events.csv", buf.getvalue())
                counts["events"] = len(events)
            if failed_details:
                zf.writestr("failed_flows.json", json.dumps(
                    failed_details, ensure_ascii=False, indent=1))
                counts["failed_flows"] = len(failed_details)
            if "raw" in parts:
                for fname, fpath in _RAW_LOG_FILES.items():
                    if fpath.is_file():
                        try:
                            zf.writestr(
                                f"raw/{fname}",
                                fpath.read_text(encoding="utf-8",
                                                errors="replace"))
                            counts[f"raw:{fname}"] = 1
                        except OSError as exc:  # noqa: BLE001
                            log.warning("日志文件入包失败 %s: %s", fname, exc)
            if "hardware" in parts:
                zf.writestr("hardware.json", json.dumps(
                    hardware, ensure_ascii=False, indent=1, default=str))
                counts["hardware"] = 1
            manifest = "\n".join([
                "OmniSpace 诊断包",
                f"生成时间: {generated_at}",
                f"版本: {APP_VERSION}",
                f"时间范围: 最近 {hours} 小时（{cutoff} 起）",
                f"包含项: {', '.join(sorted(parts))}",
                f"失败流程明细: {len(failed_details)} 条",
                f"脱敏: {'是（提示词/描述词已打码）' if sanitize else '否（含用户内容）'}",
                f"事件数: {len(events)}",
                crash_note,
                "文件说明: events.json/csv=事件库导出；failed_flows.json=失败"
                "生成流程全链路明细；raw/=原始日志；hardware.json=硬件与资源快照",
            ])
            zf.writestr("manifest.txt", manifest)
    except Exception:
        os.unlink(tmp_path)
        raise

    filename = f"omnispace_diagnostics_{now.strftime('%Y%m%d_%H%M%S')}.zip"
    return FileResponse(
        tmp_path, filename=filename, media_type="application/zip",
        background=BackgroundTask(os.unlink, tmp_path))
