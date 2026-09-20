# 本项目仅供学习使用，商业授权请+Q 3559331368
"""LAN 访问鉴权中间件（批2-1，2026-09-18）：局域网暴露的令牌闸。

背景（四轮审计 P1）：OMNISPACE_ALLOW_LAN=1 时同网段任何设备可全功能
裸奔（含文件操作类端点）。本闸语义：

- **激活条件**：绑定非回环地址（config.HOST 非 loopback）。纯本地
  （127.0.0.1）= 零行为变化，闸不参与。
- **豁免面**（远程也可达，最小集）：静态前端资源（非 /api、/ws 路径，
  远程浏览器要先能加载 UI 才能输令牌）、/health、/api/quit（关机链）。
- **令牌**：绑定非回环时首启自动生成（secrets.token_urlsafe），持久于
  system_settings 表 kv `lan.token`；服务器本机（回环请求）免鉴权，
  设置页可读码展示；远程设备输一次存 localStorage，随请求头
  ``X-Omni-Token``（HTTP）或 ``?token=``（WebSocket）携带。
- **纯 ASGI 实现**：同时拦截 http 与 websocket 两种 scope（FastAPI 的
  http middleware 拦不住 WS）；拒绝信封与全局错误格式一致。

令牌读取端点：GET /api/v1/system/lan-token（回环或持有效令牌可读，
见 api/system.py——持令牌者读令牌无提权面）。
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

from ..config import HOST

log = logging.getLogger("omnispace.middleware.lan_auth")

_LOOPBACK = ("127.0.0.1", "localhost", "::1")
_HEADER = b"x-omni-token"
_KV_KEY = "lan.token"
# 豁免路径前缀（远程可达的最小集）：健康探针/优雅退出/静态资源天然豁免
_EXEMPT_PREFIXES = ("/health", "/api/quit")


def lan_auth_enabled() -> bool:
    """闸激活判定：绑定非回环（ALLOW_LAN 豁免后的实际状态）。"""
    return HOST not in _LOOPBACK


def get_lan_token() -> str:
    """取（首次则生成并持久化）LAN 令牌。

    B1 根修（2026-09-21）：迁移 settings_kv 单源（JSON 编码）；旧裸串值
    json.loads 失败→None→重新生成（LAN 已下线拍板，令牌更换无影响）。
    """
    from ..data.settings_kv import kv_get, kv_set
    token = kv_get(_KV_KEY)
    if isinstance(token, str) and token.strip():
        return token.strip()
    token = secrets.token_urlsafe(24)
    kv_set(_KV_KEY, token)
    return token


def _client_host(scope: dict[str, Any]) -> str:
    client = scope.get("client") or ("", 0)
    return str(client[0]) if client else ""


def _token_of(scope: dict[str, Any]) -> str:
    """从请求中提取令牌：HTTP 头 X-Omni-Token / WS 查询参数 token。"""
    headers = {k.lower(): v for k, v in scope.get("headers", [])}
    raw = headers.get(_HEADER, b"").decode("latin-1").strip()
    if raw:
        return raw
    qs = scope.get("query_string", b"").decode("latin-1")
    for part in qs.split("&"):
        if part.startswith("token="):
            from urllib.parse import unquote
            return unquote(part[6:]).strip()
    return ""


def _path_exempt(path: str) -> bool:
    if not path.startswith(("/api", "/ws")):
        return True  # 静态前端资源（含 /）
    return any(path == p or path.startswith(p) for p in _EXEMPT_PREFIXES)


def _reject_body(scope_type: str, path: str) -> bytes:
    body = {
        "success": False, "data": None,
        "error": {
            "code": "LAN_AUTH_REQUIRED",
            "message": "局域网访问需要令牌：请在服务器本机的"
                       "「设置 → 通用」查看访问令牌，填入本设备后重试",
            "detail": {"path": path, "t": time.time()},
            "suggestion": "在服务器机器上打开设置页读取令牌；本机访问不受影响",
        },
        "meta": {"request_id": "lan-auth", "timestamp": time.time()},
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class LanAuthMiddleware:
    """纯 ASGI 中间件：非回环绑定时，HTTP+WS 统一令牌闸（回环免检）。"""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._token: str | None = None

    async def __call__(self, scope: dict[str, Any],
                       receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket") \
                or not lan_auth_enabled():
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if _path_exempt(path) or _client_host(scope) in _LOOPBACK:
            await self.app(scope, receive, send)
            return
        if self._token is None:
            self._token = get_lan_token()
        if _token_of(scope) == self._token:
            await self.app(scope, receive, send)
            return
        log.warning("LAN 令牌拒绝: %s %s（客户端 %s）",
                       scope.get("type"), path, _client_host(scope))
        if scope["type"] == "http":
            body = _reject_body("http", path)
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type",
                                     b"application/json; charset=utf-8"),
                                    (b"content-length",
                                     str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
        else:
            await send({"type": "websocket.close", "code": 4401,
                        "reason": "LAN token required"})
