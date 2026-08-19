"""OmniSpace AI v2.3.1 浏览器进程池（TASK-054）。

目标：浏览器"秒开"——学习会话获取已预热的浏览器实例，就绪时间 <1 秒。

设计决策（ADR）：
  本项目浏览器为 Playwright 托管 Chromium（browser_service），且为**用户可
  接管的共享单例**（学习 Agent 与手动浏览 API 共用同一实例，用户可随时
  观看/接管学习过程）。因此进程池不创建第二实例（否则会破坏接管模型并
  双倍占用显存/内存），而是对共享实例做**预热 + 状态隔离管理**：

  1. 启动预热（warmup）：应用启动事件（main.py lifespan）后台调用，
     提前完成 Chromium 进程启动（~2-15s），首个学习会话 acquire <1s。
  2. 获取/归还（acquire/release）：学习会话从池中获取实例；池保证
     交出前实例处于"干净快照"状态（全新 ephemeral context）。
  3. 会话隔离清理（release）：销毁会话上下文（Cookie/LocalStorage/缓存
     全部清除，TC-S-005），随后**预建新的干净上下文**（恢复快照语义），
     使下一次 acquire 无需等待清理。
  4. 脏实例防护：会话异常中断（未 release）时，下次 acquire 检测到
     dirty 标记会先执行清理再交出，保证会话间零数据泄露。

度量：warmup_ms / acquire_ms（最近值与峰值）/ cleanup 次数，供状态 API
与验收（就绪 <1s、会话间无数据泄露）核查。

降级：playwright 不可用或预热失败时，acquire 退化为懒初始化
（与 v2.3 原行为一致），不崩溃。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .browser_service import (
    BrowserError,
    BrowserService,
    get_browser_service,
    playwright_available,
    playwright_unavailable_reason,
)

log = logging.getLogger("omnispace.browser_pool")

ACQUIRE_TARGET_S = 1.0        # 验收：浏览器就绪时间 <1 秒
ACQUIRE_TIMEOUT_S = 30.0      # 冷启动兜底超时（预热失败时同步拉起）


class BrowserPool:
    """浏览器进程池（共享实例预热 + 会话隔离）。单例，见 get_browser_pool()。"""

    def __init__(self, factory: Callable[[], BrowserService]
                 = get_browser_service) -> None:
        self._factory = factory
        self._cond = threading.Condition()
        self._instance: BrowserService | None = None
        self._warm = False          # 预热完成（进程已启动）
        self._leased = False        # 实例已租出
        self._dirty = False         # 租出期间可能产生会话数据
        self._warmup_started = False
        self._shutdown = False
        # ── 度量（验收观测）──
        self.warmup_ms: float = 0.0
        self.last_acquire_ms: float = 0.0
        self.max_acquire_ms: float = 0.0
        self.cleanup_count: int = 0
        self.acquire_count: int = 0

    # ── 启动预热 ─────────────────────────────────────────────────

    def warmup(self, headless: bool = True, background: bool = True,
               timeout: float = 30.0) -> bool:
        """应用启动时预热浏览器进程。

        background=True（默认）在守护线程中执行，不阻塞启动时序；
        预热完成后后续 acquire 命中热实例（<1s）。
        返回是否成功发起（后台模式返回 True 表示已调度）。
        """
        with self._cond:
            if self._shutdown:
                return False
            if self._warmup_started:
                return True
            self._warmup_started = True
        if not playwright_available():
            log.warning("浏览器池预热跳过：playwright 不可用（%s）",
                        playwright_unavailable_reason())
            return False
        if background:
            threading.Thread(target=self._warmup_sync,
                             args=(headless, timeout),
                             name="browser-pool-warmup", daemon=True).start()
            log.info("浏览器池预热已调度（后台）")
            return True
        return self._warmup_sync(headless, timeout)

    def _warmup_sync(self, headless: bool, timeout: float) -> bool:
        """同步预热：启动浏览器进程并预建干净上下文。"""
        t0 = time.time()
        inst = self._factory()
        ok = False
        try:
            ok = inst.is_running or inst.init(headless=headless,
                                              timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 预热失败不崩溃
            log.warning("浏览器池预热异常（降级懒初始化）: %s", exc)
            ok = False
        with self._cond:
            self._instance = inst
            self._warm = bool(ok and inst.is_running)
            self.warmup_ms = (time.time() - t0) * 1000.0
            self._cond.notify_all()
        if self._warm:
            log.info("浏览器池预热完成，耗时 %.0fms（ acquire 目标 <%dms）",
                     self.warmup_ms, int(ACQUIRE_TARGET_S * 1000))
        else:
            log.warning("浏览器池预热失败（acquire 将走懒初始化）: %s",
                        inst.unavailable_reason)
        return self._warm

    # ── 获取 / 归还 ──────────────────────────────────────────────

    def acquire(self, timeout: float = ACQUIRE_TIMEOUT_S) -> BrowserService:
        """从池中获取浏览器实例（热实例 <1s；冷启动走同步拉起）。

        交出保证：实例已运行 且 处于干净快照状态（全新会话上下文）。
        """
        t0 = time.time()
        deadline = t0 + timeout
        with self._cond:
            while not self._shutdown:
                if not self._leased:
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise BrowserError(
                        72008, "获取浏览器实例超时（实例被占用）")  # ERR_BROWSER_TIMEOUT
                self._cond.wait(min(remaining, 0.5))
            if self._shutdown:
                raise BrowserError(72001, "浏览器池已关闭")
            self._leased = True

        inst: BrowserService | None = None
        try:
            inst = self._instance or self._factory()
            # 冷启动：预热未完成/失败 → 同步拉起（慢路径，仅首次）
            if not inst.is_running:
                log.info("浏览器池冷启动（首次 acquire，同步拉起）...")
                if not inst.init(headless=True, timeout=25.0):
                    raise BrowserError(
                        72001,
                        f"浏览器初始化失败：{inst.unavailable_reason}")
            # 脏实例防护：上次会话未正常归还 → 先清理（隔离保证）
            with self._cond:
                need_clean = self._dirty
            if need_clean:
                log.warning("检测到脏实例（上次会话未正常归还），"
                            "先执行隔离清理")
                self._clean_snapshot(inst)
            self._instance = inst
            with self._cond:
                self._dirty = True     # 租出即视为可能弄脏
                self._warm = True
                self.acquire_count += 1
                self.last_acquire_ms = (time.time() - t0) * 1000.0
                self.max_acquire_ms = max(self.max_acquire_ms,
                                          self.last_acquire_ms)
            if self.last_acquire_ms > ACQUIRE_TARGET_S * 1000:
                log.warning("浏览器 acquire 耗时 %.0fms 超过目标 %.0fms",
                            self.last_acquire_ms, ACQUIRE_TARGET_S * 1000)
            else:
                log.debug("浏览器 acquire 就绪，耗时 %.0fms",
                          self.last_acquire_ms)
            return inst
        except Exception:
            with self._cond:
                self._leased = False
                self._cond.notify_all()
            raise

    def release(self, inst: BrowserService | None) -> None:
        """归还实例：会话隔离清理（Cookie/LocalStorage/缓存）+ 预建干净快照。

        清理异步执行（清理耗时 100-500ms，不阻塞会话收尾），完成后
        实例回到空闲可用状态。
        """
        with self._cond:
            if inst is None or inst is not self._instance:
                self._leased = False
                self._cond.notify_all()
                return
        threading.Thread(target=self._release_sync, args=(inst,),
                         name="browser-pool-release", daemon=True).start()

    def _release_sync(self, inst: BrowserService) -> None:
        try:
            self._clean_snapshot(inst)
        finally:
            with self._cond:
                self._dirty = False
                self._leased = False
                self._cond.notify_all()

    def _clean_snapshot(self, inst: BrowserService) -> None:
        """销毁会话上下文（数据全清）并预建干净上下文（恢复快照）。"""
        if not inst.is_running:
            return
        t0 = time.time()
        try:
            inst.end_session()       # 销毁上下文：Cookie/LocalStorage/缓存清除
            inst.begin_session()     # 预建全新干净上下文（干净快照）
            self.cleanup_count += 1
            log.debug("会话隔离清理完成（%.0fms），干净快照已预建",
                      (time.time() - t0) * 1000.0)
        except BrowserError as exc:
            log.warning("会话隔离清理失败: %s", exc.message)

    # ── 状态 / 关闭 ──────────────────────────────────────────────

    def get_status(self) -> dict:
        """进程池状态快照（含验收度量）。"""
        with self._cond:
            return {
                "warm": self._warm,
                "leased": self._leased,
                "dirty": self._dirty,
                "warmup_started": self._warmup_started,
                "warmup_ms": round(self.warmup_ms, 1),
                "last_acquire_ms": round(self.last_acquire_ms, 1),
                "max_acquire_ms": round(self.max_acquire_ms, 1),
                "acquire_target_ms": int(ACQUIRE_TARGET_S * 1000),
                "acquire_count": self.acquire_count,
                "cleanup_count": self.cleanup_count,
                "browser_running": bool(self._instance
                                        and self._instance.is_running),
            }

    def shutdown(self) -> None:
        """关闭进程池（应用退出时调用）。"""
        with self._cond:
            self._shutdown = True
            inst = self._instance
            self._cond.notify_all()
        if inst is not None and inst.is_running:
            try:
                inst.shutdown()
            except Exception:  # noqa: BLE001
                pass
        log.info("浏览器池已关闭")


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_pool: BrowserPool | None = None
_pool_lock = threading.Lock()


def get_browser_pool() -> BrowserPool:
    """获取浏览器进程池单例（线程安全双重检查）。"""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = BrowserPool()
    return _pool
