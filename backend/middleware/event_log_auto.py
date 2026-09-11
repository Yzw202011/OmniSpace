"""API 事件日志自动兜底（自愈批2，2026-09-11 自愈与横切内建方案）。

设计（docs/自愈与横切内建方案-2026-09-10.md §批2）：把「这次接口成没成」
自动写进用户时间线（logs/events/*.jsonl → 日志页），新端点零打点也有底。
与业务内 log_event 互补不重复——业务打点记「发生了什么」（模型加载/降级），
本层记「接口成没成」。

记录范围（防刷屏三条件，方案 P2 拍板=变更+失败+慢）：
  ① 变更类请求：POST/PUT/DELETE/PATCH（复用 request_context 的活动语义）
  ② 任何失败信封：success:false（方法不限，GET 失败也要让用户看见）
  ③ 慢请求：耗时 ≥ EVENT_LOG_SLOW_MS（config.yaml event_log.slow_request_ms）
GET 轮询成功不记（防刷屏）。

失败识别原理：ok()/error() 序列化的信封 success 恒为首键
（middleware/error_handler.py），因此只需在内存里暂存 JSON 响应的前
_BODY_CAPTURE_CAP 字节即可无损判定，并顺手取出 code/message/suggestion
拼大白话（suggestion 自批1 起恒在）。非 JSON / 超帽时退化用 HTTP 状态
+ success:false 前缀嗅探兜底。

挂载（main.py）：注册在 RequestContextMiddleware 之后 = 其外层、
TrustedHost 之前 = 其内层；异常处理器在最内层先行渲染信封，本层在
响应流结束时看到的是最终结果。仅 /api/ 路径参与，静态资源与 /health 不记。
"""
from __future__ import annotations

import json
import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import EVENT_LOG_SLOW_MS

# 与 request_context._ACTIVITY_METHODS 同义（复用其语义，不 import 私有名）
_ACTIVITY_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

#: JSON 响应体暂存上限（信封 success/message/suggestion 足够；超帽走嗅探兜底）
_BODY_CAPTURE_CAP = 4096

#: 路由前缀 → 模块标签（对齐前端 LogsPage MODULE_NAMES 与既有 log_event 用词）
_PREFIX_MODULES: tuple[tuple[str, str], ...] = (
    ("/api/v1/dialog", "dialog"),
    ("/api/v1/draw", "paint"),
    ("/api/v1/manga", "manga"),
    ("/api/v1/learn", "learn"),
    ("/api/v1/learning", "learn"),
    ("/api/v1/knowledge", "knowledge"),
    ("/api/v1/browser", "browser"),
    ("/api/v1/models", "models"),
    ("/api/v1/style", "style"),
    ("/api/v1/art", "vision"),
    ("/api/v1/voice", "voice"),
    ("/api/v1/hardware", "hardware"),
    ("/api/v1/logs", "system"),
    ("/api/v1/system", "system"),
    ("/api/v1/license", "license"),
    ("/api/v1/novel", "novel"),
    ("/api/v1/cloud", "cloud"),
)

#: 模块标签 → 中文（friendly 用；与前端 LogsPage MODULE_NAMES 保持镜像）
_MODULE_ZH: dict[str, str] = {
    "dialog": "对话",
    "paint": "绘画",
    "manga": "漫剧",
    "learn": "知识学习",
    "knowledge": "知识库",
    "browser": "浏览器",
    "models": "模型",
    "style": "风格",
    "vision": "视觉工具",
    "voice": "语音",
    "hardware": "硬件",
    "system": "系统",
    "license": "激活",
    "novel": "小说",
    "cloud": "云端",
}

#: 可选精修：(method, path) → 中文动作名。未登记走通用模板兜底
#: （这正是 C 内建的意义：忘了登记也有底线）。登记前先核实路径真实存在。
EVENT_LABELS: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/draw/generate"): "图片生成",
    ("POST", "/api/v1/paint/generate"): "图片生成",
    ("POST", "/api/v1/models/load"): "模型加载",
    ("POST", "/api/v1/models/unload"): "模型卸载",
    ("POST", "/api/v1/manga/video/generate"): "视频生成",
    ("POST", "/api/v1/video/generate"): "视频生成",
}


def _module_for(path: str) -> str:
    """路径 → 模块标签；未登记前缀取 /api/v1/<第一段> 兜底（新模块自动可见）。"""
    for prefix, module in _PREFIX_MODULES:
        if path.startswith(prefix):
            return module
    rest = path[len("/api/v1/"):] if path.startswith("/api/v1/") else path.lstrip("/")
    return rest.split("/", 1)[0] or "system"


def _label_for(method: str, path: str) -> str:
    """已登记端点的中文动作名；未登记返回空串（走通用模板）。"""
    return EVENT_LABELS.get((method, path), "")


def _fmt_seconds(duration_ms: float) -> str:
    return f"{duration_ms / 1000:.1f} 秒"


def _sniff_failure(body: bytes) -> bool:
    """信封 success 恒为首键：前 64 字节即可判定失败（超帽截断时的兜底）。"""
    head = body[:64]
    return b'"success":false' in head or b'"success": false' in head


def _parse_envelope(body: bytes) -> dict[str, Any]:
    """解析暂存的信封片段 → {success, code, message, suggestion}。

    success 取值：True/False（完整解析）；"sniffed"（超帽但前缀嗅探到失败）；
    "unknown"（非信封 JSON）。
    """
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        if _sniff_failure(body):
            return {"success": "sniffed", "code": "", "message": "", "suggestion": ""}
        return {"success": "unknown", "code": "", "message": "", "suggestion": ""}
    if isinstance(data, dict) and isinstance(data.get("success"), bool):
        raw_err = data.get("error")
        err: dict[str, Any] = raw_err if isinstance(raw_err, dict) else {}
        return {
            "success": data["success"],
            "code": str(err.get("code") or ""),
            "message": str(err.get("message") or ""),
            "suggestion": str(err.get("suggestion") or ""),
        }
    if isinstance(data, dict) and _sniff_failure(body):
        return {"success": "sniffed", "code": "", "message": "", "suggestion": ""}
    return {"success": "unknown", "code": "", "message": "", "suggestion": ""}


class _CaptureState:
    """单请求响应捕获状态（send 包装器的可变载体）。"""

    __slots__ = ("status", "capture", "body", "done")

    def __init__(self) -> None:
        self.status = 0
        self.capture = False
        self.body = b""
        self.done = False


def _is_json_response(message: Message) -> bool:
    """响应头 Content-Type 是否 JSON（只对 JSON 暂存响应体）。"""
    headers = message.get("headers") or []
    for k, v in headers:
        name = k.decode("latin-1") if isinstance(k, bytes) else str(k)
        if name.lower() == "content-type":
            value = v.decode("latin-1") if isinstance(v, bytes) else str(v)
            return value.lower().startswith("application/json")
    return False


class EventLogAutoMiddleware:
    """纯 ASGI 中间件：变更类请求/失败信封/慢请求自动 log_event。

    任何自身异常都必须吞掉（log_event 永不抛错的同款纪律）——
    观测层故障不允许影响业务响应。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method") or "GET").upper()
        start = time.perf_counter()
        cap = _CaptureState()

        async def send_wrapper(message: Message) -> None:
            try:
                if message["type"] == "http.response.start":
                    cap.status = int(message.get("status") or 0)
                    cap.capture = _is_json_response(message)
                elif message["type"] == "http.response.body":
                    if cap.capture and len(cap.body) < _BODY_CAPTURE_CAP:
                        cap.body += message.get("body", b"")
                        cap.body = cap.body[:_BODY_CAPTURE_CAP]
                    if not message.get("more_body", False):
                        cap.done = True
                        self._record(scope, method, path, start, cap)
            except Exception:  # noqa: BLE001 - 观测层异常绝不影响响应
                pass
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _record(self, scope: Scope, method: str, path: str,
                start: float, cap: _CaptureState) -> None:
        """响应完成时判定三条件并落档（本方法自身不许抛错）。"""
        try:
            duration_ms = max(0.0, (time.perf_counter() - start) * 1000)
            env = _parse_envelope(cap.body) if cap.capture and cap.body else {
                "success": "unknown", "code": "", "message": "", "suggestion": ""}
            is_failure = env["success"] is False or (
                env["success"] == "sniffed") or (
                env["success"] == "unknown" and cap.status >= 400)
            is_activity = method in _ACTIVITY_METHODS
            # 模块级常量按名引用（测试可 monkeypatch 阈值）
            is_slow = duration_ms >= EVENT_LOG_SLOW_MS
            if not (is_activity or is_failure or is_slow):
                return

            module = _module_for(path)
            label = _label_for(method, path)
            zh = _MODULE_ZH.get(module, module)
            request_id = str((scope.get("state") or {}).get("request_id") or "")

            if is_failure:
                level = "error"
                event = "api_failed"
                if env["message"]:
                    friendly = f"{zh}接口{label or '调用'}失败：{env['message']}"
                elif label:
                    friendly = f"{zh}接口{label}失败（HTTP {cap.status}）"
                else:
                    friendly = f"{zh}接口 {method} {path} 失败（HTTP {cap.status}）"
                if env["suggestion"]:
                    friendly += f"；{env['suggestion']}"
            else:
                level = "success"
                event = "api_call"
                if label:
                    friendly = f"{zh}·{label}完成，用时 {_fmt_seconds(duration_ms)}"
                else:
                    friendly = (f"{zh}接口 {method} {path} 完成，"
                                f"用时 {_fmt_seconds(duration_ms)}")

            detail = f"{method} {path} → HTTP {cap.status}"
            if env["code"]:
                detail += f" code={env['code']}"
            if request_id:
                detail += f" request_id={request_id}"

            from ..services.event_log import log_event
            log_event(module, event, friendly, level=level, detail=detail,
                      duration_ms=duration_ms, trace_id=request_id)
        except Exception:  # noqa: BLE001 - 观测层异常绝不影响业务
            pass
