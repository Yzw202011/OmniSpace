"""执行流程追踪服务（2026-08-23 执行流程记录机制优化）。

为每次用户触发的功能执行建立**唯一流程标识（flow_id）**，贯穿
「API 入口 → 后台调度 → 引擎推理 → 结果落盘」全链路，记录：

- 功能触发时间点（trigger_time）与触发来源
- 涉及的核心模块及调用顺序（节点 seq 链）
- 各节点间数据传递内容（input_summary / output_summary）
- 关键节点执行状态（running/success/error/skipped）与耗时
- 资源快照（CPU / 内存 / GPU 显存与利用率，节点粒度）

双受众设计（与 event_log 同一裁定）：
- friendly 字段大白话（用户可理解：'图像生成完成，用时 166 秒'）
- detail / input / output / error 字段技术细节（开发排查）

卡住（stalled）定位机制——用户报告「某功能卡住」时：
1. status=running 且超过 STALL_THRESHOLD_S 无进度心跳 → 查询时
   动态标记 stalled，并附「最后活跃节点」（其输入/预期输出/资源快照）；
2. 长推理通过 node.progress() 心跳续命（去噪步进/token 流），真卡住
   = 无心跳 + 无节点完成；
3. 进程重启遗留的 running 记录 → 启动时标记 orphan（诚实呈现）。

存储：独立 SQLite ``logs/flow_trace.db``（WAL 模式，与业务库隔离，
避免主库单写锁竞争）。双表：

- flow_executions：流程级（flow_id/module/feature/status/起止/耗时/
  错误码/起止资源快照）
- flow_nodes：节点级（seq 链/状态/耗时/输入输出摘要/资源快照）

保留策略与事件日志一致（30 天，后台线程自动清理）。

联动：流程 start/end 与每个节点完成都会写一条 event_log（带
trace_id=flow_id），大白话时间线与既有启发式流程聚合自动可见。

用法（API 层启动，跨线程显式传递 Flow 对象）::

    from backend.services.flow_trace import start_flow, current_flow

    # 1) 请求线程（或任意线程）启动流程
    flow = start_flow("paint", "txt2img", "AI 绘画：一只橘猫…",
                      trigger="用户提交生成任务",
                      input_summary="1024x1024 30步 qwen-image-2512")

    # 2) 节点（context manager，自动计时/状态/资源/异常捕获）
    with flow.node("图像生成", input_summary="prompt=… steps=30",
                   friendly="正在生成图像") as node:
        result = engine.generate(params)
        node.progress("50% (15/30 步)")   # 长任务心跳（可选）
        node.output(f"seed=42 用时 {result['elapsed_ms']}ms")

    # 3) 结束（异常时 status=error + 错误码）
    flow.end("success", output_summary="generated/images/xxx.png")
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from ..config import LOGS_DIR

logger = logging.getLogger("omnispace.flow_trace")

# ── 配置 ────────────────────────────────────────────────────────────
DB_PATH = LOGS_DIR / "flow_trace.db"
RETENTION_DAYS = 30          # 与事件日志同一保留裁定
STALL_THRESHOLD_S = 120.0    # 无心跳超此秒数 → stalled（用户感知的"卡住"）
_CLEANUP_INTERVAL_S = 24 * 3600

# 流程终态集合（查询侧 stalled/orphan 为动态推导，不落库）
_FINAL_STATUS = {"success", "error", "cancelled"}

_write_lock = threading.RLock()  # RLock：_query/_exec 持锁调 _get_db()，后者初始化需重入（Lock 会死锁）
_db: sqlite3.Connection | None = None
_cleanup_started = False
_cleanup_lock = threading.Lock()

# 活跃流程注册表（进程内）：orphan 判定 + 崩溃恢复的内存真源
_active_flows: dict[str, Flow] = {}
_registry_lock = threading.Lock()

# contextvars：asyncio 任务链自动继承（FastAPI 请求 → 协程）
_current_flow: ContextVar[Flow | None] = ContextVar("flow_trace", default=None)


# ── 资源快照（CPU/内存/GPU，全 try 包裹，缺库降级空值） ────────────
_monitor_instance = None


def _monitor():
    """惰性复用 scheduler.monitor.HardwareMonitor（自带 TTL 缓存）。"""
    global _monitor_instance
    if _monitor_instance is None:
        try:
            from .scheduler.monitor import HardwareMonitor
            _monitor_instance = HardwareMonitor()
        except Exception:  # noqa: BLE001 - 资源快照失败不阻断业务
            _monitor_instance = False
    return _monitor_instance or None


def resource_snapshot() -> dict[str, Any]:
    """节点粒度资源快照：{cpu_percent, ram_percent, ram_used_gb, gpu}。

    开销：CPU 非阻塞采样（interval=None，自上次调用以来的均值），
    GPU 走 HardwareMonitor 的 2s TTL 缓存——单次 <1ms。
    """
    snap: dict[str, Any] = {}
    try:
        import psutil
        snap["cpu_percent"] = round(psutil.cpu_percent(interval=None), 1)
        vm = psutil.virtual_memory()
        snap["ram_percent"] = round(vm.percent, 1)
        snap["ram_used_gb"] = round(vm.used / 1024**3, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        m = _monitor()
        if m is not None:
            gpu = m.get_gpu()
            snap["gpu"] = {
                "vram_used_mb": gpu.get("vram_used_mb", 0),
                "vram_total_mb": gpu.get("vram_total_mb", 0),
                "util_percent": gpu.get("util_percent", 0.0),
                "temp_celsius": gpu.get("temp_celsius", 0.0),
            }
    except Exception:  # noqa: BLE001
        pass
    return snap


def _res_str(snap: dict[str, Any]) -> str:
    return json.dumps(snap, ensure_ascii=False) if snap else ""


# ── SQLite 初始化与写入 ─────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_executions (
    flow_id        TEXT PRIMARY KEY,
    module         TEXT NOT NULL,
    feature        TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'running',
    friendly       TEXT NOT NULL,
    trigger        TEXT DEFAULT '',
    detail         TEXT DEFAULT '',
    input_summary  TEXT DEFAULT '',
    output_summary TEXT DEFAULT '',
    error_code     TEXT DEFAULT '',
    error_detail   TEXT DEFAULT '',
    started_at     REAL NOT NULL,
    ended_at       REAL,
    duration_ms    INTEGER,
    last_active    REAL,
    node_count     INTEGER DEFAULT 0,
    resource_start TEXT DEFAULT '',
    resource_end   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_flow_module ON flow_executions(module, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_flow_status ON flow_executions(status, started_at DESC);

CREATE TABLE IF NOT EXISTS flow_nodes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id        TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    node           TEXT NOT NULL,
    module         TEXT DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'running',
    started_at     REAL NOT NULL,
    duration_ms    INTEGER,
    input_summary  TEXT DEFAULT '',
    output_summary TEXT DEFAULT '',
    detail         TEXT DEFAULT '',
    friendly       TEXT DEFAULT '',
    error          TEXT DEFAULT '',
    resource       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_node_flow ON flow_nodes(flow_id, seq);
"""


def _get_db() -> sqlite3.Connection:
    """线程安全的惰性连接（check_same_thread=False + 全局写锁）。"""
    global _db
    if _db is None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA synchronous=NORMAL")
        with _write_lock:
            _db.executescript(_SCHEMA)
            _db.commit()
    return _db


def _exec(sql: str, params: tuple = ()) -> None:
    """写库（永不抛错：追踪失败不影响业务主链路）。"""
    try:
        with _write_lock:
            db = _get_db()
            db.execute(sql, params)
            db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("flow_trace 写入失败: %s", exc)


def _query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    try:
        with _write_lock:
            db = _get_db()
            rows = db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("flow_trace 查询失败: %s", exc)
        return []


# ── 节点（context manager） ─────────────────────────────────────────

class _NodeCtx:
    """流程节点：进入记时间/资源，退出记状态/耗时，异常记 error 并放行。

    progress() 心跳：长推理（去噪步进 / token 流）持续刷新 last_active，
    使 stalled 检测精确到「无进度」而非「耗时长」。
    """

    __slots__ = ("flow", "name", "seq", "status", "started_at",
                 "_input", "_output", "_friendly", "_detail", "_module")

    def __init__(self, flow: Flow, name: str, seq: int,
                 input_summary: str = "", friendly: str = "",
                 module: str = "", detail: str = ""):
        self.flow = flow
        self.name = name
        self.seq = seq
        self.status = "running"
        self.started_at = time.time()
        self._input = input_summary
        self._output = ""
        self._friendly = friendly
        self._detail = detail
        self._module = module or flow.module

    def _ts(self) -> float:
        return time.time()

    def progress(self, note: str = "") -> None:
        """心跳：报告进行中进度（更新节点 output 与 flow.last_active）。"""
        if note:
            self._output = note
        try:
            with _write_lock:
                db = _get_db()
                now = time.time()
                db.execute(
                    "UPDATE flow_nodes SET output_summary=?, status='running'"
                    " WHERE flow_id=? AND seq=?",
                    (note, self.flow.flow_id, self.seq))
                db.execute(
                    "UPDATE flow_executions SET last_active=? WHERE flow_id=?",
                    (now, self.flow.flow_id))
                db.commit()
        except Exception:  # noqa: BLE001
            pass

    def output(self, summary: str) -> None:
        """补充输出摘要（with 块内调用；退出时统一落库）。"""
        self._output = summary

    def detail(self, text: str) -> None:
        self._detail = text

    def _finish(self, status: str, duration_ms: int, error: str = "") -> None:
        _exec(
            "UPDATE flow_nodes SET status=?, duration_ms=?, output_summary=?,"
            " detail=?, error=?, resource=? WHERE flow_id=? AND seq=?",
            (status, duration_ms, self._output, self._detail, error,
             _res_str(resource_snapshot()), self.flow.flow_id, self.seq))
        # 节点完成事件 → 大白话时间线（level 随状态）
        from .event_log import log_event
        level = ("success" if status == "success"
                 else "error" if status == "error" else "info")
        log_event(
            self._module, f"flow_node_{status}",
            self._friendly or f"{self.flow.feature} · {self.name} "
                              f"{'完成' if status == 'success' else status}",
            level=level,
            detail=f"flow={self.flow.flow_id} node={self.name}"
                   f"{f' in={self._input}' if self._input else ''}"
                   f"{f' out={self._output}' if self._output else ''}",
            duration_ms=duration_ms,
            trace_id=self.flow.flow_id)

    def __enter__(self) -> _NodeCtx:
        _exec(
            "INSERT INTO flow_nodes (flow_id, seq, node, module, status,"
            " started_at, input_summary, friendly) VALUES (?,?,?,?,?,?,?,?)",
            (self.flow.flow_id, self.seq, self.name, self._module,
             "running", self.started_at, self._input, self._friendly))
        self.flow._node_seq += 1
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        duration_ms = int((time.time() - self.started_at) * 1000)
        if exc_type is None:
            self._finish("success", duration_ms)
            _exec("UPDATE flow_executions SET last_active=?, node_count="
                  "node_count+1 WHERE flow_id=?",
                  (time.time(), self.flow.flow_id))
        else:
            self._finish("error", duration_ms, error=str(exc))
            _exec("UPDATE flow_executions SET last_active=?, node_count="
                  "node_count+1 WHERE flow_id=?",
                  (time.time(), self.flow.flow_id))
        return False  # 异常照常向上抛（业务语义优先）


# ── Flow（流程对象） ────────────────────────────────────────────────

class Flow:
    """一次功能执行的追踪句柄（跨线程显式传递，线程安全）。"""

    def __init__(self, module: str, feature: str, friendly: str,
                 trigger: str = "", input_summary: str = "",
                 detail: str = ""):
        self.module = module
        self.feature = feature
        self.friendly = friendly
        self.flow_id = (f"{module}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                        f"-{uuid.uuid4().hex[:6]}")
        self.started_at = time.time()
        self._node_seq = 0
        self._ended = False
        self._res_start = resource_snapshot()

        _exec(
            "INSERT INTO flow_executions (flow_id, module, feature, status,"
            " friendly, trigger, detail, input_summary, started_at,"
            " last_active, resource_start) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.flow_id, module, feature, "running", friendly, trigger,
             detail, input_summary, self.started_at, self.started_at,
             _res_str(self._res_start)))
        with _registry_lock:
            _active_flows[self.flow_id] = self

        from .event_log import log_event
        log_event(module, "flow_start", friendly,
                  level="info", detail=f"flow_id={self.flow_id} {detail}",
                  trace_id=self.flow_id)

    def node(self, name: str, *, input_summary: str = "",
             friendly: str = "", module: str = "",
             detail: str = "", start_at: float | None = None) -> _NodeCtx:
        """开启一个流程节点（with flow.node(...) as n:）。

        start_at：回溯型节点（如「排队等待」）的起始时刻——默认节点
        从 with 进入时计时，排队节点需回溯到流程触发时刻。
        """
        n = _NodeCtx(self, name, self._node_seq, input_summary=input_summary,
                     friendly=friendly, module=module, detail=detail)
        if start_at is not None:
            n.started_at = start_at
        return n

    def end(self, status: str = "success", *, output_summary: str = "",
            error_code: str = "", error_detail: str = "") -> None:
        """结束流程（幂等：重复调用静默忽略）。"""
        if self._ended:
            return
        self._ended = True
        now = time.time()
        duration_ms = int((now - self.started_at) * 1000)
        _exec(
            "UPDATE flow_executions SET status=?, ended_at=?, duration_ms=?,"
            " output_summary=?, error_code=?, error_detail=?, last_active=?,"
            " resource_end=? WHERE flow_id=?",
            (status, now, duration_ms, output_summary, error_code,
             error_detail, now, _res_str(resource_snapshot()), self.flow_id))
        with _registry_lock:
            _active_flows.pop(self.flow_id, None)
        _current_flow.set(None)

        from .event_log import log_event
        level = ("success" if status == "success"
                 else "error" if status == "error" else "warning")
        log_event(
            self.module, "flow_end",
            f"{self.friendly} · "
            f"{'完成' if status == 'success' else status}",
            level=level,
            detail=f"flow_id={self.flow_id} duration={duration_ms}ms"
                   f"{f' out={output_summary}' if output_summary else ''}"
                   f"{f' error={error_code}' if error_code else ''}",
            duration_ms=duration_ms,
            trace_id=self.flow_id)

    def attach(self) -> Flow:
        """在当前线程/协程上下文绑定此流程（contextvars 传播）。"""
        _current_flow.set(self)
        return self


# ── 公共 API ────────────────────────────────────────────────────────

def start_flow(module: str, feature: str, friendly: str, *,
               trigger: str = "", input_summary: str = "",
               detail: str = "") -> Flow:
    """启动一次功能执行追踪（同时绑定到当前 contextvars 上下文）。"""
    flow = Flow(module, feature, friendly, trigger=trigger,
                input_summary=input_summary, detail=detail)
    _current_flow.set(flow)
    return flow


def current_flow() -> Flow | None:
    """当前上下文的活跃流程（asyncio 链自动继承；线程需 attach）。"""
    return _current_flow.get()


def flow_node(name: str, *, input_summary: str = "", friendly: str = "",
              module: str = "") -> _NodeCtx | Any:
    """便捷节点：挂到 current_flow()；无活跃流程时返回哑节点（降级）。

    哑节点保证埋点代码在无流程上下文（历史路径/测试）时零开销通过。
    """
    flow = _current_flow.get()
    if flow is None or flow._ended:
        return _NullNode()
    return flow.node(name, input_summary=input_summary,
                     friendly=friendly, module=module)


class _NullNode:
    """无流程上下文时的哑节点（全操作 no-op）。"""

    def __enter__(self) -> _NullNode:
        return self

    def __exit__(self, *args) -> bool:
        return False

    def progress(self, note: str = "") -> None:  # noqa: ARG02
        pass

    def output(self, summary: str) -> None:  # noqa: ARG02
        pass

    def detail(self, text: str) -> None:  # noqa: ARG02
        pass


class NullFlow:
    """哨兵流程（跨线程传递丢失/历史路径时降级，全操作 no-op）。

    埋点侧 ``flow = task.pop("_flow", None) or NULL_FLOW`` 保证
    代码无 None 分支；started_at 供排队时长等回溯计算兜底。
    """

    started_at = 0.0
    flow_id = ""

    def node(self, name: str, *, input_summary: str = "",
             friendly: str = "", module: str = "",
             detail: str = "", start_at: float | None = None) -> _NullNode:
        return _NullNode()

    def end(self, status: str = "success", *, output_summary: str = "",
            error_code: str = "", error_detail: str = "") -> None:
        pass

    def attach(self) -> NullFlow:
        return self


NULL_FLOW = NullFlow()


# ── 查询（供 api/logs.py） ──────────────────────────────────────────

def _live_status(row: dict[str, Any]) -> tuple[str, float]:
    """动态状态推导：running → stalled 判定（无心跳超阈值）。

    Returns:
        (status, stalled_seconds)：stalled_seconds 非 0 时为卡住时长。
    """
    if row["status"] != "running":
        return row["status"], 0.0
    last = row.get("last_active") or row["started_at"]
    idle = time.time() - last
    if idle > STALL_THRESHOLD_S:
        return "stalled", idle
    return "running", 0.0


def _flow_to_dict(row: dict[str, Any],
                  last_node: dict[str, Any] | None = None) -> dict[str, Any]:
    status, idle = _live_status(row)
    out = {
        "flow_id": row["flow_id"],
        "module": row["module"],
        "feature": row["feature"],
        "status": status,
        "friendly": row["friendly"],
        "trigger": row.get("trigger", ""),
        "input_summary": row.get("input_summary", ""),
        "output_summary": row.get("output_summary", ""),
        "error_code": row.get("error_code", ""),
        "error_detail": row.get("error_detail", ""),
        "started_at": datetime.fromtimestamp(
            row["started_at"]).isoformat(timespec="milliseconds"),
        "ended_at": (datetime.fromtimestamp(row["ended_at"]).isoformat(
            timespec="milliseconds") if row.get("ended_at") else None),
        "duration_ms": row.get("duration_ms"),
        "node_count": row.get("node_count", 0),
        "resource_start": json.loads(row["resource_start"] or "{}"),
        "resource_end": json.loads(row["resource_end"] or "{}"),
        "stalled_seconds": round(idle, 1),
    }
    if last_node:
        out["last_node"] = {
            "seq": last_node["seq"],
            "node": last_node["node"],
            "status": last_node["status"],
            "started_at": datetime.fromtimestamp(
                last_node["started_at"]).isoformat(timespec="milliseconds"),
            "duration_ms": last_node.get("duration_ms"),
            "input_summary": last_node.get("input_summary", ""),
            "output_summary": last_node.get("output_summary", ""),
            "resource": json.loads(last_node.get("resource") or "{}"),
        }
    return out


def query_flow_traces(
    module: str | None = None,
    feature: str | None = None,
    status: str | None = None,
    keyword: str = "",
    start_ts: float | None = None,
    end_ts: float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """流程列表（新→旧）：按模块/功能/状态/关键词/时间范围筛选。

    status=stalled 特殊语义：动态判定（running 且无心跳超阈值），
    同时 status=running 筛选只返回未卡住的活跃流程。
    """
    where: list[str] = []
    params: list[Any] = []
    if module:
        where.append("module=?")
        params.append(module)
    if feature:
        where.append("feature=?")
        params.append(feature)
    if start_ts is not None:
        where.append("started_at>=?")
        params.append(start_ts)
    if end_ts is not None:
        where.append("started_at<=?")
        params.append(end_ts)
    if keyword:
        where.append("(friendly LIKE ? OR input_summary LIKE ?"
                     " OR output_summary LIKE ? OR error_detail LIKE ?)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw, kw])
    # 库内状态筛选（stalled 在结果侧二次过滤）
    if status and status != "stalled":
        where.append("status=?")
        params.append(status)

    cond = f"WHERE {' AND '.join(where)}" if where else ""
    rows = _query(
        f"SELECT * FROM flow_executions {cond} "
        f"ORDER BY started_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit + offset + 200, 0))  # 多取一些供 stalled 过滤

    out: list[dict[str, Any]] = []
    total = 0
    for row in rows:
        d = _flow_to_dict(row, _last_node_row(row["flow_id"]))
        if status == "stalled" and d["status"] != "stalled":
            continue
        if status == "running" and d["status"] == "stalled":
            continue  # running 筛选不含卡住（用 stalled 单独看）
        total += 1
        if len(out) < limit:
            out.append(d)
    return {"flows": out, "total": total}


def _last_node_row(flow_id: str) -> dict[str, Any] | None:
    rows = _query(
        "SELECT * FROM flow_nodes WHERE flow_id=? ORDER BY seq DESC LIMIT 1",
        (flow_id,))
    return rows[0] if rows else None


def get_flow_trace(flow_id: str) -> dict[str, Any] | None:
    """单流程全明细（含节点链完整输入/输出/资源/异常）。"""
    rows = _query(
        "SELECT * FROM flow_executions WHERE flow_id=?", (flow_id,))
    if not rows:
        return None
    nodes = _query(
        "SELECT * FROM flow_nodes WHERE flow_id=? ORDER BY seq",
        (flow_id,))
    d = _flow_to_dict(rows[0])
    # 卡住分析：running 流程给出最后节点的定位信息（卡在哪/输入/预期）
    if d["status"] in ("running", "stalled"):
        last = nodes[-1] if nodes else None
        if last:
            d["stall_analysis"] = {
                "where": last["node"],
                "node_input": last.get("input_summary", ""),
                "node_last_output": last.get("output_summary", ""),
                "node_status": last["status"],
                "node_running_ms": (int((time.time() - last["started_at"])
                                        * 1000)
                                    if last["status"] == "running"
                                    else last.get("duration_ms")),
                "hint": (f"流程无进展已 {d['stalled_seconds']} 秒，"
                         f"最后环节「{last['node']}」"
                         + ("仍在执行" if last["status"] == "running"
                            else "已完成但后续环节未开始")),
            }
    d["nodes"] = [{
        "seq": n["seq"],
        "node": n["node"],
        "module": n.get("module", ""),
        "status": n["status"],
        "started_at": datetime.fromtimestamp(
            n["started_at"]).isoformat(timespec="milliseconds"),
        "duration_ms": n.get("duration_ms"),
        "input_summary": n.get("input_summary", ""),
        "output_summary": n.get("output_summary", ""),
        "detail": n.get("detail", ""),
        "friendly": n.get("friendly", ""),
        "error": n.get("error", ""),
        "resource": json.loads(n.get("resource") or "{}"),
    } for n in nodes]
    return d


def flow_trace_stats() -> dict[str, Any]:
    """筛选项与状态计数（前端流程面板：模块/功能/状态分布）。"""
    since = time.time() - RETENTION_DAYS * 86400
    rows = _query(
        "SELECT module, feature, status, started_at FROM flow_executions"
        " WHERE started_at>=?", (since,))
    by_status = {"running": 0, "success": 0, "error": 0,
                 "cancelled": 0, "stalled": 0, "orphan": 0}
    modules: dict[str, int] = {}
    features: dict[str, int] = {}
    for r in rows:
        st, _ = _live_status(r)
        by_status[st] = by_status.get(st, 0) + 1
        modules[r["module"]] = modules.get(r["module"], 0) + 1
        features[r["feature"]] = features.get(r["feature"], 0) + 1
    return {
        "total": len(rows),
        "by_status": by_status,
        "modules": sorted(modules),
        "features": sorted(features),
        "stall_threshold_s": STALL_THRESHOLD_S,
    }


# ── 崩溃恢复与清理 ──────────────────────────────────────────────────

def recover_orphans() -> int:
    """进程启动时调用：上一进程遗留的 running 记录标 orphan。

    判定：status=running 但 flow_id 不在本进程活跃注册表。
    """
    rows = _query("SELECT flow_id FROM flow_executions WHERE status='running'")
    orphans = [r["flow_id"] for r in rows
               if r["flow_id"] not in _active_flows]
    for fid in orphans:
        _exec(
            "UPDATE flow_executions SET status='orphan', ended_at=?,"
            " duration_ms=?, error_code='PROCESS_EXIT',"
            " error_detail='后端进程重启，流程状态未知（历史遗留）'"
            " WHERE flow_id=?",
            (time.time(), 0, fid))
        # 遗留 running 节点同样收敛
        _exec(
            "UPDATE flow_nodes SET status='skipped',"
            " error='后端进程重启，节点状态未知' WHERE flow_id=?"
            " AND status='running'", (fid,))
    if orphans:
        logger.info("flow_trace 崩溃恢复：%d 个孤儿流程标记 orphan", len(orphans))
    return len(orphans)


def cleanup_expired() -> dict[str, Any]:
    """删除超过 30 天的流程记录（与事件日志同一保留裁定）。"""
    cutoff = time.time() - RETENTION_DAYS * 86400
    try:
        with _write_lock:
            db = _get_db()
            db.execute("DELETE FROM flow_nodes WHERE flow_id IN "
                       "(SELECT flow_id FROM flow_executions"
                       " WHERE started_at<?)", (cutoff,))
            cur = db.execute(
                "DELETE FROM flow_executions WHERE started_at<?", (cutoff,))
            db.commit()
            n = cur.rowcount or 0
        if n:
            logger.info("flow_trace 过期清理：删除 %d 条流程（超 %d 天）",
                        n, RETENTION_DAYS)
        return {"deleted": n}
    except Exception as exc:  # noqa: BLE001
        logger.warning("flow_trace 清理失败: %s", exc)
        return {"deleted": 0}


def start_cleanup_task() -> None:
    """后台清理线程（幂等）：启动即恢复孤儿 + 每 24h 清理过期。"""
    global _cleanup_started
    with _cleanup_lock:
        if _cleanup_started:
            return
        _cleanup_started = True

    def _loop() -> None:
        while True:
            try:
                cleanup_expired()
            except Exception:  # noqa: BLE001
                logger.exception("flow_trace 清理巡检异常")
            time.sleep(_CLEANUP_INTERVAL_S)

    threading.Thread(target=_loop, name="flow-trace-cleanup",
                     daemon=True).start()
    logger.info("流程追踪服务已启动（保留 %d 天，卡住阈值 %.0fs）",
                RETENTION_DAYS, STALL_THRESHOLD_S)
