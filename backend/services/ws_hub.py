"""OmniSpace AI v2.1 WebSocket 消息中枢（规格 §2.2 / §5.3 断线重连配套）。

前端协议（frontend/src/hooks/useWebSocket.ts）：
  URL: ws://host/ws
  客户端 → 服务端: {"type": "ping"|"subscribe_system"|"unsubscribe_system"|...}
  服务端 → 客户端: {"type": "pong"|"task_progress"|"task_complete"|"task_error"|
                    "system_status"|"notification"|..., "data": {...}}

内部各服务（api.draw / services.lora_training_service /
services.browser_agent_service）经各自的 set_ws_broadcaster 注入
WsHub.broadcast(payload)（线程安全，可从任意工作线程调用），hub 将内部
事件翻译成前端协议后广播给全部连接；发送 subscribe_system 的连接每 2 秒
额外收到一次 system_status 硬件遥测（复用 api.hardware._realtime_data）。

单例用法::

    from backend.services.ws_hub import get_ws_hub
    hub = get_ws_hub()
    hub.bind_loop(asyncio.get_running_loop())   # 应用启动时
    hub.start_telemetry()                        # 启动遥测推送
    draw.set_ws_broadcaster(hub.broadcast)       # 注入各服务广播器
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("omnispace.ws_hub")

# 前端协议允许的 15 种服务端消息类型（与 useWebSocket.ts 保持一致）
_FRONTEND_TYPES = {
    "pong", "task_progress", "task_preview", "task_complete", "task_error",
    "system_status", "vram_warning", "download_progress", "notification",
    "parallel_progress", "collision_alert", "segment_progress",
    "update_available", "sync_complete", "quality_degraded",
}

_TELEMETRY_INTERVAL_S = 2.0


def _translate(payload: Any) -> dict:
    """内部事件 → 前端协议 {"type":..., "data":...}。

    映射规则：
    - draw {"type":"progress","module":"paint","data":{...}} → task_progress
    - lora {"type":"status","module":"learn","data":{"event":...}}
        training_completed → task_complete；training_failed → task_error；
        其余事件 → task_progress
    - browser_agent {"type":"learn_progress"|"learn_action"|...} → task_progress；
        learn_session_end → task_complete
    - 已是前端协议类型的消息原样透传（规整 data）
    - 未知类型 → notification
    """
    if not isinstance(payload, dict):
        return {"type": "notification", "data": {"raw": str(payload)}}
    ptype = str(payload.get("type") or "")
    module = str(payload.get("module") or "")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

    if ptype == "progress":  # api.draw
        return {"type": "task_progress", "data": {"module": module, **data}}

    if ptype == "status":    # services.lora_training_service
        event = str(data.get("event") or "")
        out = {"module": module or "learn", "event": event,
               **{k: v for k, v in data.items() if k != "event"}}
        if event == "training_completed":
            return {"type": "task_complete", "data": out}
        if event == "training_failed":
            return {"type": "task_error", "data": out}
        return {"type": "task_progress", "data": out}

    if ptype.startswith("learn_"):  # services.browser_agent_service
        rest = {k: v for k, v in payload.items() if k != "type"}
        rest.setdefault("module", "learn")
        rest["event"] = ptype
        if ptype == "learn_session_end":
            return {"type": "task_complete", "data": rest}
        return {"type": "task_progress", "data": rest}

    if ptype in _FRONTEND_TYPES:
        out_data = data or {k: v for k, v in payload.items() if k != "type"}
        return {"type": ptype, "data": out_data}

    return {"type": "notification",
            "data": {"event": ptype or "message",
                     **{k: v for k, v in payload.items() if k != "type"}}}


class WsHub:
    """WebSocket 连接中枢（单例）。线程安全广播 + 系统遥测订阅推送。"""

    _instance: Optional["WsHub"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._conns: set[WebSocket] = set()
        self._sys_subs: set[WebSocket] = set()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._telemetry_task: Optional[asyncio.Task] = None

    @classmethod
    def instance(cls) -> "WsHub":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── 生命周期 ───────────────────────────────────────────────

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定应用事件循环（lifespan 启动时调用）。"""
        self._loop = loop

    def start_telemetry(self) -> None:
        """启动 2 秒周期的 system_status 遥测推送任务。"""
        if self._loop is None:
            return
        if self._telemetry_task is None or self._telemetry_task.done():
            self._telemetry_task = self._loop.create_task(self._telemetry_loop())

    async def stop_telemetry(self) -> None:
        """停止遥测任务（lifespan 关闭时调用）。"""
        task = self._telemetry_task
        self._telemetry_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ── 连接管理 ───────────────────────────────────────────────

    async def handle_connection(self, ws: WebSocket) -> None:
        """处理一条 /ws 连接的完整生命周期。"""
        await ws.accept()
        with self._lock:
            self._conns.add(ws)
        log.info("WS 客户端已连接（当前 %d 个）", len(self._conns))
        try:
            while True:
                raw = await ws.receive_text()
                await self._handle_client_message(ws, raw)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001 - 连接级异常不扩散
            log.warning("WS 连接异常: %s", exc)
        finally:
            with self._lock:
                self._conns.discard(ws)
                self._sys_subs.discard(ws)
            log.info("WS 客户端已断开（当前 %d 个）", len(self._conns))

    async def _handle_client_message(self, ws: WebSocket, raw: str) -> None:
        """处理客户端消息：ping→pong；subscribe_system 订阅遥测。"""
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        mtype = str(msg.get("type") or "")
        if mtype == "ping":
            await ws.send_json({"type": "pong", "data": {"ts": time.time()}})
        elif mtype == "subscribe_system":
            with self._lock:
                self._sys_subs.add(ws)
        elif mtype == "unsubscribe_system":
            with self._lock:
                self._sys_subs.discard(ws)

    # ── 广播（线程安全，供各服务工作线程调用）───────────────────

    def broadcast(self, payload: dict) -> None:
        """把内部事件翻译为前端协议并广播给全部连接（任意线程可调用）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            msg = _translate(payload)
        except Exception:  # noqa: BLE001 - 翻译异常不阻断业务
            return
        with self._lock:
            targets = list(self._conns)
        if not targets:
            return
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self._send_to(targets, msg)))

    async def _send_to(self, targets: list[WebSocket], msg: dict) -> None:
        """向目标连接发送消息，清理已断开的连接。"""
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        if dead:
            with self._lock:
                for ws in dead:
                    self._conns.discard(ws)
                    self._sys_subs.discard(ws)

    # ── 系统遥测推送 ───────────────────────────────────────────

    async def _telemetry_loop(self) -> None:
        """每 2 秒向 subscribe_system 的连接推送 system_status。"""
        while True:
            await asyncio.sleep(_TELEMETRY_INTERVAL_S)
            with self._lock:
                targets = list(self._sys_subs)
            if not targets:
                continue
            try:
                from ..api.hardware import _realtime_data
                data = await asyncio.to_thread(_realtime_data)
            except Exception:  # noqa: BLE001
                continue
            await self._send_to(targets, {"type": "system_status", "data": data})


def get_ws_hub() -> WsHub:
    """获取 WS 中枢单例。"""
    return WsHub.instance()
