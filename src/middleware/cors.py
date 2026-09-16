"""OmniSpace AI v2.1 CORS 中间件配置（规格 §6.1 安全架构 L4 / §14 约束2）。

仅允许本地来源（http://localhost:* 和 http://127.0.0.1:*），
拒绝一切外部 Origin 的跨域请求。提供 setup_cors(app) 函数。

规格引用：
  - §6.1 L4：CORS 仅本地
  - §14 约束2：服务绑定 127.0.0.1，不可更改
"""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from ..config import PORT
from .error_handler import error

log = logging.getLogger("omnispace.cors")

# 允许的 Origin 正则：仅 localhost / 127.0.0.1 / 0.0.0.0 任意端口
_ALLOWED_ORIGIN_PATTERN = re.compile(
    r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?$"
)

# 显式白名单（含常见端口，供 CORSMiddleware allow_origins 使用）
_DEFAULT_ORIGINS = [
    "http://localhost",
    "http://localhost:5800",
    "http://localhost:3000",
    "http://localhost:8080",
    "http://127.0.0.1",
    "http://127.0.0.1:5800",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:8080",
]

# 允许的 HTTP 方法
_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

# 允许的请求头
_ALLOWED_HEADERS = [
    "Content-Type",
    "Authorization",
    "X-Request-ID",
    "X-Session-ID",
]

# 允许暴露给前端的响应头
_EXPOSE_HEADERS = [
    "X-Request-ID",
    "X-RateLimit-Remaining",
]


def is_allowed_origin(origin: str | None) -> bool:
    """检查 Origin 是否属于允许的本地来源。"""
    if not origin:
        return False
    return bool(_ALLOWED_ORIGIN_PATTERN.match(origin))


async def ws_origin_guard(websocket: WebSocket) -> bool:
    """WebSocket Origin 校验（审计 R3-SEC1：防 CSWSH 跨站 WebSocket 劫持）。

    CORS 中间件不覆盖 WS 握手，恶意网页可 new WebSocket("ws://127.0.0.1:5800/ws")
    窃取硬件遥测/任务进度广播。此处复用 HTTP 侧同一套本地来源白名单：
      - 有 Origin 且不匹配 → close(1008 Policy Violation)，返回 False
      - 无 Origin（非浏览器客户端）→ 放行（与 cors_origin_guard 同语义）
    调用方须在 accept() 之前执行，返回 False 时不得再 accept/收发。
    """
    origin = websocket.headers.get("origin")
    if origin and not is_allowed_origin(origin):
        log.warning("WS 来源被拒绝（非本地 Origin）: %s", origin)
        await websocket.close(code=1008)
        return False
    return True


def setup_cors(app: FastAPI, extra_origins: list[str] | None = None) -> None:
    """为 FastAPI 应用配置 CORS 中间件。

    采用「白名单 + 正则双保险」策略：
      1. CORSMiddleware 的 allow_origins 处理已知端口
      2. 自定义中间件对动态端口做正则校验，拒绝外部 Origin

    Args:
        app: FastAPI 应用实例
        extra_origins: 额外允许的 Origin（仍需匹配本地正则才会生效）
    """
    origins = list(_DEFAULT_ORIGINS)
    if extra_origins:
        for o in extra_origins:
            if is_allowed_origin(o) and o not in origins:
                origins.append(o)

    # 确保当前端口在白名单中
    current = [f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"]
    for o in current:
        if o not in origins:
            origins.append(o)

    # ── 标准 CORSMiddleware ──────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        # P7：启动页（127.0.0.1:5850-5869）直连后端激活接口——复用下方
        # 自定义中间件同款「仅本地任意端口」严格正则，安全性不降级
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?$",
        allow_credentials=True,
        allow_methods=_ALLOWED_METHODS,
        allow_headers=_ALLOWED_HEADERS,
        expose_headers=_EXPOSE_HEADERS,
    )

    # ── 动态 Origin 校验中间件（拦截非常规端口的本地请求放行，外部拒绝）──
    @app.middleware("http")
    async def cors_origin_guard(request: Request,
                                call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        origin = request.headers.get("origin")
        # 非浏览器请求（无 Origin 头）直接放行
        if origin and not is_allowed_origin(origin):
            # 审计 R1-03：CORS 拒绝走 error() 统一信封（语义码
            # SYSTEM_UNAUTHORIZED），HTTP 状态保持 403（中间件层合理例外）
            resp = error(
                "SYSTEM_UNAUTHORIZED",
                "跨域请求被拒绝：仅允许本地访问",
                detail={"origin": origin},
            )
            resp.status_code = 403
            return resp
        return await call_next(request)

    log.info("CORS 中间件已配置（允许来源: %s）", origins)
# 本项目仅供学习使用，商业授权请+Q 3559331368
