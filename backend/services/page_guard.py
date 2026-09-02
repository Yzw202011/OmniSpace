"""页面全关自动退出（2026-09-03 用户拍板，接入 boot 链）。

背景：浏览器页面只是"遥控器"，关页面不停服是无头后端的设计行为；
会"关了就自己退"的壳 X 键方案停在桌面壳 PoC 未接入。本服务把同一
体验带进 boot 链：**所有前端页面关闭（WS 连接归零）→ 倒计时（默认
60s，kv 可调可关）→ 到点复查无任务无加载 → POST 启动页 /api/quit
正规链全退**（等价于 stop.py，boot 看门狗配合放行，ComfyUI 一并清理）。

安全边界（铁律继承）：
  - 仅当由 boot 链拉起（环境变量 OMNISPACE_SPLASH_PORT 存在）才激活；
    开发直启 / 测试实例无此变量 = 永不触发，绝不裸杀后端；
  - 任一页面重连 → 取消本轮倒计时；
  - 到点时若忙（功能锁持有 / 图像或视频队列非空）→ 放弃本轮并在下
    一个轮询周期重新武装（任务跑完且页面仍未打开 → 届时再退）；
  - 启动页不在（/api/quit 失败）→ 记录错误并停用自身（不重试轰炸，
    不降级为裸杀）。

注意：预热中的模型加载不算"忙"（warmup 非用户工作，中途退出无害）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass

from ..data.database import get_db_safe
from ..services.event_log import log_event

log = logging.getLogger("omnispace.services.page_guard")

SPLASH_PORT_ENV = "OMNISPACE_SPLASH_PORT"
_SETTINGS_KEY = "page_guard"
_POLL_INTERVAL_S = 5.0
DEFAULT_COUNTDOWN_S = 60.0


def _read_config() -> tuple[bool, float]:
    """读 kv 配置 → (enabled, countdown_s)。异常回默认（宁可不退不误杀）。"""
    enabled, countdown = True, DEFAULT_COUNTDOWN_S
    try:
        db = get_db_safe()
        if db is None:
            return enabled, countdown
        row = db.query_one(
            "SELECT value FROM system_settings WHERE key=?", (_SETTINGS_KEY,))
        if row:
            data = json.loads(row["value"])
            if isinstance(data, dict):
                enabled = bool(data.get("enabled", enabled))
                raw_cd = data.get("countdown_s", countdown)
                if isinstance(raw_cd, (int, float)) and raw_cd >= 10:
                    countdown = float(raw_cd)
    except Exception as exc:  # noqa: BLE001 - 配置读失败按默认
        log.debug("page_guard 配置读取失败: %s", exc)
    return enabled, countdown


def _busy() -> bool:
    """忙判定：功能锁持有 / 图像队列非空 / 视频队列非空。"""
    try:
        from ..middleware.feature_lock import get_feature_lock
        if get_feature_lock().active_feature is not None:
            return True
    except Exception as exc:  # noqa: BLE001 - 探测失败按忙处理（保守）
        log.debug("page_guard 功能锁探测失败: %s", exc)
        return True
    try:
        from .image_queue import get_image_queue
        snap = get_image_queue().snapshot()
        if snap.get("current") or snap.get("queued"):
            return True
    except Exception as exc:  # noqa: BLE001
        log.debug("page_guard 图像队列探测失败: %s", exc)
        return True
    try:
        from .video_queue import get_video_queue
        snap = get_video_queue().snapshot()
        if snap.get("current") or snap.get("queued"):
            return True
    except Exception as exc:  # noqa: BLE001
        log.debug("page_guard 视频队列探测失败: %s", exc)
        return True
    return False


@dataclass
class GuardState:
    """守卫状态机（纯数据，便于单测）。

    saw_pages：本进程生命周期内见过至少一个页面连接——只有"开过又
    全关"才是退出信号；从头到尾没人开页面（无头/API-only）永不触发。
    """
    saw_pages: bool = False
    deadline: float | None = None


def step(state: GuardState, *, count: int, busy: bool, enabled: bool,
         countdown_s: float, now: float) -> str:
    """状态机单步。返回动作：none / arm / fire / abort。

    - 页面开着（count>0）：记住见过页面，清倒计时；
    - 未启用：清倒计时（saw_pages 保留，重新启用后无需再开页面）；
    - 全关且启用：未见页面前不武装；武装后到点 → 忙则 abort（不清
      saw_pages，下轮重新武装继续等），闲则 fire。
    """
    if count > 0:
        state.saw_pages = True
        state.deadline = None
        return "none"
    if not enabled:
        state.deadline = None
        return "none"
    if not state.saw_pages:
        return "none"
    if state.deadline is None:
        state.deadline = now + countdown_s
        return "arm"
    if now >= state.deadline:
        state.deadline = None
        return "fire" if not busy else "abort"
    return "none"


class PageGuardWatcher:
    """轮询循环（lifespan 启动/关闭）。非 boot 链实例整体不启动。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._splash_port = os.environ.get(SPLASH_PORT_ENV, "")
        self.disabled_forever = False  # /api/quit 失败后停用（不裸杀）

    @property
    def active(self) -> bool:
        return bool(self._splash_port) and not self.disabled_forever

    async def start(self) -> None:
        if not self._splash_port:
            log.info("页面守卫未激活（非 boot 链启动，无 %s）", SPLASH_PORT_ENV)
            return
        self._task = asyncio.create_task(self._loop())
        log.info("页面守卫已启动：全部页面关闭 %ss 且无任务 → 正规链退出"
                 "（kv page_guard 可调/可关）", DEFAULT_COUNTDOWN_S)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        from .ws_hub import WsHub
        hub = WsHub.instance()
        state = GuardState()
        while True:
            await asyncio.sleep(_POLL_INTERVAL_S)
            try:
                enabled, countdown_s = _read_config()
                if self.disabled_forever or not self._splash_port:
                    return
                count = hub.connection_count()
                busy = _busy() if count == 0 else False
                action = step(state, count=count, busy=busy, enabled=enabled,
                              countdown_s=countdown_s, now=time.time())
                if action == "arm":
                    log.info("页面守卫：全部页面已关闭，%ss 内无页面重连且无任务"
                             "将自动退出", countdown_s)
                elif action == "abort":
                    log.info("页面守卫：倒计时到点但有任务在跑，放弃本轮退出"
                             "（任务结束后自动重试）")
                elif action == "fire":
                    await self._fire()
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 守卫自身异常不扩散
                log.warning("页面守卫轮询异常（继续）: %s", exc)

    async def _fire(self) -> None:
        port = self._splash_port
        log.info("页面守卫：倒计时到点且无任务，经启动页 /api/quit 正规退出")
        try:
            log_event("system", "page_guard_quit",
                      "所有页面已关闭且无进行中任务，应用自动退出"
                      "（可在设置页调整或关闭该行为）", level="info")
        except Exception:  # noqa: BLE001
            pass
        url = f"http://127.0.0.1:{port}/api/quit"

        def _post() -> None:
            req = urllib.request.Request(url, method="POST", data=b"")
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                if resp.status != 200:
                    raise RuntimeError(f"启动页返回 {resp.status}")

        try:
            await asyncio.to_thread(_post)
            log.info("页面守卫：退出请求已受理（boot 链收尾中）")
        except Exception as exc:  # noqa: BLE001 - 启动页不在=无法正规退，停用自身
            self.disabled_forever = True
            log.warning("页面守卫：/api/quit 失败（%s），自动停用——"
                        "不会降级为直接杀进程，请用停止快捷方式退出", exc)


_watcher: PageGuardWatcher | None = None


def get_page_guard() -> PageGuardWatcher:
    global _watcher
    if _watcher is None:
        _watcher = PageGuardWatcher()
    return _watcher
