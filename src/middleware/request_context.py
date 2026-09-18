"""OmniSpace AI v2.5.0 请求上下文（文档B/D：统一响应 meta 字段支撑）。

为统一响应信封的 meta{request_id, timestamp, duration_ms} 提供请求级上下文：
  - 每个 HTTP 请求进入时生成 uuid4 request_id 并记录起始时间；
  - ok()/error() 通过 contextvars 读取，无需各路由显式传参；
  - WebSocket 等非 HTTP 场景缺失上下文时自动退化为独立 request_id + duration_ms=0。

零信任说明：request_id 仅用于日志追踪与排障，不作为任何鉴权依据。
"""
from __future__ import annotations

import contextvars
import logging
import time
import uuid
from datetime import datetime

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "omnispace_request_id", default=""
)
_start_ts: contextvars.ContextVar[float] = contextvars.ContextVar(
    "omnispace_request_start", default=0.0
)

# 用户活动上报（审计 R2-B02 配套）：变更类 HTTP 请求视为真实用户操作，
# 刷新学习调度器空闲计时。GET 轮询（状态栏/列表刷新）不计，避免
# 后台轮询把"用户空闲"误判为活跃。导入惰性化，异常静默（不影响请求链）。
_ACTIVITY_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def _report_user_activity(scope: Scope) -> None:
    if scope.get("method") not in _ACTIVITY_METHODS:
        return
    path = str(scope.get("path") or "")
    if not path.startswith("/api/"):
        return
    try:
        from ..services.learning_scheduler import get_learning_scheduler
        get_learning_scheduler().notify_user_activity()
    except Exception:  # noqa: BLE001 - 活动上报失败不影响请求
        log.debug("_report_user_activity: 降级忽略", exc_info=True)


class RequestContextMiddleware:
    """纯 ASGI 中间件：为每个 HTTP 请求建立追踪上下文。

    放在中间件链最内层（最后注册），确保业务 handler 与异常处理器
    均能读到上下文；WebSocket 作用域直接放行（WS 帧无 envelope）。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        _report_user_activity(scope)
        rid = uuid.uuid4().hex
        token_id = _request_id.set(rid)
        token_ts = _start_ts.set(time.perf_counter())
        # 写入 scope 便于异常处理器/日志取用
        scope["state"] = {**scope.get("state", {}), "request_id": rid}  # type: ignore[arg-type]
        try:
            await self.app(scope, receive, send)
        finally:
            _request_id.reset(token_id)
            _start_ts.reset(token_ts)


def current_request_id() -> str:
    """返回当前请求 request_id；无上下文（WS/后台任务）时生成独立 ID。"""
    rid = _request_id.get()
    return rid if rid else uuid.uuid4().hex


def current_duration_ms() -> int:
    """返回当前请求已耗时毫秒；无上下文时返回 0。"""
    start = _start_ts.get()
    if not start:
        return 0
    return max(0, int((time.perf_counter() - start) * 1000))


def utc_now_iso() -> str:
    """信封时间戳（文档D：meta.timestamp）。

    批3（2026-09-18）时区统一：**本地时间带偏移**（如
    2026-09-18T00:40:05+08:00）——与后端日志/前端显示同口径，跨端对账
    不再差 8 小时（audit-2026-09-18 P1：旧 UTC-Z 格式与本地日志日期都
    对不上）。ISO 8601 带偏移对 new Date()/fromisoformat 均可解析。
    函数名保留 utc_now_iso 仅减改动面，语义以本注记为准。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")
# 本项目仅供学习使用，商业授权请+Q 3559331368
