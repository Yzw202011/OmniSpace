"""OmniSpace AI v2.3.1 资源硬限制守卫（用户裁定 2026-08-22）。

限制目标：
  - 系统运行内存（RAM）占用 ≤ 85%
  - GPU 显存（VRAM）占用 ≤ 90%

策略映射（用户需求原文）：
  - 硬件配置足够（降载后空间充足）→ 模型装载闸门放行 fp16 全速
    直载 = 保证质量、缩短生成时间；
  - 硬件配置不足 → 既有 offload 链兜底（视频管线 CPU offload /
    vLLM gpu_memory_utilization=0.85 预算）= 保证质量、延长生成时间。
    守卫自身不做降参降质动作。

动作链（调度器每秒调用 check()，独立于模式滞回，类比 thermal_guard）：
  RAM ≥ 80%（预警）   : gc.collect + torch.cuda.empty_cache（10s 节流）
  RAM ≥ 85%（动作线） : 卸载空闲大模型（低优先级→大体积序）→ gc →
                        Windows 工作集收缩 EmptyWorkingSet；60s 冷却
  VRAM ≥ 90%（动作线）: 逐个卸载空闲低优先级模型，每卸一个重采样，
                        回到线下即停；无可卸时广播提示（活动任务由
                        offload 机制保质量）。30s 冷却

安全约束（与 release_for_module 同一套语义）：
  - 功能锁持有中的活动功能类别不卸（不拆运行中任务的管线）；
  - 共享小模型类别（embedding/voice/auxiliary）不卸；
  - 卸载复用 model_manager.unload_model 完整释放链。

单例用法::

    from src.services.resource_guard import get_resource_guard
    guard = get_resource_guard()
    guard.check(mem_used_percent, vram_used_ratio)
"""
from __future__ import annotations

import gc
import logging
import threading
import time

from ..config import THRESHOLDS

log = logging.getLogger("omnispace.resource_guard")

# ── 阈值（config.yaml scheduler.thresholds 可覆盖）──────────────
# RAM 动作线：mem_critical_ratio=0.15 可用占比 = 已用 85%（文档B §4.1）
RAM_HARD_RATIO = 1.0 - float(THRESHOLDS.get("mem_critical_ratio", 0.15))
RAM_SOFT_RATIO = 1.0 - float(THRESHOLDS.get("mem_warning_ratio", 0.25))
# RAM 危急线（2026-09-02 静默死亡事故）：已用 90%——WER 取证
# RADAR_PRE_LEAK_64 实证提交耗尽原生硬死；动作线卸完空闲模型后仍
# 可能持续高位（可再生缓存占着），危急线收缩可再生内存防打顶。
RAM_CRITICAL_RATIO = float(THRESHOLDS.get("mem_critical2_ratio", 0.90))
# VRAM 动作线：gpu_vram_critical=0.90（用户裁定硬限制）
VRAM_HARD_RATIO = float(THRESHOLDS.get("gpu_vram_critical", 0.90))
# 提交内存动作线（2026-09-05 A2 补口，架构升级计划 P0-2）：WER 取证
# RADAR_PRE_LEAK_64 实证死因是提交（commit）耗尽——提交上限=物理+页面
# 文件，物理 RAM 百分比盯不住它。GetPerformanceInfo 与 WER 同源口径；
# 90% 动作线=本机 59.2G 上限留 ~6G 缓冲。
COMMIT_HARD_RATIO = float(THRESHOLDS.get("commit_critical_ratio", 0.90))

# 动作冷却（秒）：避免阈值边缘抖动导致反复卸载/装载 thrashing
_RAM_SHED_COOLDOWN_S = 60.0
_RAM_CRITICAL_COOLDOWN_S = 120.0
_VRAM_SHED_COOLDOWN_S = 30.0
_SOFT_COLLECT_THROTTLE_S = 10.0

# 跨模块共享小模型类别（与 model_manager._SHARED_KEEP_CATEGORIES 一致）
_SHARED_KEEP = {"embedding", "voice", "auxiliary"}


class ResourceGuard:
    """RAM/VRAM 硬限制守卫（进程内单例，线程安全）。"""

    _instance: ResourceGuard | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ram_shed = 0.0
        self._last_vram_shed = 0.0
        self._last_commit_shed = 0.0
        self._last_critical_shed = 0.0
        self._last_soft_collect = 0.0
        self._last_broadcast = 0.0
        # 状态快照（get_status / hardware API 透出）
        self._ram_percent = 0.0
        self._vram_ratio = 0.0
        self._commit_ratio = 0.0
        self._ram_shed_count = 0
        self._vram_shed_count = 0
        self._commit_shed_count = 0
        self._last_event = "normal"       # normal/ram_soft/ram_shed/vram_shed
        self._last_event_at = 0.0
        self._last_detail: str = ""

    @classmethod
    def instance(cls) -> ResourceGuard:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── 主检查入口（调度器每秒调用）──────────────────────────────
    def check(self, mem_used_percent: float, vram_used_ratio: float) -> None:
        """按当前 RAM/VRAM 占用推进守卫动作。

        Args:
            mem_used_percent: 系统 RAM 已用百分比（0~100，psutil）
            vram_used_ratio:  GPU 显存已用占比（0~1，nvml）
        """
        self._ram_percent = mem_used_percent
        self._vram_ratio = vram_used_ratio

        # RAM 预警线（≥80%）：轻量回收，不卸载
        if mem_used_percent >= RAM_SOFT_RATIO * 100:
            self._soft_collect()

        # RAM 动作线（≥85%）：强制降载空闲模型
        if mem_used_percent >= RAM_HARD_RATIO * 100:
            self._shed_ram(mem_used_percent)

        # VRAM 动作线（≥90%）：卸载空闲模型直到回到线下
        if vram_used_ratio >= VRAM_HARD_RATIO:
            self._shed_vram(vram_used_ratio)

        # 提交内存动作线（≥90%，2026-09-05 A2 补口）：采样在守卫内部
        # 完成，调度器调用方零改动；非 Windows/采样失败返回 None 静默跳过
        commit = _commit_charge_ratio()
        if commit is not None:
            self._commit_ratio = commit
            if commit >= COMMIT_HARD_RATIO:
                self._shed_commit(commit)

    # ── RAM 预警：轻量回收 ──────────────────────────────────────
    def _soft_collect(self) -> None:
        now = time.monotonic()
        if now - self._last_soft_collect < _SOFT_COLLECT_THROTTLE_S:
            return
        self._last_soft_collect = now
        self._record_event("ram_soft",
                           f"RAM {self._ram_percent:.0f}% 越预警线，主动回收")
        gc.collect()
        try:
            import torch  # type: ignore
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 - torch 缺失时仅 gc
            pass

    # ── RAM 动作线：卸载空闲大模型 + 工作集收缩 ─────────────────
    def _shed_ram(self, percent: float) -> None:
        now = time.monotonic()
        if now - self._last_ram_shed < _RAM_SHED_COOLDOWN_S:
            return
        self._last_ram_shed = now

        freed = self._evict_idle_models("ram")
        # 卸载后回收：Python 堆 + CUDA 缓存 + Windows 工作集
        gc.collect()
        try:
            import torch  # type: ignore
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        self._shrink_working_set()
        self._ram_shed_count += 1
        detail = (f"RAM {percent:.0f}% 越线，卸载 {freed} 个空闲模型并回收内存"
                  if freed else
                  f"RAM {percent:.0f}% 越线，无空闲模型可卸，已做 gc/工作集收缩")
        self._record_event("ram_shed", detail)
        self._broadcast("resource_ram_high",
                        f"运行内存占用 {percent:.0f}% 超过 {RAM_HARD_RATIO*100:.0f}% 限制，"
                        + (f"已释放 {freed} 个空闲模型" if freed else "无可卸载的空闲模型"),
                        warning=True)
        _log_event("ram_shed", detail, level="warning")
        # 危急线（≥90%）：动作线之上的追加收缩——把可再生内存（权重
        # 缓存/磁盘扫描缓存）全吐出来。数据可再生（miss 自动重建），
        # 不碰已加载模型与运行中任务，宁可激进防提交打顶硬死。
        if (percent >= RAM_CRITICAL_RATIO * 100
                and now - self._last_critical_shed >= _RAM_CRITICAL_COOLDOWN_S):
            self._last_critical_shed = now
            try:
                from .model_manager import get_model_manager
                shed = get_model_manager().shed_memory()
                detail = (f"RAM {percent:.0f}% 越危急线（≥"
                          f"{RAM_CRITICAL_RATIO*100:.0f}%），已收缩可再生内存："
                          f"权重缓存卸载 {shed.get('cache_unloaded')} 条、"
                          f"磁盘扫描缓存丢弃 {shed.get('scan_cache_dropped')} 条")
                self._record_event("ram_critical_shed", detail)
                _log_event("ram_critical_shed", detail, level="warning")
                log.warning("RAM 危急收缩: %s", detail)
            except Exception as exc:  # noqa: BLE001 - 收缩失败不影响守卫主链
                log.warning("RAM 危急收缩失败: %s", exc)

    # ── VRAM 动作线：逐个卸载直到回到线下 ───────────────────────
    def _shed_vram(self, ratio: float) -> None:
        now = time.monotonic()
        if now - self._last_vram_shed < _VRAM_SHED_COOLDOWN_S:
            return
        self._last_vram_shed = now

        candidates = self._evict_candidates()
        if not candidates:
            self._record_event(
                "vram_shed",
                f"显存 {ratio*100:.0f}% 越线但无空闲模型可卸"
                "（活动任务由 offload 机制保质量、延长生成时间）")
            self._broadcast(
                "resource_vram_high",
                f"显存占用 {ratio*100:.0f}% 超过 {VRAM_HARD_RATIO*100:.0f}% 限制；"
                "当前任务全在运行中，已自动切换为时间换空间模式（质量不变）",
                warning=True)
            _log_event("vram_shed_no_candidate",
                       f"显存 {ratio*100:.0f}%，无可卸空闲模型", level="warning")
            return

        freed = 0
        for entry in candidates:
            try:
                if self._unload_entry(entry):
                    freed += 1
                    log.warning("显存越线降载: 卸载 %s (%.1fGB)",
                                entry["model_id"], entry.get("vram_gb", 0.0))
            except Exception as exc:  # noqa: BLE001
                log.warning("显存降载卸载失败 (%s): %s",
                            entry.get("model_id"), exc)
            # 重采样：回到线下即停（保留剩余空闲模型，避免过度卸载）。
            # 新建 monitor 实例绕过 TTL 缓存，直达 nvml 取即时值
            from .scheduler.monitor import HardwareMonitor
            gpu = HardwareMonitor().get_gpu()
            total = int(gpu.get("vram_total_mb", 0))
            used = int(gpu.get("vram_used_mb", 0))
            if total > 0 and used / total < VRAM_HARD_RATIO:
                break
        self._vram_shed_count += 1
        detail = f"显存 {ratio*100:.0f}% 越线，卸载 {freed} 个空闲模型"
        self._record_event("vram_shed", detail)
        self._broadcast("resource_vram_high",
                        f"显存占用超 {VRAM_HARD_RATIO*100:.0f}% 限制，"
                        f"已释放 {freed} 个空闲模型", warning=True)
        _log_event("vram_shed", detail, level="warning")

    # ── 提交内存动作线：与 RAM 动作线同一卸载链 ─────────────────
    def _shed_commit(self, ratio: float) -> None:
        """提交内存越线：卸空闲模型 + 工作集收缩 + 危急收缩一链到底。

        与 RAM 危急线的语义差异：提交打顶（100%）即 WER 原生硬死，
        没有第二档余地——动作线触发时直接并入危急收缩（shed_memory）。
        独立事件名（commit_shed）进心跳历史，A3 压测按此取证。
        """
        now = time.monotonic()
        if now - self._last_commit_shed < _RAM_SHED_COOLDOWN_S:
            return
        self._last_commit_shed = now

        freed = self._evict_idle_models("commit")
        gc.collect()
        try:
            import torch  # type: ignore
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        self._shrink_working_set()
        self._commit_shed_count += 1
        detail = (f"提交内存 {ratio*100:.0f}% 越线（WER RADAR 同源口径），"
                  f"卸载 {freed} 个空闲模型")
        self._record_event("commit_shed", detail)
        self._broadcast("resource_commit_high",
                        f"提交内存占用 {ratio*100:.0f}% 超过 "
                        f"{COMMIT_HARD_RATIO*100:.0f}% 限制，"
                        + (f"已释放 {freed} 个空闲模型" if freed
                           else "无可卸载的空闲模型"),
                        warning=True)
        _log_event("commit_shed", detail, level="warning")
        try:
            from .model_manager import get_model_manager
            shed = get_model_manager().shed_memory()
            msg = (f"提交内存越线已收缩可再生内存：权重缓存卸载 "
                   f"{shed.get('cache_unloaded')} 条、磁盘扫描缓存丢弃 "
                   f"{shed.get('scan_cache_dropped')} 条")
            self._record_event("commit_critical_shed", msg)
            _log_event("commit_critical_shed", msg, level="warning")
        except Exception as exc:  # noqa: BLE001 - 收缩失败不影响守卫主链
            log.warning("提交内存危急收缩失败: %s", exc)

    # ── 卸载候选筛选 ────────────────────────────────────────────
    def _evict_candidates(self) -> list[dict]:
        """空闲可卸模型列表（低优先级 → 大体积排序）。

        排除：功能锁活动功能类别 + 共享小模型（embedding/voice/auxiliary）
        + 常驻对话模型。
        """
        from ..middleware.feature_lock import get_feature_lock
        from .model_manager import _FEATURE_KEEP_CATEGORIES, get_model_manager

        keep_cats: set[str] = set(_SHARED_KEEP)
        try:
            active = get_feature_lock().active_feature
            if active:
                keep_cats |= _FEATURE_KEEP_CATEGORIES.get(active, set())
        except Exception:  # noqa: BLE001
            pass

        # 常驻对话模型保护（2026-08-22 ResourceGuard 误卸事故）：vLLM
        # 对话模型按设计常驻（gpu_memory_utilization=0.85 整卡预算，
        # 16GB 卡 VRAM 常态 ~99% 是预期状态），就绪初期无对话请求
        # （功能锁未持有）曾被当"空闲模型"卸载——就绪 4s 即被杀，
        # 用户侧表现为 157s 冷启动死循环。守卫兜底不得拆常驻模型；
        # 模块切换释放由 release_for_module 统一管理（带功能锁保护）。
        resident_ids: set[str] = set()
        try:
            from .inference.dialog_engine import get_dialog_engine
            st = get_dialog_engine().get_status()
            if st.get("state") == "ready" and st.get("model"):
                resident_ids.add(str(st["model"]))
        except Exception:  # noqa: BLE001
            pass

        loaded = get_model_manager().get_loaded_models()
        candidates = [e for e in loaded
                      if e.get("category") not in keep_cats
                      and e.get("model_id") not in resident_ids]
        # 台账外兜底：paint 引擎管线（2026-08-23 RAM 滞留事故根因——
        # qwen-image GGUF 12.31GB 经 paint_engine 直连 diffusers 加载，
        # 不入 model_manager 台账，任务完成后 resource_guard 看不到它，
        # 权重永久滞留 RAM，系统空闲仅剩 0.6GB）。
        # 2026-08-26 e2e 事故修复：兜底条目此前无条件 append，绕过
        # keep_cats 功能锁保护——paint 锁持有中（方案 A 逐镜循环）
        # FLUX 仍被当「空闲模型」卸载，第 4 镜 PAINT_GENERATION_
        # FAILED。兜底条目必须与 ledger 条目同一过滤语义：活动
        # 功能类别不卸（不拆运行中任务的管线），锁释放后才可回收。
        if "paint" not in keep_cats:
            try:
                from .inference.paint_engine import get_paint_engine
                st = get_paint_engine().get_status()
                if st.get("state") == "ready" and st.get("model"):
                    candidates.append({
                        "model_id": str(st["model"]),
                        "category": "paint",
                        "vram_gb": 0.0,
                        "ram_gb": (12.31 if str(st["model"]).startswith(
                            "qwen-image") else 0.0),
                        "priority": 9,
                        "_engine": "paint",
                    })
            except Exception:  # noqa: BLE001 - 引擎不可用时无兜底条目
                pass
        # 低优先级数值小者先卸；同优先级大模型先卸（一次释放更多）
        candidates.sort(key=lambda e: (
            e.get("priority", 0), -float(e.get("vram_gb", 0.0))))
        return candidates

    @staticmethod
    def _unload_entry(entry: dict) -> bool:
        """卸载单个候选：台账模型走 model_manager，引擎直连模型
        （_engine 标记）走对应引擎的 unload_model()。"""
        engine = entry.get("_engine")
        if engine == "paint":
            from .inference.paint_engine import get_paint_engine
            return get_paint_engine().unload_model()
        from .model_manager import get_model_manager
        return get_model_manager().unload_model(entry["model_id"])

    def _evict_idle_models(self, reason: str) -> int:
        """RAM 场景卸载：一次卸掉全部空闲候选（CPU 镜像随进程释放）。"""
        candidates = self._evict_candidates()
        freed = 0
        for entry in candidates:
            try:
                if self._unload_entry(entry):
                    freed += 1
                    log.warning("RAM 越线降载(%s): 卸载 %s (%.1fGB)",
                                reason, entry["model_id"],
                                entry.get("ram_gb", 0.0)
                                or entry.get("vram_gb", 0.0))
            except Exception as exc:  # noqa: BLE001
                log.warning("RAM 降载卸载失败 (%s): %s",
                            entry.get("model_id"), exc)
        return freed

    @staticmethod
    def _shrink_working_set() -> None:
        """Windows：收缩本进程工作集（把不活跃页换出，立即降 RAM 峰值）。

        句柄铁律（2026-08-26 RAM 滞留事故修复）：GetCurrentProcess()
        返回伪句柄 -1，但 ctypes.windll 默认 restype=c_int 会把它截断为
        32 位 0xFFFFFFFF → EmptyWorkingSet 静默失败（返回 False 无人检
        查），守卫降载后已 free 的权重页滞留工作集、系统 RAM 不回落
        （实测 klein-4B 卸载后 uss 停 16.6GB；正确传 c_void_p(-1) 后
        uss 立即归零）。必须直接构造 64 位伪句柄。
        """
        try:
            import ctypes
            psapi = ctypes.windll.psapi      # type: ignore[attr-defined]
            ok = bool(psapi.EmptyWorkingSet(ctypes.c_void_p(-1)))
            if not ok:
                log.debug("EmptyWorkingSet 调用失败（权限不足或非 Windows）")
        except Exception:  # noqa: BLE001 - 非 Windows/权限不足时跳过
            pass

    # ── 状态与广播 ──────────────────────────────────────────────
    def _record_event(self, event: str, detail: str) -> None:
        self._last_event = event
        self._last_event_at = time.time()
        self._last_detail = detail
        log.warning("资源守卫: %s", detail)

    def _broadcast(self, event: str, message: str,
                   warning: bool = False) -> None:
        """WS 广播（10s 节流，状态类事件不刷屏）。"""
        now = time.time()
        if now - self._last_broadcast < 10.0:
            return
        self._last_broadcast = now
        try:
            from .ws_hub import get_ws_hub
            get_ws_hub().broadcast({
                "type": "status",
                "module": "system",
                "data": {"event": event,
                         "level": "warning" if warning else "info",
                         "message": message},
            })
        except Exception as exc:  # noqa: BLE001 - 广播失败不影响守卫
            log.debug("资源守卫广播失败（忽略）: %s", exc)

    def get_status(self) -> dict:
        """守卫状态快照（供 /hardware 聚合透出）。"""
        with self._lock:
            return {
                "ram_limit_percent": round(RAM_HARD_RATIO * 100, 1),
                "vram_limit_ratio": VRAM_HARD_RATIO,
                "ram_used_percent": round(self._ram_percent, 1),
                "vram_used_ratio": round(self._vram_ratio, 3),
                "commit_used_ratio": round(self._commit_ratio, 3),
                "commit_limit_percent": round(COMMIT_HARD_RATIO * 100, 1),
                "last_event": self._last_event,
                "last_event_at": self._last_event_at,
                "last_detail": self._last_detail,
                "session_ram_shed_count": self._ram_shed_count,
                "session_vram_shed_count": self._vram_shed_count,
                "session_commit_shed_count": self._commit_shed_count,
            }


def _commit_charge_ratio() -> float | None:
    """Windows 提交内存占比（CommitTotal/CommitLimit，WER RADAR 同源）。

    非 Windows / 调用失败 / 上限为 0 返回 None——守卫跳过该维度，
    不阻断 RAM/VRAM 主链。
    """
    try:
        import ctypes

        class _PERF_INFO(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("CommitTotal", ctypes.c_size_t),
                ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t),
                ("PhysicalTotal", ctypes.c_size_t),
                ("PhysicalAvailable", ctypes.c_size_t),
                ("SystemCache", ctypes.c_size_t),
                ("KernelTotal", ctypes.c_size_t),
                ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t),
                ("PageSize", ctypes.c_size_t),
                ("HandleCount", ctypes.c_ulong),
                ("ProcessCount", ctypes.c_ulong),
                ("ThreadCount", ctypes.c_ulong),
            ]

        info = _PERF_INFO()
        info.cb = ctypes.sizeof(info)
        psapi = ctypes.windll.psapi      # type: ignore[attr-defined]
        if not psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
            return None
        if not info.CommitLimit:
            return None
        return info.CommitTotal / info.CommitLimit
    except Exception:  # noqa: BLE001 - 非 Windows/权限不足时跳过该维度
        return None


def _log_event(event: str, friendly: str, level: str = "info") -> None:
    """大白话事件日志（写失败静默，不阻断守卫）。"""
    try:
        from .event_log import log_event
        log_event("system", event, friendly, level=level)
    except Exception:  # noqa: BLE001
        pass


# ── 模块级单例 ──────────────────────────────────────────────────
def get_resource_guard() -> ResourceGuard:
    """获取 ResourceGuard 全局单例。"""
    return ResourceGuard.instance()
# 本项目仅供学习使用，商业授权请+Q 3559331368
