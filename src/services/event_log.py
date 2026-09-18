"""统一事件日志服务（2026-08-21 日志可视化裁定）。

全项目重要事件的**结构化**日志通道：每条事件一行 JSON 写入
``logs/events/events-YYYYMMDD.jsonl``（按天切分），供前端日志面板
可视化查询。与既有文本日志（backend.log，技术诊断用）互补：

- 文本日志：给开发者看的技术细节（保留原样）
- 事件日志：给用户看的「大白话」时间线——每条必须带 friendly 字段，
  用通俗语言描述发生了什么（如「对话模型加载完成，用了 48 秒，
  占了 7.2GB 显存」）

保留策略（用户裁定）：**30 天自动清除**
- 每天一个文件，启动时 + 每日凌晨清理 30 天前的过期文件
- 原始日志（backend.log/error.log 轮转备份、vllm-server.log）同样
  纳入 30 天清理

事件结构::

    {
      "ts": "2026-08-21T16:30:00.123",     # ISO8601 本地时间
      "level": "success",                   # info/success/warning/error
      "module": "dialog",                   # 模块标识（筛选维度）
      "event": "model_loaded",              # 事件类型
      "friendly": "对话模型「Qwen3-VL-8B」加载完成，用时 48 秒",
      "detail": "backend=vllm, dir=models/qwen3-vl-8b-awq",  # 技术详情
      "duration_ms": 48000                  # 可选耗时
    }

用法（任意模块）::

    from src.services.event_log import log_event

    log_event("dialog", "model_loaded", "对话模型「Qwen3-VL-8B」加载完成",
              level="success", detail=f"backend={kind}", duration_ms=4800)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import LOGS_DIR

log = logging.getLogger("omnispace.event_log")

# 事件日志根目录（按天切分文件）
EVENTS_DIR = LOGS_DIR / "events"
# 测试流量隔离（2026-09-17 依赖文档审计批1）：pytest 态下事件 jsonl 默认
# 分流到独立目录——错误面板（query_events / /logs/errors/summary 聚合）
# 从此只见真实用户流量，不再被 pytest/TestClient 流量污染（definitely/
# 三连/pytest/mock 四簇两轮实锤）。与 single_instance._exempt 同款
# PYTEST_VERSION 判据（进程全生命周期含 fixture 期）；tests/unit conftest
# 的 B9 只重定向了 logging 文件 handler，本目录是裸 open() 写、此前漏网。
# 需要观察产品真实落点的测试可置 OMNISPACE_OBSERVE_IN_TESTS=1 逃逸。
if (os.environ.get("PYTEST_VERSION")
        and os.environ.get("OMNISPACE_OBSERVE_IN_TESTS") != "1"):
    EVENTS_DIR = LOGS_DIR / "events-test"
# 保留天数（用户裁定 2026-08-21：每个日志只保存 30 天）
RETENTION_DAYS = 30
# 一次性调试日志残留的过期天数与清扫模式（2026-09-17 拍板：自动过期
# 替代手动清——boot 冒烟/A-B 对比/诊断/补丁脚本等会话产物实测一轮
# 堆 87 个/11MB；仅 logs/ 一级文件，白名单模式绝不误伤轮转系）
DEBUG_RESIDUE_DAYS = 14
_DEBUG_RESIDUE_PATTERNS = (
    "boot_smoke_*", "boot_test_*", "boot_out*", "boot_err*",
    "boot_relaunch_*", "_boot_live*", "_e2e*", "c2_e2e*",
    "ab_*", "diag_*", "_qa_*", "_patch_*", "smoke*", "describe_test*",
    "fake_searxng*", "test*_out.log", "test*_err.log",
)
# 清理巡检间隔（24h；启动时另执行一次）
_CLEANUP_INTERVAL_S = 24 * 3600

_LEVELS = {"info", "success", "warning", "error"}

_write_lock = threading.Lock()
_cleanup_started = False
_cleanup_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════
#  写入
# ═══════════════════════════════════════════════════════════════════

def _day_file(day: datetime | None = None) -> Path:
    """事件按天切分：logs/events/events-YYYYMMDD.jsonl。"""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    d = day or datetime.now()
    return EVENTS_DIR / f"events-{d.strftime('%Y%m%d')}.jsonl"


def log_event(
    module: str,
    event: str,
    friendly: str,
    *,
    level: str = "info",
    detail: str = "",
    duration_ms: float | None = None,
    trace_id: str = "",
) -> None:
    """写入一条用户可读的重要事件（永不抛错，日志失败不影响业务）。

    Args:
        module: 模块标识（system/dialog/paint/models/vllm/training/...），
            前端筛选维度
        event: 事件类型（model_loaded/model_switched/service_started/...）
        friendly: 大白话描述——必须用用户能看懂的语言说清「发生了什么」
        level: info/success/warning/error（成功完成用 success）
        detail: 技术详情（可选，前端折叠展示给开发者）
        duration_ms: 耗时毫秒（可选）
        trace_id: 流程追踪 ID（可选，2026-08-22 执行流程面板）：同一次
            跨模块功能执行（如模型热切换：dialog→vllm→models）中所有
            事件带同一 trace_id，流程面板可精确串联；历史数据无此字段
            时由 query_flows 按时间邻近性启发式聚合
    """
    entry: dict[str, Any] = {
        # 时区口径（2026-09-19 Q7）：对外发射的时间戳=本地带偏移（aware），
        # 消除与 UTC 侧对比时的 8h 漂移；库内日界聚合仍用 naive 本地（自洽）。
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "level": level if level in _LEVELS else "info",
        "module": module,
        "event": event,
        "friendly": friendly,
    }
    if detail:
        entry["detail"] = detail
    if duration_ms is not None:
        entry["duration_ms"] = round(duration_ms)
    if trace_id:
        entry["trace_id"] = trace_id

    line = json.dumps(entry, ensure_ascii=False, default=str)
    try:
        with _write_lock:
            with open(_day_file(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as exc:
        # 事件日志写失败只影响可视化，不影响业务主链路
        log.warning("事件日志写入失败: %s", exc)


# ═══════════════════════════════════════════════════════════════════
#  查询（供 api/logs.py）
# ═══════════════════════════════════════════════════════════════════

def _iter_entries(
    days: int = 7,
    level: str | None = None,
    module: str | None = None,
    keyword: str = "",
) -> Iterator[dict[str, Any]]:
    """倒序遍历最近 N 天的事件（新→旧），应用过滤条件。"""
    now = datetime.now()
    for offset in range(days):
        day = now - timedelta(days=offset)
        f = _day_file(day)
        if not f.is_file():
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        # 单文件内也是新→旧
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if level and e.get("level") != level:
                continue
            if module and e.get("module") != module:
                continue
            if keyword:
                hay = f"{e.get('friendly', '')} {e.get('detail', '')} {e.get('event', '')}"
                if keyword.lower() not in hay.lower():
                    continue
            yield e


def query_events(
    limit: int = 200,
    offset: int = 0,
    days: int = 7,
    level: str | None = None,
    module: str | None = None,
    keyword: str = "",
) -> dict[str, Any]:
    """分页查询事件（新→旧）。返回 {items, total, has_more}。"""
    items: list[dict[str, Any]] = []
    total = 0
    for e in _iter_entries(days=days, level=level, module=module,
                           keyword=keyword):
        total += 1
        if len(items) < limit:
            items.append(e)
    # offset 语义：跳过前 offset 条（新→旧游标翻页）
    return {"items": items, "total": total,
            "has_more": total > offset + len(items)}


# ═══════════════════════════════════════════════════════════════════
#  执行流程聚合（2026-08-22 功能执行流程可视化面板）
# ═══════════════════════════════════════════════════════════════════

# 周期性巡检/心跳类事件：不参与流程分割（否则每 30s 一条的心跳会把
# 相互独立的功能执行粘连成超长流程）；聚合时直接跳过，不进入流程节点
_NOISE_EVENTS = {"vram_shed_no_candidate"}

# 相邻主链路事件间隔超过该秒数即判定为新的功能执行流程
_FLOW_GAP_S = 120.0

# 单流程事件数上限（防御日志风暴把流程撑爆前端渲染）
_FLOW_MAX_EVENTS = 120

# 事件 → 功能类型（前端「按功能类型筛选」维度）
_EVENT_CATEGORY_RULES: list[tuple[str, str]] = [
    ("model_prewarm", "模型加载"),
    ("model_load", "模型加载"),
    ("model_switch", "模型切换"),
    ("gen_route", "模型切换"),
    ("model_unload", "模型卸载"),
    ("service_", "推理服务"),
    ("start_", "推理服务"),
    ("warmup", "推理服务"),
    ("module_release", "资源释放"),
    ("vram_shed", "显存巡检"),
    ("logs_clean", "日志清理"),
    ("training", "模型训练"),
    ("learn_", "知识学习"),
    ("keyframe_consistency", "漫剧分镜"),
]

# 事件名规则未命中时的模块级兜底
_MODULE_CATEGORY_FALLBACK = {
    "dialog": "AI 对话",
    "paint": "AI 绘画",
    "learn": "知识学习",
    "vllm": "推理服务",
    "models": "模型管理",
    "training": "模型训练",
    "system": "系统调度",
}


def _event_category(event: str, module: str) -> str:
    """事件功能类型（模型加载/模型切换/资源释放/...）。"""
    for prefix, cat in _EVENT_CATEGORY_RULES:
        if event.startswith(prefix):
            return cat
    return _MODULE_CATEGORY_FALLBACK.get(module, "其他")


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def query_flows(
    days: int = 1,
    level: str | None = None,
    module: str | None = None,
    keyword: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """把事件流聚合为「功能执行流程」（供前端流程可视化面板）。

    聚合规则（启发式，兼容无 trace_id 的全部历史数据）：
    1. 跳过噪声事件（周期心跳）；
    2. 相邻事件间隔 > _FLOW_GAP_S 开新流程（同一次功能执行的跨模块
       事件间隔通常在秒级，如模型热切换 dialog→vllm→models 全链 <10s）；
    3. 流程状态：含 error → error；含 warning → warning；否则 ok。

    筛选语义（流程级而非事件级，保证流程完整性）：
    - module: 流程涉及该模块；level: 流程含该级别事件；
    - keyword: 流程内任一事件的大白话/详情/事件名命中。

    Returns:
        {"flows": [...], "total": N}（flows 新→旧），
        每条 flow: flow_id/start_ts/end_ts/span_ms/modules/categories/
        status/event_count/total_duration_ms/summary/events（旧→新执行序）
    """
    # 1. 收集主链路事件并转为旧→新时间序
    entries: list[dict[str, Any]] = []
    for e in _iter_entries(days=days):
        if e.get("event") in _NOISE_EVENTS:
            continue
        if _parse_ts(e.get("ts", "")) is None:
            continue
        entries.append(e)
    entries.reverse()  # _iter_entries 是新→旧，流程需旧→新执行序

    # 2. 按时间邻近性切分流程
    flows: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    prev_ts: datetime | None = None
    for e in entries:
        ts = _parse_ts(e["ts"])
        assert ts is not None  # 上文已过滤解析失败的条目
        if prev_ts is not None and (ts - prev_ts).total_seconds() > _FLOW_GAP_S:
            flows.append(cur)
            cur = []
        cur.append(e)
        prev_ts = ts
    if cur:
        flows.append(cur)

    # 3. 逐流程计算元数据 + 组装
    out: list[dict[str, Any]] = []
    for seq in flows:
        if len(seq) > _FLOW_MAX_EVENTS:
            seq = seq[-_FLOW_MAX_EVENTS:]
        events: list[dict[str, Any]] = []
        modules: list[str] = []
        categories: list[str] = []
        has_error = has_warning = False
        total_ms = 0.0
        for e in seq:
            ev = dict(e)
            ev["category"] = _event_category(
                str(e.get("event", "")), str(e.get("module", "")))
            if ev["module"] not in modules:
                modules.append(ev["module"])
            if ev["category"] not in categories:
                categories.append(ev["category"])
            lv = ev.get("level", "info")
            has_error = has_error or lv == "error"
            has_warning = has_warning or lv == "warning"
            total_ms += float(ev.get("duration_ms") or 0)
            events.append(ev)

        start = events[0]["ts"]
        end = events[-1]["ts"]
        t0 = _parse_ts(start)
        t1 = _parse_ts(end)
        span_ms = round(((t1 - t0).total_seconds() * 1000)
                        if t0 and t1 else 0)
        status = "error" if has_error else ("warning" if has_warning else "ok")
        err_count = sum(1 for e in events if e.get("level") == "error")

        # 人话摘要：主功能类型 + 跨模块数 + 步数 + 结果
        main_cat = categories[0] if categories else "其他"
        mod_note = f"{len(modules)} 模块" if len(modules) > 1 else modules[0]
        if has_error:
            verdict = f"{err_count} 个错误"
        elif has_warning:
            verdict = "有注意项"
        else:
            verdict = "全部成功"
        summary = f"{main_cat} · {mod_note} · {len(events)} 步 · {verdict}"

        out.append({
            "flow_id": f"flow-{start.replace(':', '').replace('-', '')}",
            "start_ts": start,
            "end_ts": end,
            "span_ms": span_ms,
            "modules": modules,
            "categories": categories,
            "status": status,
            "event_count": len(events),
            "total_duration_ms": round(total_ms),
            "summary": summary,
            "events": events,
        })

    # 4. 流程级筛选（在完整流程上判定，不切断流程内部事件）
    def _flow_match(f: dict[str, Any]) -> bool:
        if module and module not in f["modules"]:
            return False
        if level:
            if not any(e.get("level") == level for e in f["events"]):
                return False
        if keyword:
            kw = keyword.lower()
            hay = " ".join(
                f"{e.get('friendly', '')} {e.get('detail', '')} "
                f"{e.get('event', '')}" for e in f["events"])
            if kw not in hay.lower():
                return False
        return True

    out = [f for f in out if _flow_match(f)]
    out.reverse()  # 返回新→旧
    return {"flows": out[:limit], "total": len(out)}


def event_stats(days: int = 7) -> dict[str, Any]:
    """统计：级别分布 + 模块分布 + 最近 24 小时逐小时数量（趋势图）。"""
    by_level = {"info": 0, "success": 0, "warning": 0, "error": 0}
    by_module: dict[str, int] = {}
    hourly: list[dict[str, Any]] = []

    now = datetime.now()
    # 24h 逐小时桶（含标签，旧→新）
    buckets: dict[str, int] = {}
    for h in range(23, -1, -1):
        t = now - timedelta(hours=h)
        buckets[t.strftime("%m-%d %H:00")] = 0

    for e in _iter_entries(days=days):
        lv = e.get("level", "info")
        by_level[lv] = by_level.get(lv, 0) + 1
        mod = e.get("module", "other")
        by_module[mod] = by_module.get(mod, 0) + 1
        try:
            ts = datetime.fromisoformat(e.get("ts", ""))
            age_h = (now - ts).total_seconds() / 3600
            if 0 <= age_h < 24:
                key = ts.strftime("%m-%d %H:00")
                if key in buckets:
                    buckets[key] += 1
        except ValueError:
            log.debug("event_stats: 降级忽略", exc_info=True)

    hourly = [{"hour": k, "count": v} for k, v in buckets.items()]
    # 模块分布按量排序（Top 12）
    top_modules = sorted(by_module.items(), key=lambda x: -x[1])[:12]
    return {
        "total": sum(by_level.values()),
        "by_level": by_level,
        "by_module": [{"module": m, "count": c} for m, c in top_modules],
        "hourly_24h": hourly,
        "retention_days": RETENTION_DAYS,
    }


def list_modules() -> list[str]:
    """已产生事件的模块列表（前端筛选项）。"""
    mods: set[str] = set()
    for e in _iter_entries(days=RETENTION_DAYS):
        mods.add(str(e.get("module", "other")))
    return sorted(mods)


# ═══════════════════════════════════════════════════════════════════
#  30 天自动清除
# ═══════════════════════════════════════════════════════════════════

def cleanup_expired(now: datetime | None = None) -> dict[str, Any]:
    """删除超过 30 天的日志文件（事件日志 + 原始日志轮转备份）。

    Returns:
        {"deleted": [...], "freed_bytes": N}
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=RETENTION_DAYS)
    deleted: list[str] = []
    freed = 0

    # 1) 事件日志：文件名日期 > 30 天即删
    if EVENTS_DIR.is_dir():
        for f in EVENTS_DIR.glob("events-*.jsonl"):
            try:
                day = datetime.strptime(f.stem, "events-%Y%m%d")
            except ValueError:
                continue
            if day < cutoff:
                freed += f.stat().st_size
                f.unlink(missing_ok=True)
                deleted.append(f.name)

    # 2) 原始日志轮转备份（backend.log.N / error.log.N）：按文件修改时间
    for pattern in ("backend.log.*", "error.log.*", "vllm-server.log*"):
        for f in LOGS_DIR.glob(pattern):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
            except OSError:
                continue
            if mtime < cutoff:
                freed += f.stat().st_size
                f.unlink(missing_ok=True)
                deleted.append(f.name)

    # 3) 一次性调试日志残留（2026-09-17 拍板=自动过期）：boot 冒烟/A-B
    # 对比/诊断/补丁脚本等会话产物会反复堆积（实测 87 个/11MB 一轮），
    # 超 14 天按模式清扫；仅清 logs/ 一级文件（不递归、不碰目录）
    debug_cutoff = now - timedelta(days=DEBUG_RESIDUE_DAYS)
    for pattern in _DEBUG_RESIDUE_PATTERNS:
        for f in LOGS_DIR.glob(pattern):
            if not f.is_file():
                continue
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
            except OSError:
                continue
            if mtime < debug_cutoff:
                freed += f.stat().st_size
                f.unlink(missing_ok=True)
                deleted.append(f.name)

    if deleted:
        log.info("日志过期清理：%d 个文件（超 %d 天），释放 %.1fMB",
                    len(deleted), RETENTION_DAYS, freed / 1024 / 1024)
        log_event(
            "system", "logs_cleaned",
            f"自动清理了 {len(deleted)} 个超过 30 天的旧日志文件，"
            f"释放磁盘 {freed / 1024 / 1024:.1f}MB",
            level="info", detail=f"files={deleted[:10]}")
    return {"deleted": deleted, "freed_bytes": freed}


def start_cleanup_task() -> None:
    """启动后台清理线程（幂等）：立即清一次 + 每 24h 巡检。

    B5 步5：循环体收敛到 periodic.start_periodic_daemon（消克隆）。"""
    global _cleanup_started
    with _cleanup_lock:
        if _cleanup_started:
            return
        _cleanup_started = True

    from .periodic import start_periodic_daemon
    start_periodic_daemon("log-cleanup", _CLEANUP_INTERVAL_S,
                          cleanup_expired, run_immediately=True)
# 本项目仅供学习使用，商业授权请+Q 3559331368
