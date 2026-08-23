"""内置浏览器 API 路由（TASK-031/036/037，v2.3）。

端点清单（由 main.py 以 /v1 前缀挂载）：
- GET  /browser/status         浏览器状态
- GET  /browser/screenshot     当前截图（base64 PNG）
- GET  /browser/current-page   当前页面信息
- GET  /browser/tabs           标签页列表
- POST /browser/navigate       用户手动导航
- POST /browser/takeover       用户接管（暂停AI控制）
- POST /browser/handback       交还AI控制

约定：router 不带 prefix（main.py include 时加 /v1）；
成功 ok(data)；浏览器域错误码 7xxxx 段（72001-72008）经 ApiError 抛出；
playwright 缺失/浏览器未运行时返回友好错误而非崩溃。
"""
from __future__ import annotations

import base64
import logging
import time

from fastapi import APIRouter, Body

from ..middleware.error_handler import ApiError, ok
from ..services.browser_service import (
    ERR_BROWSER_UNAVAILABLE,
    BrowserError,
    get_browser_service,
    playwright_available,
    playwright_unavailable_reason,
)

router = APIRouter()
log = logging.getLogger("omnispace.api.browser")


def _call_browser(fn, *args, **kwargs):
    """执行浏览器操作并把 BrowserError 转为 ApiError（统一错误码）。"""
    try:
        return fn(*args, **kwargs)
    except BrowserError as exc:
        raise ApiError(exc.code, exc.message, detail=exc.detail) from exc
    except Exception as exc:  # noqa: BLE001
        log.warning("浏览器接口异常: %s", exc)
        raise ApiError(ERR_BROWSER_UNAVAILABLE,
                       f"浏览器服务异常：{exc}") from exc


def _ensure_ready():
    """确保浏览器可用：未运行则尝试初始化；失败抛 72001/72002。"""
    svc = get_browser_service()
    if svc.is_running:
        return svc
    if not playwright_available():
        raise ApiError(
            ERR_BROWSER_UNAVAILABLE,
            "浏览器组件不可用：playwright 未安装或未能导入",
            detail={"reason": playwright_unavailable_reason()},
            suggestion="浏览器功能将在组件安装完成后自动可用")
    if not svc.init():
        raise ApiError(
            ERR_BROWSER_UNAVAILABLE,
            "浏览器初始化失败",
            detail={"reason": svc.unavailable_reason},
            suggestion="请稍后重试；chromium 可能仍在后台安装")
    return svc


@router.get("/browser/status")
def browser_status():
    """浏览器状态快照（running/tabs/memory/current_url 等）。"""
    svc = get_browser_service()
    return ok(svc.get_status())


# ── 截图结果缓存 ──────────────────────────────────────────────────────
# 背景：browser_service 所有页面操作强制 ≥1s 节流（TC-S-004）且单工作线程
# 串行执行。前端 2s 轮询截图若无缓存，请求会在节流队列积压 6-7s，
# 挂起连接占满 Chromium 同域 6 连接上限，导致按钮触发的 API 全部排队
# （表现为"所有点击按钮延迟过高"）。
# 方案：TTL 2.5s > 轮询周期 2s，隔次轮询命中缓存直接返回，截图操作量
# 减半且队列不再积压。导航后缓存主动失效（见 browser_navigate）。
_SCREENSHOT_CACHE_TTL = 2.5
_shot_cache: tuple[float, str] | None = None  # (monotonic 时间戳, base64)


@router.get("/browser/screenshot")
def browser_screenshot():
    """当前页面截图，返回 base64 PNG（前端 2s 轮询用；2.5s 结果缓存）。"""
    global _shot_cache
    svc = _ensure_ready()
    if _shot_cache is not None and time.monotonic() - _shot_cache[0] < _SCREENSHOT_CACHE_TTL:
        return ok({"image_base64": _shot_cache[1], "mime": "image/png"})
    png = _call_browser(svc.screenshot)
    _shot_cache = (time.monotonic(), base64.b64encode(png).decode("ascii"))
    return ok({"image_base64": _shot_cache[1], "mime": "image/png"})


@router.get("/browser/current-page")
def browser_current_page():
    """当前页面信息：url/title。

    仅 1 个节流操作（list_tabs）。曾在此处调 get_text + get_links 统计
    文本长度/链接数，但前端无任何消费者，且每调用一次占用 2 个 ≥1s
    节流操作额度，是轮询期操作队列积压的元凶之一，已移除。
    """
    svc = _ensure_ready()
    status = svc.get_status()
    page: dict = {"url": status.get("current_url", ""), "title": ""}
    tabs = _call_browser(svc.list_tabs)
    for t in tabs:
        if t.get("active"):
            page["title"] = t.get("title", "")
            page["url"] = t.get("url", page["url"])
    return ok(page)


@router.get("/browser/tabs")
def browser_tabs():
    """标签页列表（上限按硬件等级自适应 1~5 个，文档B §4.2，审计 BK-011）。"""
    svc = _ensure_ready()
    tabs = _call_browser(svc.list_tabs)
    from ..services.browser_service import get_max_tabs
    return ok({"items": tabs, "total": len(tabs), "max_tabs": get_max_tabs()})


@router.post("/browser/navigate")
def browser_navigate(body: dict = Body(default_factory=dict)):
    """用户手动导航（仅 http/https；黑名单拒绝；≥1s 节流）。"""
    global _shot_cache
    url = str((body or {}).get("url", "") or "").strip()
    if not url:
        raise ApiError(40008, "缺少必填参数: url")
    svc = _ensure_ready()
    info = _call_browser(svc.navigate, url)
    _shot_cache = None  # 页面已变化，截图缓存立即失效
    return ok(info, message="导航完成")


@router.post("/browser/takeover")
def browser_takeover():
    """用户接管浏览器：AI 控制暂停（Agent 循环每步检查）。"""
    svc = get_browser_service()
    svc.set_user_takeover(True)
    log.info("用户已接管浏览器，AI 控制暂停")
    return ok({"user_takeover": True}, message="已接管，AI 控制已暂停")


@router.post("/browser/handback")
def browser_handback():
    """交还浏览器控制权给 AI。"""
    svc = get_browser_service()
    svc.set_user_takeover(False)
    log.info("用户已交还浏览器控制权给 AI")
    return ok({"user_takeover": False}, message="已交还 AI 控制")
