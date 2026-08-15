"""OmniSpace AI v2.1 限流中间件（规格 §7 server.rate_limit / §4.1 中间件层）。

限制每个端点 100 requests/minute per client，使用内存滑动窗口计数器。
Redis 不可用时降级为本地内存计数。

规格引用：
  - §7 server.rate_limit = 100（requests/minute per endpoint）
  - §8 错误码 10002：请求过于频繁，请稍后再试
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import FastAPI, Request

from ..config import API_PREFIX, RATE_LIMIT
from .error_handler import error

log = logging.getLogger("omnispace.ratelimit")


class RateLimiter:
    """基于滑动窗口的内存限流器。

    每个 (client_ip, endpoint) 组合维护一个时间戳队列，
    超出窗口的旧时间戳被定期清理。
    """

    def __init__(self, max_requests: int = RATE_LIMIT,
                 window_seconds: int = 60) -> None:
        self._max_requests = max_requests
        self._window = window_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_cleanup = time.time()

    def _key(self, client_ip: str, endpoint: str) -> str:
        return f"{client_ip}:{endpoint}"

    def _cleanup_expired(self, key: str, now: float) -> None:
        """清理指定桶中过期的时间戳。"""
        bucket = self._buckets[key]
        cutoff = now - self._window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def _global_cleanup(self, now: float) -> None:
        """定期全局清理空桶，防止内存泄漏（每 5 分钟一次）。"""
        if now - self._last_cleanup < 300:
            return
        self._last_cleanup = now
        empty_keys = [k for k, v in self._buckets.items() if not v]
        for k in empty_keys:
            del self._buckets[k]

    def check(self, client_ip: str, endpoint: str) -> tuple[bool, int, int]:
        """检查是否允许请求。

        Returns:
            (allowed, remaining, retry_after_seconds)
        """
        now = time.time()
        key = self._key(client_ip, endpoint)
        with self._lock:
            self._global_cleanup(now)
            self._cleanup_expired(key, now)
            bucket = self._buckets[key]

            if len(bucket) >= self._max_requests:
                # 计算最早请求何时过期
                oldest = bucket[0]
                retry_after = int(self._window - (now - oldest)) + 1
                retry_after = max(retry_after, 1)
                return False, 0, retry_after

            bucket.append(now)
            remaining = self._max_requests - len(bucket)
            return True, remaining, 0

    def reset(self, client_ip: str | None = None,
              endpoint: str | None = None) -> None:
        """重置限流计数。"""
        with self._lock:
            if client_ip is None and endpoint is None:
                self._buckets.clear()
            else:
                keys_to_remove = []
                for k in self._buckets:
                    ip, ep = k.split(":", 1)
                    if (client_ip is None or ip == client_ip) and \
                       (endpoint is None or ep == endpoint):
                        keys_to_remove.append(k)
                for k in keys_to_remove:
                    del self._buckets[k]

    def stats(self) -> dict:
        """返回限流器统计信息。"""
        with self._lock:
            return {
                "max_requests": self._max_requests,
                "window_seconds": self._window,
                "active_buckets": len(self._buckets),
            }


# ═══════════════════════════════════════════════════════════════════
#  全局限流器单例
# ═══════════════════════════════════════════════════════════════════

_limiter: Optional[RateLimiter] = None
_limiter_lock = threading.Lock()


def get_limiter() -> RateLimiter:
    """获取全局限流器单例。"""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = RateLimiter()
    return _limiter


# ── 白名单路径（不限流）──────────────────────────────────────
# Whitelist paths that bypass rate limiting (health checks, favicon)。
# 审计 R1-14：静态资源经 _ApiAwareMount 挂载在根路径（main.py），
# 不存在 /static 前缀路由，移除该残留白名单项。
_WHITELIST_PREFIXES = (
    "/favicon.ico",
    "/health",
)

# ── 只读媒体 GET 独立限流桶（审计 R3-P3 媒体限流桶过粗）────────────
# 资产库缩略图 / 关键帧版本图经 /manga/media/{relpath} 回读，前端批量
# 渲染资产库/版本列表时请求密集；媒体 relpath 中的日期目录（纯数字段）
# 与 uuid.hex 文件名会被 _normalize_path 归一化，大量不同文件共享同一
# 计数桶，100/min 通用限额会误伤正常浏览。此类路径为只读静态产物回读
# （无状态变更），放宽到 600/min 独立桶，与其余写/查询端点隔离。
_MEDIA_GET_PREFIXES = (
    f"{API_PREFIX}/manga/media/",
)
_MEDIA_RATE_LIMIT = 600


def _get_client_ip(request: Request) -> str:
    """获取客户端 IP。

    审计 R3-P3：回环直连部署（规格 §14 约束2 仅绑 127.0.0.1，无前置
    反向代理）下，X-Forwarded-For 可被客户端任意伪造以不断重置限流桶，
    故优先取真实 TCP 对端 request.client.host；仅当其缺失（如某些
    进程内 TestClient 场景）时才回退 XFF 头。
    """
    if request.client and request.client.host:
        return request.client.host
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return "unknown"


# ── 路径归一化（审计 P1-1）────────────────────────────────────
# 本中间件在路由匹配之前执行，request.scope["route"] 尚不存在，
# 直接拿 request.url.path 会让 /api/v1/models/<id1> 与 /api/v1/models/<id2>
# 落入不同计数桶，per-endpoint 限流形同虚设。此处把 uuid / 纯数字 /
# 长 hex 段归一化为 {}，使参数化路径共享同一桶。
_RE_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_RE_HEX32 = re.compile(r"^[0-9a-fA-F]{16,}$")   # 无连字符 uuid.hex / 长 hex id
_RE_DIGITS = re.compile(r"^\d+$")               # 纯数字 id


def _normalize_path(path: str) -> str:
    """把路径中的动态参数段替换为 {}，得到稳定的端点标识。"""
    parts = []
    for seg in path.split("/"):
        if seg and (_RE_UUID.match(seg) or _RE_HEX32.match(seg)
                    or _RE_DIGITS.match(seg)):
            parts.append("{}")
        else:
            parts.append(seg)
    return "/".join(parts)


def _get_endpoint(request: Request) -> str:
    """获取端点标识（路由模板优先；中间件阶段无路由信息时归一化 URL）。"""
    route = request.scope.get("route")
    if route and hasattr(route, "path"):
        return route.path
    return _normalize_path(request.url.path)


def setup_rate_limit(app: FastAPI, max_requests: int = RATE_LIMIT,
                     window_seconds: int = 60) -> RateLimiter:
    """为 FastAPI 应用配置限流中间件。

    Args:
        app: FastAPI 应用实例
        max_requests: 窗口内最大请求数（默认 100）
        window_seconds: 窗口大小秒数（默认 60）

    Returns:
        RateLimiter 实例
    """
    limiter = RateLimiter(max_requests=max_requests,
                          window_seconds=window_seconds)
    # 只读媒体 GET 独立桶（审计 R3-P3），与通用桶计数互不影响
    media_limiter = RateLimiter(max_requests=_MEDIA_RATE_LIMIT,
                                window_seconds=window_seconds)

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        path = request.url.path
        # 白名单放行
        if any(path.startswith(p) for p in _WHITELIST_PREFIXES):
            return await call_next(request)

        client_ip = _get_client_ip(request)
        endpoint = _get_endpoint(request)
        # 只读媒体 GET 走独立放宽桶，其余请求维持通用限额
        is_media_get = (request.method == "GET"
                        and any(path.startswith(p)
                                for p in _MEDIA_GET_PREFIXES))
        active_limiter = media_limiter if is_media_get else limiter
        active_limit = _MEDIA_RATE_LIMIT if is_media_get else max_requests
        allowed, remaining, retry_after = active_limiter.check(
            client_ip, endpoint)

        if not allowed:
            log.warning("限流触发: %s @ %s（超出 %d req/%ds）",
                        client_ip, endpoint, active_limit, window_seconds)
            # 审计 R1-02：429 响应走 error() 统一信封（语义码
            # SYSTEM_RATE_LIMITED），HTTP 状态保持 429（中间件层合理例外）
            resp = error(
                "SYSTEM_RATE_LIMITED",
                detail={
                    "retry_after": retry_after,
                    "limit": active_limit,
                    "window": window_seconds,
                },
            )
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry_after)
            resp.headers["X-RateLimit-Limit"] = str(active_limit)
            resp.headers["X-RateLimit-Remaining"] = "0"
            return resp

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(active_limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

    log.info("限流中间件已配置: %d req/%ds per endpoint", max_requests, window_seconds)
    return limiter
