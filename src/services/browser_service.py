"""OmniSpace AI v2.3 内置浏览器服务（TASK-031）。

ADR 决策：使用 Playwright 托管 Chromium 替代 CEF（cefpython3 内核过旧）。

架构：
  - Playwright sync_api 运行在专属守护线程（_BrowserWorker）中，
    所有 Playwright 对象只在该线程内创建与使用（greenlet 线程亲和）。
  - 对外暴露线程安全的同步方法：每个操作封装为闭包投递到工作线程的
    命令队列，通过 concurrent.futures.Future 阻塞取回结果。
  - playwright 包或 chromium 浏览器缺失时优雅降级：init() 返回 False
    并记录原因，所有页面操作抛出 BrowserUnavailableError（由 API 层
    转为 72xxx 友好错误），进程不崩溃。

安全层（对应测试 TC-S-001~010）：
  - 仅允许 HTTP/HTTPS 协议导航（TC-S-003）
  - 所有操作类方法强制 ≥1s 间隔（内部节流，TC-S-004）
  - Cookie 隔离：每次会话新建 ephemeral context，结束即销毁（TC-S-005）
  - 下载拦截：任何 download 事件直接 cancel 并日志（TC-S-002）
  - 域名黑名单检查（TC-S-010，存 SQLite learning_settings 表）
  - 弹窗自动关闭 / 同源重定向>3 次停止 / WebGL 禁用（TC-S-006）
  - 输入仅限搜索框（TC-S-001 辅助约束）
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import logging
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import DATA_DIR

log = logging.getLogger("omnispace.browser")

# ═══════════════════════════════════════════════════════════════════
#  Playwright 可用性探测（缺失时全模块优雅降级）
# ═══════════════════════════════════════════════════════════════════

try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
    _PLAYWRIGHT_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - 降级而非崩溃
    _sync_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_AVAILABLE = False
    _PLAYWRIGHT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def playwright_available() -> bool:
    """playwright 运行时可导入（读取模块级标志，便于测试替换）。"""
    return bool(_PLAYWRIGHT_AVAILABLE) and _sync_playwright is not None


# ── 环境级失败熔断（2026-08-20 资源爆满事故修复）───────────────────
# Chromium 二进制缺失（playwright install 未执行）属环境级错误，重试
# 永远失败且每次都要 spawn playwright driver 子进程（历史上被
# learning_scheduler 每 30s 循环触发，4 天 3.7 万次失败刷屏 ~50MB
# 日志并侵蚀内存）。首次探测到该错误后置位模块级标志，后续 init()
# 直接快速失败，不再 spawn 进程。
_ENV_BROKEN_MARKERS = (
    "Executable doesn't exist",   # 浏览器二进制缺失
    "playwright install",         # 官方 banner 提示
    "Looks like Playwright",      # 官方 banner 前缀
)
_env_chromium_missing = False


def chromium_env_broken() -> bool:
    """Chromium 二进制是否缺失（环境级熔断标志，进程内持久）。"""
    return _env_chromium_missing


def _mark_env_broken_if_match(error_text: str) -> None:
    """init 失败原因命中环境级标记时置位熔断（模块级，跨实例）。"""
    global _env_chromium_missing
    if not error_text:
        return
    if any(m in error_text for m in _ENV_BROKEN_MARKERS):
        _env_chromium_missing = True
        log.warning(
            "检测到 Chromium 浏览器未安装（环境级错误，本进程内不再"
            "尝试启动浏览器；请执行 `playwright install chromium` 后"
            "重启后端恢复）")


def playwright_unavailable_reason() -> str:
    """返回 playwright 不可用的原因描述。"""
    if playwright_available():
        return ""
    return _PLAYWRIGHT_IMPORT_ERROR or "playwright 包未安装"


# ═══════════════════════════════════════════════════════════════════
#  错误码（7xxxx 段：72001-72008，浏览器域）
# ═══════════════════════════════════════════════════════════════════

ERR_BROWSER_UNAVAILABLE = 72001   # playwright 未安装/初始化失败
ERR_BROWSER_NOT_RUNNING = 72002   # 浏览器未运行
ERR_TAB_LIMIT = 72003             # 标签页数量已达上限
ERR_INVALID_URL = 72004           # 非 HTTP/HTTPS 协议
ERR_DOMAIN_BLACKLISTED = 72005    # 域名在黑名单中
ERR_PAGE_OPERATION = 72006        # 页面操作失败
ERR_USER_TAKEOVER = 72007         # 用户已接管浏览器
ERR_BROWSER_TIMEOUT = 72008       # 浏览器操作超时


class BrowserError(Exception):
    """浏览器服务基础异常，携带统一错误码。"""

    def __init__(self, code: int, message: str, detail: Any = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail if detail is not None else {}
        super().__init__(message)


class BrowserUnavailableError(BrowserError):
    """浏览器不可用/未运行。"""


class TabLimitError(BrowserError):
    """标签页数量已达上限。"""


class InvalidUrlError(BrowserError):
    """URL 协议不在白名单（仅 http/https）。"""


class BlacklistedDomainError(BrowserError):
    """目标域名在黑名单中。"""


class PageOperationError(BrowserError):
    """页面操作失败或被安全策略拒绝。"""


# ═══════════════════════════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════════════════════════

MAX_TABS = 5                      # 标签页绝对上限（规格：最多5个）
MIN_OP_INTERVAL = 1.0             # 操作最小间隔（秒，TC-S-004）
REDIRECT_LIMIT = 3                # 同源重定向/同地址重复导航上限（TC-S-006）
ALLOWED_SCHEMES = ("http", "https")
DEFAULT_NAV_TIMEOUT_MS = 20000
DEFAULT_OP_TIMEOUT_MS = 15000
MAX_TEXT_LENGTH = 20000           # get_text 截断
MAX_DOM_LENGTH = 200000           # get_dom 截断
MAX_LINKS = 50                    # get_links 上限
EASYLIST_PATH = DATA_DIR / "easylist.txt"

# ── 标签页上限硬件等级自适应（文档B §4.2 learn_tabs 配额，审计 BK-011）──
# RTX 5090/4090→5，4070Ti→3，3060→2，RX6600/纯CPU→1；探测失败保守取 MAX_TABS。
_max_tabs_cache: int | None = None


def _probe_gpu_brief() -> tuple[str, int]:
    """轻量探测 GPU 型号名与显存总量 MB（pynvml 优先，torch 兜底）。"""
    try:
        import pynvml
        pynvml.nvmlInit()
        try:
            name = pynvml.nvmlDeviceGetName(0)
            mem = pynvml.nvmlDeviceGetMemoryInfo(0)
        finally:
            pynvml.nvmlShutdown()
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        return str(name), int(mem.total // (1024 * 1024))
    except Exception:  # noqa: BLE001
        pass
    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            return str(props.name), int(props.total_memory // (1024 * 1024))
    except Exception:  # noqa: BLE001
        pass
    return "", 0


def get_max_tabs() -> int:
    """当前硬件等级允许的标签页上限（1~MAX_TABS，进程内缓存一次探测）。"""
    global _max_tabs_cache
    if _max_tabs_cache is not None:
        return _max_tabs_cache
    limit = MAX_TABS
    try:
        from ..data.models import detect_hardware_tier
        gpu_name, vram_mb = _probe_gpu_brief()
        limit = max(1, min(MAX_TABS, int(
            detect_hardware_tier(gpu_name, vram_mb).get("learn_tabs", MAX_TABS))))
    except Exception:  # noqa: BLE001 - 探测失败保守取默认上限
        limit = MAX_TABS
    _max_tabs_cache = limit
    return limit

# 搜索框判定（type_text 仅允许这些控件，TC-S-001 辅助）
_SEARCH_BOX_HINT = re.compile(r"(search|query|keyword|^q$|wd|搜索|查询)", re.I)
_FORBIDDEN_INPUT_TYPES = {"password", "email", "hidden", "tel", "number",
                          "checkbox", "radio", "file", "submit"}

# ── 登录墙/验证码/付费墙启发规则（选择器+文本，供纯函数判定）─────────
_LOGIN_TEXT_HINTS = (
    "请先登录", "登录后查看", "登录后继续", "登录以继续", "需要登录",
    "扫码登录", "登录/注册", "注册后查看", "登录后可阅读", "请登录",
    "sign in to continue", "log in to continue", "please log in",
    "please sign in", "login required", "sign in to read",
)
_LOGIN_KEYWORDS = ("登录", "登入", "登陆", "sign in", "signin", "log in",
                   "login", "密码", "password")
_LOGIN_URL_HINTS = ("/login", "/signin", "/auth", "/passport", "/account/login")

_CAPTCHA_HINTS = (
    "captcha", "recaptcha", "g-recaptcha", "hcaptcha", "h-captcha",
    "geetest", "人机验证", "安全验证", "滑块验证", "滑动验证", "点选验证",
    "验证您不是机器人", "验证你不是机器人", "请完成安全验证", "行为验证",
    "i'm not a robot", "i am not a robot", "checking your browser",
    "cf-challenge", "cf-chl", "challenge-platform", "please verify",
    "verify you are human", "are you a robot",
)

_PAYWALL_HINTS = (
    "开通会员", "付费阅读", "付费查看", "开通vip", "vip专享", "会员专享",
    "会员免费读", "订阅后阅读", "支付后查看", "本文仅对会员开放", "购买专栏",
    "解锁全文", "付费解锁", "升级会员", "此内容为付费内容", "内容需付费",
    "试读结束", "剩余内容需付费", "成为会员后可读",
    "subscribe to read", "subscription required", "paywall",
    "premium content", "this content is for subscribers", "unlock full article",
)


# ═══════════════════════════════════════════════════════════════════
#  纯函数（不依赖浏览器实例，可独立单测）
# ═══════════════════════════════════════════════════════════════════

def is_allowed_url(url: str) -> bool:
    """协议白名单检查：仅允许 http/https（TC-S-003）。

    file:///、ftp://、javascript:、data: 等一律拒绝。
    """
    if not url or not isinstance(url, str):
        return False
    scheme = urlparse(url.strip()).scheme.lower()
    return scheme in ALLOWED_SCHEMES


def extract_domain(url: str) -> str:
    """提取 URL 的主机名（小写，不含端口）。"""
    try:
        host = urlparse(url).netloc.lower()
        return host.split(":", 1)[0]
    except Exception:  # noqa: BLE001
        return ""


def domain_matches(host: str, pattern: str) -> bool:
    """域名匹配：精确或子域名后缀匹配。"""
    host = (host or "").lower().strip()
    pattern = (pattern or "").lower().strip()
    if not host or not pattern:
        return False
    return host == pattern or host.endswith("." + pattern)


def is_blacklisted_url(url: str, blacklist: list[str]) -> bool:
    """检查 URL 域名是否命中黑名单（TC-S-010）。"""
    host = extract_domain(url)
    return any(domain_matches(host, pat) for pat in (blacklist or []))


def detect_login_wall_html(html: str, url: str = "") -> bool:
    """登录墙检测（选择器+文本启发规则，TC-S-001）。

    规则：
      1. 页面含 password 输入框 且 含登录关键词 → 登录墙
      2. 页面含"请先登录/登录后查看/sign in to continue"等文本 → 登录墙
      3. URL 命中登录路径且含 password 输入框 → 登录墙
    """
    if not html:
        return False
    h = html.lower()
    has_password = ('type="password"' in h or "type='password'" in h
                    or "type=password" in h)
    if has_password and any(k in h for k in _LOGIN_KEYWORDS):
        return True
    if any(t in h for t in _LOGIN_TEXT_HINTS):
        return True
    if has_password and url and any(p in url.lower() for p in _LOGIN_URL_HINTS):
        return True
    return False


def detect_captcha_html(html: str) -> bool:
    """验证码检测（TC-S-007）：captcha/recaptcha/人机验证/滑块等。"""
    if not html:
        return False
    h = html.lower()
    return any(t in h for t in _CAPTCHA_HINTS)


def detect_paywall_html(html: str) -> bool:
    """付费墙检测（TC-S-008）：开通会员/付费阅读/subscribe to read 等。"""
    if not html:
        return False
    h = html.lower()
    return any(t in h for t in _PAYWALL_HINTS)


def load_ad_rules(path: Path | str = EASYLIST_PATH,
                  max_domains: int = 5000,
                  max_substrings: int = 2000) -> tuple[list[str], list[str]]:
    """加载 EasyList 本地规则，返回 (域名规则, 子串规则)。

    文件不存在时返回空规则（优雅降级）。仅解析：
      - ||domain^        → 域名规则
      - 纯文本子串行      → 子串规则（长度≥6）
      - ! 注释 / @@ 白名单 / 含正则语法的复杂行 → 跳过
    """
    domains: list[str] = []
    substrings: list[str] = []
    p = Path(path)
    if not p.exists():
        log.info("EasyList 规则文件不存在，广告过滤停用: %s", p)
        return domains, substrings
    try:
        for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("!") or line.startswith("["):
                continue
            if line.startswith("@@"):  # 白名单规则不用于拦截
                continue
            if line.startswith("||"):
                token = line[2:].split("^", 1)[0].split("/", 1)[0].strip().lower()
                if token and "*" not in token and len(domains) < max_domains:
                    domains.append(token)
            elif "|" not in line and "$" not in line:
                token = re.sub(r"[\^*]", "", line).strip().lower()
                if len(token) >= 6 and len(substrings) < max_substrings:
                    substrings.append(token)
            if len(domains) >= max_domains and len(substrings) >= max_substrings:
                break
        log.info("EasyList 加载完成: %d 域名规则 + %d 子串规则",
                 len(domains), len(substrings))
    except Exception as exc:  # noqa: BLE001
        log.warning("EasyList 规则解析失败（忽略，继续无过滤运行）: %s", exc)
    return domains, substrings


def matches_ad_rule(url: str, domains: list[str], substrings: list[str]) -> bool:
    """URL 是否命中广告规则（域名后缀匹配 + URL 子串匹配）。"""
    if not domains and not substrings:
        return False
    host = extract_domain(url)
    for d in domains:
        if domain_matches(host, d):
            return True
    u = url.lower()
    for s in substrings:
        if s in u:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════
#  Playwright 工作线程（所有 Playwright 对象仅在此线程使用）
# ═══════════════════════════════════════════════════════════════════

class _BrowserWorker(threading.Thread):
    """Playwright 专属守护线程：命令队列 + Future 结果回传。"""

    def __init__(self, headless: bool = True) -> None:
        super().__init__(name="OmniSpaceBrowser", daemon=True)
        self.cmd_queue: queue.Queue = queue.Queue()
        self.ready = threading.Event()
        self.init_ok = False
        self.init_error = ""
        self.headless = headless
        # ── 以下对象仅在工作线程内访问 ──
        self.pw: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.pages: dict[str, Any] = {}
        self.current_tab: str | None = None
        self.creating_page = False
        self.ad_domains: list[str] = []
        self.ad_substrings: list[str] = []
        self.ad_filter_enabled = True
        self.blacklist: list[str] = []
        self.recent_navs: list[str] = []   # 最近导航 URL（同地址循环停止用）
        self.traffic_bytes = 0

    # ── 线程主流程 ────────────────────────────────────────────────

    def run(self) -> None:
        try:
            assert _sync_playwright is not None
            self.pw = _sync_playwright().start()
            self.browser = self.pw.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-gpu",               # 爬虫无需 GPU 合成（2026-08-23
                                                   # 显存锚定事故：满载卡上 Chromium
                                                   # GPU 进程挤占 WDDM 预算）
                    "--disable-webgl",            # TC-S-006：禁 WebGL 防挖矿
                    "--disable-3d-apis",
                    "--mute-audio",
                    "--no-first-run",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-sync",
                    "--disable-features=Translate,OptimizationHints",
                    "--metrics-recording-only",
                ],
            )
            self.init_ok = True
        except Exception as exc:  # noqa: BLE001 - 降级而非崩溃
            self.init_error = f"{type(exc).__name__}: {exc}"
            # 单行截断记录：官方 banner 含 30+ 行边框文本，完整输出
            # 曾 4 天刷屏 3.7 万条（~50MB）；完整原因已在父层
            # _mark_env_broken_if_match 标记。
            log.warning("Chromium 启动失败: %s",
                        self.init_error.splitlines()[0][:160])
            self._cleanup()
        finally:
            self.ready.set()
        if not self.init_ok:
            return
        # 命令循环
        while True:
            item = self.cmd_queue.get()
            if item is None:  # 停止哨兵
                break
            fn, fut = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(self))
            except Exception as exc:  # noqa: BLE001 - 结果经 Future 回传
                fut.set_exception(exc)
        self._cleanup()

    def _cleanup(self) -> None:
        for obj_name in ("context", "browser"):
            obj = getattr(self, obj_name, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, obj_name, None)
        if self.pw is not None:
            try:
                self.pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self.pw = None
        self.pages.clear()
        self.current_tab = None

    # ── 事件处理（均在工作线程内触发）─────────────────────────────

    def on_route(self, route: Any, request: Any) -> None:
        """拦截层：协议白名单 / 重定向上限 / 黑名单 / 广告过滤。"""
        try:
            url = request.url
            if request.is_navigation_request():
                scheme = urlparse(url).scheme.lower()
                if scheme not in ALLOWED_SCHEMES and url != "about:blank":
                    log.warning("拦截非HTTP协议导航: %s", url[:120])
                    route.abort()
                    return
                # 同源重定向链 >3 次停止（TC-S-006）
                depth = 0
                req = request
                while getattr(req, "redirected_from", None) is not None:
                    depth += 1
                    req = req.redirected_from
                    if depth > REDIRECT_LIMIT:
                        break
                if depth > REDIRECT_LIMIT:
                    log.warning("重定向超过%d次，停止: %s", REDIRECT_LIMIT, url[:120])
                    route.abort()
                    return
                if self.blacklist and is_blacklisted_url(url, self.blacklist):
                    log.info("域名在黑名单中，跳过: %s", url[:120])
                    route.abort()
                    return
            if self.ad_filter_enabled and matches_ad_rule(
                    url, self.ad_domains, self.ad_substrings):
                route.abort()
                return
            route.continue_()
        except Exception:  # noqa: BLE001 - 拦截器异常不得阻断页面
            try:
                route.continue_()
            except Exception:  # noqa: BLE001
                pass

    def on_context_page(self, page: Any) -> None:
        """window.open 弹窗自动关闭（自己 new_tab 创建的页除外）。"""
        if self.creating_page:
            return  # 受控创建，由 _w_new_tab 注册
        try:
            log.info("检测到弹窗页面，自动关闭: %s", page.url[:120])
            page.close()
        except Exception:  # noqa: BLE001
            pass

    def on_popup(self, page: Any) -> None:
        """源页面 window.open 触发的 popup 事件 → 关闭。"""
        try:
            log.info("弹窗已自动关闭: %s", (page.url or "")[:120])
            page.close()
        except Exception:  # noqa: BLE001
            pass

    def on_download(self, download: Any) -> None:
        """下载拦截：任何下载直接取消（TC-S-002）。"""
        try:
            log.warning("下载被拦截: %s", download.suggested_filename)
            download.cancel()
        except Exception:  # noqa: BLE001
            pass

    def on_dialog(self, dialog: Any) -> None:
        """JS alert/confirm 自动关闭，避免阻塞。"""
        try:
            dialog.dismiss()
        except Exception:  # noqa: BLE001
            pass

    def on_response(self, response: Any) -> None:
        """流量统计：按 content-length 头累计（估算值，供配额控制）。"""
        try:
            headers = response.headers or {}
            length = headers.get("content-length")
            if length and length.isdigit():
                self.traffic_bytes += int(length)
        except Exception:  # noqa: BLE001
            pass


# ═══════════════════════════════════════════════════════════════════
#  工作线程内执行的操作函数（fn(worker) 形式）
# ═══════════════════════════════════════════════════════════════════

def _w_ensure_context(w: _BrowserWorker) -> None:
    """确保存在浏览器上下文；每次会话新建 ephemeral context（TC-S-005）。"""
    if w.context is not None:
        return
    _w_new_context(w)


def _w_new_context(w: _BrowserWorker) -> str:
    """销毁旧上下文并新建 ephemeral context（Cookie/存储完全隔离）。"""
    if w.context is not None:
        try:
            w.context.close()
        except Exception:  # noqa: BLE001
            pass
        w.context = None
        w.pages.clear()
        w.current_tab = None
    ctx = w.browser.new_context(
        accept_downloads=True,   # 允许事件触发，handler 中立即 cancel
        viewport={"width": 1280, "height": 800},
        java_script_enabled=True,
        bypass_csp=False,
    )
    ctx.set_default_timeout(DEFAULT_OP_TIMEOUT_MS)
    ctx.set_default_navigation_timeout(DEFAULT_NAV_TIMEOUT_MS)
    ctx.route("**/*", w.on_route)
    ctx.on("page", w.on_context_page)
    w.context = ctx
    log.info("已创建新的隔离浏览器上下文（ephemeral）")
    return "ok"


def _w_close_context(w: _BrowserWorker) -> str:
    """关闭上下文：Cookie/LocalStorage/缓存随之销毁（TC-S-005）。"""
    if w.context is not None:
        try:
            w.context.close()
        except Exception:  # noqa: BLE001
            pass
        w.context = None
    w.pages.clear()
    w.current_tab = None
    log.info("浏览器上下文已销毁，Cookie/LocalStorage/缓存已清除")
    return "ok"


def _w_current_page(w: _BrowserWorker) -> Any:
    if not w.current_tab or w.current_tab not in w.pages:
        raise PageOperationError(ERR_PAGE_OPERATION,
                                 "没有活动标签页，请先新建标签页或导航")
    return w.pages[w.current_tab]


def _record_nav(w: _BrowserWorker, url: str) -> None:
    """同地址连续导航 >3 次停止（配合路由层重定向链限制）。"""
    w.recent_navs.append(url)
    del w.recent_navs[:-8]
    tail = w.recent_navs[-(REDIRECT_LIMIT + 1):]
    if len(tail) == REDIRECT_LIMIT + 1 and len(set(tail)) == 1:
        w.recent_navs.clear()
        raise PageOperationError(
            ERR_PAGE_OPERATION,
            f"同一地址连续导航超过{REDIRECT_LIMIT}次，判定为重定向循环，已停止")


def _w_goto(w: _BrowserWorker, page: Any, url: str) -> dict:
    _record_nav(w, url)
    resp = page.goto(url, wait_until="domcontentloaded")
    status = 0
    try:
        status = resp.status if resp else 0
    except Exception:  # noqa: BLE001
        pass
    return {"url": page.url, "title": _safe_title(page), "http_status": status}


def _safe_title(page: Any) -> str:
    try:
        return page.title() or ""
    except Exception:  # noqa: BLE001
        return ""


def _safe_url(page: Any) -> str:
    try:
        return page.url or ""
    except Exception:  # noqa: BLE001
        return ""


def _w_new_tab(w: _BrowserWorker, url: str | None = None) -> dict:
    _w_ensure_context(w)
    limit = get_max_tabs()
    if len(w.pages) >= limit:
        raise TabLimitError(ERR_TAB_LIMIT,
                            f"标签页数量已达上限（{limit}个）")
    w.creating_page = True
    try:
        page = w.context.new_page()
    finally:
        w.creating_page = False
    tab_id = uuid.uuid4().hex[:12]
    w.pages[tab_id] = page
    page.on("popup", w.on_popup)
    page.on("dialog", w.on_dialog)
    page.on("download", w.on_download)
    page.on("response", w.on_response)
    w.current_tab = tab_id
    info: dict[str, Any] = {"tab_id": tab_id, "url": "", "title": ""}
    if url:
        info.update(_w_goto(w, page, url))
    return info


def _w_close_tab(w: _BrowserWorker, tab_id: str) -> bool:
    page = w.pages.pop(tab_id, None)
    if page is None:
        return False
    try:
        page.close()
    except Exception:  # noqa: BLE001
        pass
    if w.current_tab == tab_id:
        w.current_tab = next(iter(w.pages), None)
    return True


def _w_list_tabs(w: _BrowserWorker) -> list[dict]:
    return [{
        "tab_id": tid,
        "url": _safe_url(p),
        "title": _safe_title(p),
        "active": tid == w.current_tab,
    } for tid, p in w.pages.items()]


_FORBIDDEN_CLICK_JS = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  const tag = (el.tagName || '').toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  if (tag === 'input' && type === 'password') return true;
  const form = el.closest('form');
  if (form && form.querySelector('input[type="password"]')) {
    if (tag === 'button' || (tag === 'input' &&
        ['submit', 'button', 'image'].includes(type))) return true;
  }
  return false;
}"""

_SEARCH_BOX_JS = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  const tag = (el.tagName || '').toLowerCase();
  if (tag !== 'input' && tag !== 'textarea') return false;
  const type = (el.getAttribute('type') || 'text').toLowerCase();
  if (['password','email','hidden','tel','number','checkbox','radio',
       'file','submit'].includes(type)) return false;
  const form = el.closest('form');
  if (form && form.querySelector('input[type="password"]')) return false;
  const s = [el.getAttribute('name'), el.id, el.getAttribute('placeholder'),
             el.getAttribute('aria-label'), el.getAttribute('class')]
            .filter(Boolean).join(' ').toLowerCase();
  if (type === 'search') return true;
  return /(search|query|keyword|^q$|wd|搜索|查询)/.test(s);
}"""


def _w_click(w: _BrowserWorker, selector: str | None,
             coordinates: tuple | None) -> str:
    page = _w_current_page(w)
    if selector:
        try:
            if page.evaluate(_FORBIDDEN_CLICK_JS, selector):
                raise PageOperationError(
                    ERR_PAGE_OPERATION,
                    "安全策略：禁止点击登录/密码表单相关控件（TC-S-001）")
        except PageOperationError:
            raise
        except Exception:  # noqa: BLE001 - 检测失败不阻断普通点击
            pass
        page.click(selector)
        return f"clicked:{selector}"
    if coordinates:
        page.mouse.click(float(coordinates[0]), float(coordinates[1]))
        return f"clicked:({coordinates[0]},{coordinates[1]})"
    raise PageOperationError(ERR_PAGE_OPERATION,
                             "click 需要提供 selector 或 coordinates")


def _w_type_text(w: _BrowserWorker, selector: str, text: str) -> str:
    page = _w_current_page(w)
    try:
        is_search = bool(page.evaluate(_SEARCH_BOX_JS, selector))
    except Exception:  # noqa: BLE001
        is_search = False
    if not is_search:
        raise PageOperationError(
            ERR_PAGE_OPERATION,
            "安全策略：仅允许在搜索框中输入文本，禁止填写任何表单（TC-S-001）")
    page.fill(selector, text)
    return f"typed:{selector}"


def _w_scroll(w: _BrowserWorker, direction: str, amount: int) -> str:
    page = _w_current_page(w)
    dx, dy = 0, 0
    if direction == "down":
        dy = amount
    elif direction == "up":
        dy = -amount
    elif direction == "right":
        dx = amount
    elif direction == "left":
        dx = -amount
    else:
        raise PageOperationError(ERR_PAGE_OPERATION,
                                 f"无效的滚动方向: {direction}")
    page.mouse.wheel(dx, dy)
    return f"scrolled:{direction}:{amount}"


def _w_get_text(w: _BrowserWorker) -> str:
    page = _w_current_page(w)
    text = page.evaluate(
        "() => document.body ? document.body.innerText : ''") or ""
    return text[:MAX_TEXT_LENGTH]


def _w_get_dom(w: _BrowserWorker) -> str:
    page = _w_current_page(w)
    html = page.content() or ""
    return html[:MAX_DOM_LENGTH]


def _w_get_links(w: _BrowserWorker) -> list[dict]:
    page = _w_current_page(w)
    links = page.evaluate(
        """() => Array.from(document.querySelectorAll('a[href]'))
             .slice(0, 300)
             .map(a => ({text: (a.innerText || '').trim().slice(0, 80),
                         href: a.href}))
             .filter(x => /^https?:/.test(x.href))""") or []
    return links[:MAX_LINKS]


def _w_dismiss_banner(w: _BrowserWorker) -> int:
    """移除常见 Cookie/订阅横幅（限量删除，避免误伤正文）。"""
    page = _w_current_page(w)
    try:
        return int(page.evaluate(
            """() => {
              const sels = ['[class*="cookie"]', '[id*="cookie"]',
                            '[class*="consent"]', '[id*="consent"]',
                            '[class*="subscribe-banner"]', '[class*="gdpr"]'];
              let n = 0;
              for (const s of sels) {
                document.querySelectorAll(s).forEach(el => {
                  if (n < 10 && el && el.parentNode && el.offsetHeight > 0
                      && el.offsetHeight < window.innerHeight * 0.9) {
                    el.parentNode.removeChild(el); n++;
                  }
                });
              }
              return n;
            }""") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _w_close_popups(w: _BrowserWorker) -> int:
    """关闭除当前标签页外的所有页面（弹窗清理），返回关闭数。"""
    closed = 0
    for tid in list(w.pages.keys()):
        if tid == w.current_tab:
            continue
        page = w.pages.pop(tid, None)
        if page is not None:
            try:
                page.close()
                closed += 1
            except Exception:  # noqa: BLE001
                pass
    return closed


def _w_detect(w: _BrowserWorker, kind: str) -> bool:
    """在当前页面执行墙检测（login/paywall/captcha）。"""
    page = _w_current_page(w)
    try:
        html = page.content() or ""
    except Exception:  # noqa: BLE001
        return False
    url = _safe_url(page)
    if kind == "login":
        return detect_login_wall_html(html, url)
    if kind == "paywall":
        return detect_paywall_html(html)
    if kind == "captcha":
        return detect_captcha_html(html)
    return False


# ═══════════════════════════════════════════════════════════════════
#  BrowserService：线程安全同步门面（单例）
# ═══════════════════════════════════════════════════════════════════

class BrowserService:
    """内置浏览器服务（Playwright 托管 Chromium）。

    所有公开方法线程安全；操作类方法强制 ≥1s 节流（TC-S-004）。
    playwright 缺失或 chromium 启动失败时 init() 返回 False，
    此后所有页面操作抛 BrowserUnavailableError。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._op_lock = threading.Lock()
        self._last_op_ts = 0.0
        self._worker: _BrowserWorker | None = None
        self._running = False
        self._unavailable_reason = ""
        self._headless = True
        self._user_takeover = False
        self._ad_domains: list[str] = []
        self._ad_substrings: list[str] = []
        self._ad_filter_enabled = True
        self._blacklist_cache: list[str] = []
        self._blacklist_loaded_at = 0.0

    # ── 内部：节流 / 提交 / 状态 ──────────────────────────────────

    def _throttle(self) -> None:
        """操作频率限制：任意两次操作间隔 ≥1s（TC-S-004）。"""
        with self._op_lock:
            now = time.monotonic()
            wait = MIN_OP_INTERVAL - (now - self._last_op_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_op_ts = time.monotonic()

    def _ensure_running(self) -> None:
        if (not self._running or self._worker is None
                or not self._worker.init_ok):
            reason = self._unavailable_reason or "init() 尚未成功执行"
            raise BrowserUnavailableError(
                ERR_BROWSER_NOT_RUNNING, f"浏览器服务未运行：{reason}",
                detail={"reason": reason})

    def _submit(self, fn: Callable[[_BrowserWorker], Any],
                timeout: float = 30.0) -> Any:
        """投递操作到浏览器工作线程并阻塞等待结果。"""
        self._ensure_running()
        assert self._worker is not None
        fut: Future = Future()
        self._worker.cmd_queue.put((fn, fut))
        try:
            return fut.result(timeout)
        except FutureTimeoutError:
            raise BrowserError(ERR_BROWSER_TIMEOUT,
                               f"浏览器操作超时（>{timeout}s）") from None
        except BrowserError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转为友好错误
            raise PageOperationError(
                ERR_PAGE_OPERATION, f"页面操作失败：{exc}") from exc

    def _check_url(self, url: str) -> None:
        """导航前安全检查：协议白名单 + 域名黑名单。"""
        if not is_allowed_url(url):
            log.warning("拒绝非HTTP/HTTPS协议导航: %s", (url or "")[:120])
            raise InvalidUrlError(
                ERR_INVALID_URL,
                f"仅允许访问 HTTP/HTTPS 地址，已拒绝: {(url or '')[:80]}")
        blacklist = self._get_blacklist()
        if blacklist and is_blacklisted_url(url, blacklist):
            log.info("域名在黑名单中，跳过: %s", url[:120])
            raise BlacklistedDomainError(
                ERR_DOMAIN_BLACKLISTED,
                f"域名在黑名单中，已阻止访问: {extract_domain(url)}")

    def _get_blacklist(self) -> list[str]:
        """从 SQLite learning_settings 读取域名黑名单（60s 缓存）。"""
        now = time.time()
        if now - self._blacklist_loaded_at < 60:
            return self._blacklist_cache
        result: list[str] = []
        try:
            from ..data.database import get_db_safe, parse_json
            db = get_db_safe()
            if db is not None:
                db.sql(
                    "CREATE TABLE IF NOT EXISTS learning_settings ("
                    "key TEXT PRIMARY KEY, value TEXT DEFAULT '{}', "
                    "updated_at REAL NOT NULL DEFAULT 0)")
                row = db.query_one(
                    "SELECT value FROM learning_settings "
                    "WHERE key='domain_blacklist'")
                if row:
                    val = parse_json(row.get("value"), [])
                    if isinstance(val, list):
                        result = [str(d).strip().lower() for d in val
                                  if str(d).strip()]
        except Exception as exc:  # noqa: BLE001 - 黑名单不可用时放行并日志
            log.warning("读取域名黑名单失败（按空名单处理）: %s", exc)
        self._blacklist_cache = result
        self._blacklist_loaded_at = now
        return result

    def reload_blacklist(self) -> list[str]:
        """设置变更后强制刷新黑名单缓存，并同步到工作线程。"""
        self._blacklist_loaded_at = 0.0
        bl = self._get_blacklist()
        if self._running and self._worker is not None:
            self._worker.blacklist = bl
        return bl

    # ── 生命周期 ──────────────────────────────────────────────────

    def init(self, headless: bool = True, timeout: float = 20.0) -> bool:
        """初始化浏览器（目标 <3s）。失败返回 False 并记录原因。"""
        with self._lock:
            if self._running:
                return True
            # 环境级熔断：Chromium 缺失已探测过 → 快速失败，不 spawn 进程
            if _env_chromium_missing:
                self._unavailable_reason = (
                    "Chromium 浏览器未安装（环境级熔断生效，"
                    "请执行 `playwright install chromium` 并重启后端）")
                log.debug("BrowserService init 跳过（Chromium 缺失熔断）")
                return False
            if not playwright_available():
                self._unavailable_reason = (
                    f"playwright 不可用：{playwright_unavailable_reason()}")
                log.warning("BrowserService 初始化失败（优雅降级）: %s",
                            self._unavailable_reason)
                return False
            t0 = time.time()
            self._headless = headless
            self._ad_domains, self._ad_substrings = load_ad_rules()
            worker = _BrowserWorker(headless=headless)
            worker.ad_domains = self._ad_domains
            worker.ad_substrings = self._ad_substrings
            worker.ad_filter_enabled = self._ad_filter_enabled
            worker.blacklist = self._get_blacklist()
            self._worker = worker
            worker.start()
            if not worker.ready.wait(timeout):
                self._unavailable_reason = f"浏览器启动超时（>{timeout:.0f}s）"
                log.warning("BrowserService 初始化失败: %s",
                            self._unavailable_reason)
                self._worker = None
                return False
            if not worker.init_ok:
                self._unavailable_reason = (worker.init_error
                                            or "chromium 启动失败")
                _mark_env_broken_if_match(worker.init_error or "")
                log.warning("BrowserService 初始化失败（优雅降级）: %s",
                            self._unavailable_reason.splitlines()[0][:160])
                self._worker = None
                return False
            self._running = True
            try:
                self._submit(lambda w: _w_new_context(w), timeout=15)
            except BrowserError as exc:
                self._unavailable_reason = exc.message
                self._running = False
                return False
            elapsed = time.time() - t0
            self._unavailable_reason = ""
            if elapsed > 3.0:
                log.warning("浏览器初始化耗时 %.1fs（目标 <3s）", elapsed)
            else:
                log.info("浏览器初始化完成，耗时 %.1fs", elapsed)
            return True

    def shutdown(self) -> bool:
        """关闭浏览器，销毁上下文并清除全部会话数据（TC-S-005）。"""
        with self._lock:
            worker = self._worker
            self._worker = None
            self._running = False
        if worker is not None:
            try:
                worker.cmd_queue.put(None)
                worker.join(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        log.info("浏览器已关闭，全部 Cookie/缓存数据已清除")
        return True

    def restart(self) -> bool:
        """重启浏览器进程（内存泄漏恢复）。"""
        log.info("浏览器重启中...")
        self.shutdown()
        ok = self.init(headless=self._headless)
        if not ok:
            log.warning("浏览器重启失败: %s", self._unavailable_reason)
        return ok

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    # ── 会话（Cookie 隔离）─────────────────────────────────────────

    def begin_session(self) -> bool:
        """开始新学习会话：销毁旧上下文并新建 ephemeral context。"""
        self._submit(lambda w: _w_new_context(w), timeout=15)
        return True

    def end_session(self) -> bool:
        """结束学习会话：销毁上下文，清除 Cookie/LocalStorage/缓存。"""
        if not self._running:
            return False
        self._submit(lambda w: _w_close_context(w), timeout=15)
        return True

    # ── 标签页管理（上限按硬件等级自适应 1~5 个）─────────────────────

    def new_tab(self, url: str | None = None) -> str:
        """新建标签页，返回 tab_id；超过当前硬件等级上限拒绝。"""
        self._throttle()
        self._check_url(url) if url else None
        info = self._submit(lambda w: _w_new_tab(w, url))
        return info["tab_id"]

    def switch_tab(self, tab_id: str) -> bool:
        self._throttle()

        def _fn(w: _BrowserWorker) -> bool:
            if tab_id not in w.pages:
                raise PageOperationError(ERR_PAGE_OPERATION,
                                         f"标签页不存在: {tab_id}")
            w.current_tab = tab_id
            return True
        return bool(self._submit(_fn))

    def close_tab(self, tab_id: str) -> bool:
        self._throttle()
        return bool(self._submit(lambda w: _w_close_tab(w, tab_id)))

    def list_tabs(self) -> list[dict]:
        if not self._running:
            return []
        return list(self._submit(lambda w: _w_list_tabs(w)))

    # ── 页面操作（供 AI Agent 调用）─────────────────────────────────

    def navigate(self, url: str) -> dict:
        """导航到 URL（仅 http/https；黑名单拒绝；同地址循环>3次停止）。"""
        self._throttle()
        self._check_url(url)

        def _fn(w: _BrowserWorker) -> dict:
            _w_ensure_context(w)
            if not w.pages:
                info = _w_new_tab(w, url)
                return {"url": info.get("url", url),
                        "title": info.get("title", ""),
                        "http_status": info.get("http_status", 0)}
            return _w_goto(w, _w_current_page(w), url)
        return dict(self._submit(_fn, timeout=35))

    def click(self, selector: str | None = None,
              coordinates: tuple | None = None) -> str:
        self._throttle()
        return str(self._submit(lambda w: _w_click(w, selector, coordinates)))

    def type_text(self, selector: str, text: str) -> str:
        """输入文本（仅限搜索框，其余一律拒绝，TC-S-001）。"""
        self._throttle()
        return str(self._submit(lambda w: _w_type_text(w, selector, text)))

    def scroll(self, direction: str = "down", amount: int = 600) -> str:
        self._throttle()
        return str(self._submit(lambda w: _w_scroll(w, direction, amount)))

    def screenshot(self) -> bytes:
        """当前页面截图（PNG 字节）。"""
        self._throttle()
        page_fn = lambda w: _w_current_page(w).screenshot(type="png")  # noqa: E731
        return bytes(self._submit(page_fn, timeout=20))

    def get_dom(self) -> str:
        self._throttle()
        return str(self._submit(_w_get_dom))

    def get_text(self) -> str:
        self._throttle()
        return str(self._submit(_w_get_text))

    def get_links(self) -> list[dict]:
        self._throttle()
        return list(self._submit(_w_get_links))

    def execute_js(self, code: str) -> Any:
        self._throttle()
        return self._submit(lambda w: _w_current_page(w).evaluate(code))

    def go_back(self) -> dict:
        self._throttle()

        def _fn(w: _BrowserWorker) -> dict:
            page = _w_current_page(w)
            try:
                page.go_back(wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001 - 无历史时忽略
                pass
            return {"url": _safe_url(page), "title": _safe_title(page)}
        return dict(self._submit(_fn))

    def go_forward(self) -> dict:
        self._throttle()

        def _fn(w: _BrowserWorker) -> dict:
            page = _w_current_page(w)
            try:
                page.go_forward(wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass
            return {"url": _safe_url(page), "title": _safe_title(page)}
        return dict(self._submit(_fn))

    def wait(self, selector: str | None = None,
             timeout: float = 10.0) -> bool:
        self._throttle()

        def _fn(w: _BrowserWorker) -> bool:
            page = _w_current_page(w)
            if selector:
                page.wait_for_selector(selector, timeout=timeout * 1000)
            else:
                page.wait_for_timeout(int(timeout * 1000))
            return True
        return bool(self._submit(_fn, timeout=timeout + 10))

    # ── 弹窗与墙检测 ────────────────────────────────────────────────

    def close_popup(self) -> int:
        """关闭除当前页外的弹窗页面，返回关闭数量。"""
        self._throttle()
        return int(self._submit(_w_close_popups))

    def dismiss_banner(self) -> int:
        """移除 Cookie/订阅横幅，返回移除元素数。"""
        self._throttle()
        return int(self._submit(_w_dismiss_banner))

    def detect_login_wall(self) -> bool:
        self._throttle()
        return bool(self._submit(lambda w: _w_detect(w, "login")))

    def detect_paywall(self) -> bool:
        self._throttle()
        return bool(self._submit(lambda w: _w_detect(w, "paywall")))

    def detect_captcha(self) -> bool:
        self._throttle()
        return bool(self._submit(lambda w: _w_detect(w, "captcha")))

    # ── 用户接管 ────────────────────────────────────────────────────

    def set_user_takeover(self, flag: bool) -> None:
        with self._lock:
            self._user_takeover = bool(flag)
        log.info("用户接管状态: %s", "已接管（AI暂停）" if flag else "已交还AI")

    def is_user_takeover(self) -> bool:
        with self._lock:
            return self._user_takeover

    def ensure_ai_control(self) -> None:
        """AI 操作前检查：用户接管期间抛错（72007）。"""
        if self.is_user_takeover():
            raise BrowserError(ERR_USER_TAKEOVER,
                               "用户已接管浏览器，AI 控制已暂停")

    # ── 广告过滤开关 ────────────────────────────────────────────────

    def set_ad_filter(self, enabled: bool) -> None:
        self._ad_filter_enabled = bool(enabled)
        if self._running and self._worker is not None:
            self._worker.ad_filter_enabled = bool(enabled)

    # ── 状态与流量 ──────────────────────────────────────────────────

    def get_traffic_stats(self) -> dict:
        """返回自浏览器启动以来的累计流量（按 content-length 估算）。"""
        total = 0
        if self._running and self._worker is not None:
            try:
                total = int(self._submit(lambda w: w.traffic_bytes,
                                         timeout=5))
            except BrowserError:
                total = 0
        return {"total_bytes": total,
                "total_mb": round(total / 1048576, 3)}

    def _memory_usage_mb(self) -> float:
        """统计 playwright 子进程树内存；不可得时按标签数估算。"""
        try:
            import psutil
            me = psutil.Process()
            total = 0
            for child in me.children(recursive=True):
                try:
                    name = (child.name() or "").lower()
                    if any(k in name for k in ("chrome", "chromium", "node")):
                        total += child.memory_info().rss
                except Exception:  # noqa: BLE001
                    continue
            if total > 0:
                return round(total / 1048576, 1)
        except Exception:  # noqa: BLE001
            pass
        tabs = 0
        try:
            if self._running and self._worker is not None:
                tabs = len(self._worker.pages)
        except Exception:  # noqa: BLE001
            pass
        return round(200.0 + tabs * 300.0, 1)  # 估算：单标签 ~300MB

    def get_status(self) -> dict:
        """浏览器状态快照（不节流、不要求运行中）。"""
        tabs_count = 0
        current_url = ""
        if self._running and self._worker is not None:
            try:
                info = self._submit(
                    lambda w: (len(w.pages),
                               _safe_url(w.pages[w.current_tab])
                               if w.current_tab in w.pages else ""),
                    timeout=5)
                tabs_count, current_url = info
            except BrowserError:
                pass
        return {
            "running": self._running,
            "tabs_count": tabs_count,
            "memory_usage_mb": self._memory_usage_mb() if self._running else 0.0,
            "current_url": current_url,
            "user_takeover": self.is_user_takeover(),
            "playwright_available": playwright_available(),
            "unavailable_reason": self._unavailable_reason,
            "ad_filter_enabled": self._ad_filter_enabled,
            "ad_rules_loaded": len(self._ad_domains) + len(self._ad_substrings),
        }


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_service: BrowserService | None = None
_service_lock = threading.Lock()


def get_browser_service() -> BrowserService:
    """获取浏览器服务单例（线程安全双重检查）。"""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = BrowserService()
    return _service
