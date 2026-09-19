"""功能级互斥锁（规格 §6.1 大模型功能互斥状态机）。

四种重量级 AI 功能互斥：dialog / paint / video_gen / training。
同一时刻、同一资源域仅允许一类在运行，其余被阻断并返回 40007。

批1 多卡地基（2026-09-05）：互斥粒度从「全机一把锁」升级为「按资源
域互斥」（域=卡号字符串，裁决单源 engines/gpu_domains.py）——双卡机
器上对话占 0 卡、绘画占 1 卡可并行；单卡机器所有功能同域，行为与
历史逐比特一致。"remote" 伪域（批3 远程引擎）不占本地卡，与本地
功能天然并行。

非大模型功能（分镜表编辑、资产浏览、项目设置、历史查看、模型管理、
性能监控）始终可用，不受本锁约束。

这是 v2.1 中间件层实现（替代已废弃的 v1.0 src/core/feature_lock.py）。
使用进程内内存状态，单机本地运行（规格 §14 约束1：不上云）。

兼容注（2026-09-05 批1）：lora_training_service / style_lora_service
的同步降级路径改走 acquire_sync / release_sync（与异步路径共享同一
份按域状态），不再直写 _holder 私有字段。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import asyncio
import logging
import threading
import time

from .error_handler import ApiError

log = logging.getLogger("omnispace.feature_lock")

# ── 互斥规则：每个功能阻断同域的其余三类 ──────────────────────
_VALID_FEATURES = ("dialog", "paint", "video_gen", "training")

_BLOCKED: dict[str, list[str]] = {
    "dialog": ["paint", "video_gen", "training"],
    "paint": ["dialog", "video_gen", "training"],
    "video_gen": ["dialog", "paint", "training"],
    "training": ["dialog", "paint", "video_gen"],
}

_BLOCK_MESSAGES: dict[str, str] = {
    "dialog": "当前正在使用对话功能，请结束对话后再使用此功能",
    "paint": "绘画进行中，其他AI功能暂不可用",
    "video_gen": "视频生成中，请等待完成后再切换",
    "training": "训练进行中，其他AI功能暂不可用",
}


def _domain_of(feature: str) -> str:
    """feature → 资源域（懒 import 避免 middleware→engines 循环依赖；
    分配器异常回落 "0" 主卡域，保持单卡语义）。"""
    try:
        from ..engines.gpu_domains import resolve_feature_domain
        return resolve_feature_domain(feature)
    except Exception:  # noqa: BLE001 - 探测失败按主卡域
        return "0"



# ── 批5 P21（2026-09-19）：睡眠保护 ────────────────────────────────
# 重量级任务（对话生成/绘画/视频/训练）持锁期间阻止系统睡眠——防
# 「挂机生成一半电脑睡了」。Windows SetThreadExecutionState；其他
# 平台/调用失败=静默降级（不阻断任务）。引用计数：全域从 0→N 持有
# 时请求清醒，N→0 全释放时恢复可睡。
_sleep_guard_refs = 0


def _sleep_guard_hold() -> None:
    global _sleep_guard_refs
    _sleep_guard_refs += 1
    if _sleep_guard_refs == 1:
        try:
            import ctypes
            # ES_CONTINUOUS(0x80000000) | ES_SYSTEM_REQUIRED(0x1)
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
            log.info("P21 睡眠保护：重量级任务持锁期间阻止系统睡眠")
        except Exception:  # noqa: BLE001 - 非 Windows/调用失败静默降级
            log.debug("P21 睡眠保护不可用（降级）", exc_info=True)


def _sleep_guard_release() -> None:
    global _sleep_guard_refs
    _sleep_guard_refs = max(0, _sleep_guard_refs - 1)
    if _sleep_guard_refs == 0:
        try:
            import ctypes
            # ES_CONTINUOUS 单独 = 恢复系统默认睡眠策略
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            log.info("P21 睡眠保护：任务全部结束，恢复可睡")
        except Exception:  # noqa: BLE001
            pass


class FeatureLockManager:
    """功能级互斥管理器（进程内单例，异步安全）。

    状态按资源域分桶（_holders: domain → feature）：同一功能可重入
    （允许多次 acquire），同域跨功能互斥，异域并行。
    持有者信息用于返回友好的阻断提示。
    """

    _instance: FeatureLockManager | None = None

    def __init__(self) -> None:
        # 域 → 持有功能（域=卡号字符串 / "remote"）
        self._holders: dict[str, str] = {}
        self._hold_counts: dict[str, int] = {}
        self._acquired_ats: dict[str, float] = {}
        self._holder_task_ids: dict[str, str | None] = {}
        # 同功能重入计数（审计 P1-1 修复，2026-08-29）：此前同功能
        # 第二次 acquire 成功后，先完成者的 release 会把锁整个清空，
        # 另一任务仍在运行时跨功能互斥即告失效（视频双任务并发生成
        # 实证路径）。release 递减，归零才真正放锁。批1 起计数按域
        # 分桶（_hold_counts），语义不变。
        self._lock = asyncio.Lock()
        # 跨线程状态锁（2026-09-16 P1 根修）：acquire_sync/release_sync
        # 由训练工作线程调用（lora/style_lora），与事件循环的 async
        # acquire/release 共享上述四个 dict——asyncio.Lock 挡不住别的
        # 线程，此前两路 read-modify-write 无互斥，计数漂移会导致
        # 假释放/永久卡锁。全部读写临界区统一过本 RLock。
        self._state_lock = threading.RLock()
        # 最近一次用户功能活动时间（acquire/release 均刷新，含任意域），
        # 供调度器空闲显存回收判定；进程启动即开始计空闲。
        self._last_activity_at: float = time.time()

    @classmethod
    def instance(cls) -> FeatureLockManager:
        """获取单例（无需 await，单线程事件循环下安全）。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def holders(self) -> dict[str, str]:
        """当前全部域持有快照（domain → feature，只读副本）。"""
        return dict(self._holders)

    @property
    def active_feature(self) -> str | None:
        """兼容快照（单卡语义逐比特不变）。

        唯一持有者原样返回；多域并行持有时返回主卡域持有者（无则
        None）——保守语义：消费方「有活动就不卸载/不回收」的判断在
        多卡下依然成立。完整持有表走 status()["holders"]。
        """
        if not self._holders:
            return None
        if len(self._holders) == 1:
            return next(iter(self._holders.values()))
        primary = _domain_of("dialog")
        return self._holders.get(primary)

    @property
    def idle_seconds(self) -> float:
        """距最近一次功能活动的秒数（任意域有锁持有时为 0）。"""
        if self._holders:
            return 0.0
        return max(0.0, time.time() - self._last_activity_at)

    def is_feature_active(self, feature: str) -> bool:
        """该功能是否正在任意域持有（批1 新增：多域下精确查询）。"""
        return feature in self._holders.values()

    def get_block_reason(self, feature: str) -> str | None:
        """如果 feature 被阻断，返回阻断原因消息；否则返回 None。

        按域判定：只有 feature 所属域被其他功能持有时才阻断。
        """
        dom = _domain_of(feature)
        holder = self._holders.get(dom)
        if holder is None or holder == feature:
            return None
        if feature in _BLOCKED.get(holder, []):
            return _BLOCK_MESSAGES.get(
                holder, f"{holder} 进行中，请稍后再试")
        return None

    async def acquire(self, feature: str,
                      task_id: str | None = None,
                      *,
                      domain: str | None = None) -> bool:
        """尝试获取功能锁，成功返回 True，被阻断返回 False。

        同一功能可重入：当本域持有者 == feature 时直接成功。
        domain 显式传入时跳过资源域解析（批3 远程引擎用 REMOTE_DOMAIN）。
        """
        if feature not in _VALID_FEATURES:
            raise ApiError("UNSUPPORTED_FORMAT", f"无效的 feature：{feature}",
                           detail={"feature": feature,
                                   "valid": list(_VALID_FEATURES)})
        dom = domain if domain is not None else _domain_of(feature)
        async with self._lock:
            with self._state_lock:
                holder = self._holders.get(dom)
                if holder is not None and holder != feature:
                    return False
                if holder == feature:
                    self._hold_counts[dom] = self._hold_counts.get(dom, 1) + 1
                else:
                    self._holders[dom] = feature
                    self._hold_counts[dom] = 1
                    self._acquired_ats[dom] = time.time()
                    _sleep_guard_hold()  # P21 睡眠保护
                self._holder_task_ids[dom] = task_id
                self._last_activity_at = time.time()
                log.info("功能锁获取: %s (域=%s, task=%s, 重入=%d)",
                         feature, dom, task_id, self._hold_counts[dom])
                return True

    async def release(self, feature: str,
                      *,
                      domain: str | None = None) -> None:
        """释放功能锁（仅当当前域持有者与 feature 一致时）。

        计数式释放（审计 P1-1）：同功能多次 acquire 须等额 release，
        计数归零才放锁——先完成的请求不再提前瓦解互斥。
        """
        dom = domain if domain is not None else _domain_of(feature)
        async with self._lock:
            with self._state_lock:
                found: str = dom
                if self._holders.get(found) != feature:
                    # 域配置热切换兜底（批3：远程开关切换瞬间 acquire/release
                    # 解析出的域可能不一致）——从任意域找该功能的持有记录
                    alt = next((d for d, f in self._holders.items()
                                if f == feature), None)
                    if alt is None or self._holders.get(alt) != feature:
                        return
                    found = alt
                held = time.time() - self._acquired_ats.get(found, time.time())
                self._hold_counts[found] = max(
                    0, self._hold_counts.get(found, 1) - 1)
                if self._hold_counts[found] == 0:
                    log.info("功能锁释放: %s (域=%s, 持有 %.1fs)",
                             feature, found, held)
                    self._holders.pop(found, None)
                    self._hold_counts.pop(found, None)
                    self._holder_task_ids.pop(found, None)
                    self._last_activity_at = time.time()
                    _sleep_guard_release()  # P21
                else:
                    log.info("功能锁递减: %s (域=%s, 剩余重入=%d)",
                             feature, found, self._hold_counts[found])

    def acquire_sync(self, feature: str,
                     task_id: str | None = None) -> bool:
        """同步降级获取（批1 接管 lora/style 训练服务直写私有字段的
        旧路径）：无事件循环可用时调用；与异步路径共享同一份按域状态。
        """
        if feature not in _VALID_FEATURES:
            return False
        dom = _domain_of(feature)
        with self._state_lock:
            holder = self._holders.get(dom)
            if holder is not None and holder != feature:
                return False
            if holder == feature:
                self._hold_counts[dom] = self._hold_counts.get(dom, 1) + 1
            else:
                self._holders[dom] = feature
                self._hold_counts[dom] = 1
                self._acquired_ats[dom] = time.time()
                _sleep_guard_hold()  # P21
            self._holder_task_ids[dom] = task_id
            self._last_activity_at = time.time()
            log.info("功能锁获取(同步降级): %s (域=%s, task=%s, 重入=%d)",
                     feature, dom, task_id, self._hold_counts[dom])
            return True

    def release_sync(self, feature: str) -> None:
        """同步降级释放（与 acquire_sync 对称；计数归零才放锁）。"""
        with self._state_lock:
            found: str = _domain_of(feature)
            if self._holders.get(found) != feature:
                # 域配置热切换兜底（同 release）
                alt = next((d for d, f in self._holders.items()
                            if f == feature), None)
                if alt is None or self._holders.get(alt) != feature:
                    return
                found = alt
            self._hold_counts[found] = max(
                0, self._hold_counts.get(found, 1) - 1)
            if self._hold_counts[found] == 0:
                log.info("功能锁释放(同步降级): %s (域=%s)", feature, found)
                self._holders.pop(found, None)
                self._hold_counts.pop(found, None)
                self._holder_task_ids.pop(found, None)
                self._last_activity_at = time.time()
                _sleep_guard_release()  # P21

    def status(self) -> dict:
        """返回当前互斥状态快照。

        active_feature/held_seconds/task_id 三键保持旧口径（单卡语义
        逐比特不变）；holders 数组为批1 新增（多域并行全貌）。
        """
        active = self.active_feature
        active_dom: str | None = None
        if active is not None:
            active_dom = next(
                (d for d, f in sorted(self._holders.items())
                 if f == active), None)
        holders = [
            {
                "domain": d,
                "feature": f,
                "task_id": self._holder_task_ids.get(d),
                "held_seconds": round(
                    time.time() - self._acquired_ats.get(d, time.time()), 1),
            }
            for d, f in sorted(self._holders.items())
        ]
        return {
            "active_feature": active,
            "held_seconds": round(
                time.time() - self._acquired_ats.get(active_dom, time.time()),
                1) if (active and active_dom) else 0,
            "task_id": (self._holder_task_ids.get(active_dom)
                        if active_dom is not None else None),
            "holders": holders,
        }


def get_feature_lock() -> FeatureLockManager:
    """获取功能锁单例。"""
    return FeatureLockManager.instance()


async def acquire_or_raise(feature: str,
                           task_id: str | None = None) -> FeatureLockManager:
    """获取功能锁；被阻断时抛 ApiError("FEATURE_MUTEX_LOCKED")。

    供 API 端点使用：
        lock = await acquire_or_raise("dialog")
        try:
            ... do work ...
        finally:
            await lock.release("dialog")

    审计 P0-3：GPU 温度 >= 临界值（90°C）时热保护暂停生效——
    一切新重量级任务在锁入口统一拒绝（20004），进行中的任务由
    调度器 ALL_TENSE 强制卸载兜底中断（规格 §4.1.3 / TASK-016）。
    """
    # 热保护检查（惰性 import 避免 middleware->services 循环依赖）
    try:
        from ..services.thermal_guard import get_thermal_guard
        guard = get_thermal_guard()
        if guard.is_paused():
            status = guard.get_status()
            raise ApiError("HARDWARE_THERMAL_THROTTLE",
                f"GPU 温度过高（{status['last_temp_celsius']:.0f}°C），"
                f"已强制暂停生成任务，请等待散热后重试",
                detail={"feature": feature,
                        "temp_celsius": status["last_temp_celsius"],
                        "critical_threshold": status["critical_threshold"]})
    except ApiError:
        raise
    except Exception:  # noqa: BLE001 - 热保护探测失败不阻断正常流程
        log.debug("acquire_or_raise: 降级忽略", exc_info=True)

    mgr = get_feature_lock()
    success = await mgr.acquire(feature, task_id=task_id)
    if not success:
        reason = mgr.get_block_reason(feature) or "当前已有其他AI功能运行中"
        raise ApiError("FEATURE_MUTEX_LOCKED", reason,
                       detail={"active_feature": mgr.active_feature,
                               "feature": feature})
    return mgr
