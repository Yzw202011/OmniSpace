"""GPU 显存统一账本（显存调度机制批1/批2，2026-09-10）。

设计真源：docs/显存调度机制方案-2026-09-10.md §3.1/§3.3/§3.6
（用户拍板 D1=A 五批全做 / D3=A 准入先顾问后硬闸，2026-09-10）。

批1（纯新增零接线）：``GpuBudget.snapshot`` 每卡供需快照、预留表、
``reconcile`` 收账钩子、``BusyRegistry`` 忙碌登记雏形。

批2（准入编排，**顾问模式**——只判定+登记+日志，不执行让位、
不拦截、不影响任何现有判定；硬闸翻转=批3 实弹验证后）：
  - ``request`` / ``release``：重型任务统一准入入口。判定
    budget_gb（空闲−预留）≥ need_gb → GRANTED；不足 → WAIT（附
    建议让位阶梯，仅提示）/ DENY。GRANTED 登记 busy（release 注销）；
    顾问模式**不记逻辑预留**（避免改变 vllm_service 准入闸等现有
    行为），非顾问模式才记账。
  - ``read_physical_bytes``：字节级物理读数唯一底层（torch 优先，
    ``torch_only=True`` 不回落 pynvml——WDDM 下 NVML 口径含系统驻留，
    vLLM 准入闸保持历史 torch-only 口径逐比特不变）。
  - 台账源由 model_manager 注入（``set_ledger_source``，批2 接线），
    本模块不反向依赖大单例。

铁律：**账本失明不阻断业务**——任何读数异常一律返回
available=False 的快照/零值，绝不向外抛异常（最坏退化为现状的
「直接看物理读数」行为）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

_GB = float(1024 ** 3)

log = logging.getLogger("omnispace.gpu_budget")

# 账外占用（external_gb）异常判定线（GB）：used − 台账 − 预留持续
# 高于此值视为孤儿进程/台账漂移，reconcile 触发收账钩子。取 2.0：
# 桌面/浏览器常态账外驻留 ~2GB 属正常波动（docs/架构升级计划-
# 2026-09-05.md 口径），超过即有可疑大额账外占用（孤儿 vLLM 实测
# 13.9GB 级，vllm_service._reap_orphans 事故史）。
EXTERNAL_ANOMALY_GB = 2.0


def _read_torch(device: int) -> tuple[bool, int, int]:
    """torch 通道（CUDA 可分配口径，WDDM 承诺制——子进程持有量不全可见）。"""
    try:
        import torch

        if torch.cuda.is_available():
            free_b, total_b = torch.cuda.mem_get_info(device)
            return True, int(free_b), int(total_b)
    except Exception:  # noqa: BLE001 - 通道失败由调用方降级
        pass
    return False, 0, 0


def _read_nvml(device: int) -> tuple[bool, int, int]:
    """NVML 通道（驱动驻留口径——WDDM 全部驻留含子进程）。"""
    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(device)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        return True, int(mem.free), int(mem.total)
    except Exception:  # noqa: BLE001 - 通道失败由调用方降级
        pass
    return False, 0, 0


def read_physical_bytes(
    device: int = 0, *, torch_only: bool = False
) -> tuple[bool, int, int]:
    """物理读数（字节）→ (ok, free_b, total_b)——**保守双口径**（F-4 修复，
    2026-09-10 严格测试实证：GGUF 子进程在跑时 torch 口径 free=13.4GB
    而 NVML 实际 7.2GB，高估 6.18GB——乐观口径下 12GB 级装载需求会误
    放行致 OOM，故默认取 free=min(torch, nvml)、total=max）。

    torch_only=True 保持历史 torch 纯口径——vllm_service 准入闸专用
    （行为等价性要求；该闸自身体量 vLLM 装载语义）。双通道均失败返回
    (False, 0, 0)，不抛异常。
    """
    t_ok, t_free, t_total = _read_torch(device)
    if torch_only:
        return t_ok, t_free, t_total
    n_ok, n_free, n_total = _read_nvml(device)
    if t_ok and n_ok:
        return True, min(t_free, n_free), max(t_total, n_total)
    if t_ok:
        return t_ok, t_free, t_total
    return n_ok, n_free, n_total


@dataclass(frozen=True)
class BudgetSnapshot:
    """单卡显存供需一帧（物理读数为最终真值，台账为解释层）。"""

    device: int
    available: bool
    total_gb: float
    free_gb: float
    used_gb: float
    ledger_gb: float
    """台账在载模型显存合计（该卡，经注入的台账源读取）。"""

    reserved_gb: float
    """已批准未落地的逻辑预留合计（request/release 记账）。"""

    external_gb: float
    """used − ledger − reserved ≈ 账外占用（外部进程/孤儿/漂移，含
    WDDM 系统驻留常态基线——判异常看 external_anomaly_gb）。"""

    external_anomaly_gb: float
    """账外异常余量 = external − 动态基线（F-4 次生修复）：>0 表示
    超出该卡常态驻留的账外占用，孤儿/漂移对账判据。"""

    @property
    def budget_gb(self) -> float:
        """当前可批额度 = 物理空闲 − 逻辑预留。"""
        return max(0.0, round(self.free_gb - self.reserved_gb, 2))


class Verdict(str, Enum):
    """准入判定结果。"""

    GRANTED = "granted"
    """额度足够，放行（已登记 busy）。"""

    WAIT = "wait"
    """额度不足但存在让位路径（附建议阶梯——批2 顾问模式仅提示，
    让位执行在批3 单一编排者统一后接入）。"""

    DENY = "deny"
    """不可满足（allow_yield=False 时）——附出路提示。"""

    # DOWNGRADE：批4 接 DIALOG_TIERS 档位表时启用（降档建议）


@dataclass(frozen=True)
class RequestResult:
    """request() 的判定结果（顾问帧日志/批3 硬闸依据）。"""

    verdict: Verdict
    feature: str
    need_gb: float
    device: int
    advisory: bool
    snapshot: BudgetSnapshot
    reason: str
    busy_token: str = ""


class GpuBudget:
    """显存统一账本单例。

    批1：读数 + 预留表 + 对账钩子；批2：准入编排（顾问模式）；
    批3：让位执行（L0~L2）收归单一编排者。线程安全（模块内
    threading.Lock；不涉及跨进程协调——跨进程真相始终以物理读数
    为准）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reserved: dict[int, float] = {}
        # 台账供给回调（device → 该卡在载模型显存合计 GB）。由
        # model_manager 侧注入——本模块不反向 import 大单例，避免
        # 循环依赖与冷启动开销；未注入时 ledger_gb 恒 0（external
        # 退化为 used − reserved，仍可用）。
        self._ledger_source: Callable[[int], float] | None = None
        self._reconcile_hooks: list[Callable[[BudgetSnapshot], None]] = []
        # F-4 次生修复（2026-09-10）：保守口径下 external 含 WDDM
        # 系统驻留常态基线（实测 ~4-5GB），固定 2.0GB 线常态越线、
        # 判定语义失效——改为滑动基线：基线随外部占用的**下行**缓慢
        # 跟随、上行立即跟随（孤儿出现立刻抬高基线不成灾），异常 =
        # 超出基线 EXTERNAL_ANOMALY_GB 以上。
        self._external_baseline: dict[int, float] = {}

    # ── 注入接口 ────────────────────────────────────────────────

    def set_ledger_source(self, source: Callable[[int], float]) -> None:
        """注册台账供给回调（device → 该卡在载模型合计 GB）。"""
        with self._lock:
            self._ledger_source = source

    def add_reconcile_hook(
        self, hook: Callable[[BudgetSnapshot], None]
    ) -> None:
        """注册收账钩子：external 超线时按注册序调用（批3 接孤儿
        收账，如 vllm_service.reap_orphans）。钩子异常逐个吞掉，
        不阻断其余钩子与业务。"""
        with self._lock:
            self._reconcile_hooks.append(hook)

    # ── 逻辑预留表（非顾问 request/release 记账）───────────────

    def reserve(self, device: int, gb: float) -> None:
        """登记逻辑预留（已批准未落地的显存额度）。"""
        with self._lock:
            self._reserved[device] = self._reserved.get(device, 0.0) + float(gb)

    def release_reservation(self, device: int, gb: float) -> None:
        """释放逻辑预留（钳制非负，重复释放安全）。"""
        with self._lock:
            cur = self._reserved.get(device, 0.0)
            self._reserved[device] = max(0.0, cur - float(gb))

    def clear_reservations(self, device: int | None = None) -> None:
        """清空预留（全部或指定卡）——异常兜底/测试用。"""
        with self._lock:
            if device is None:
                self._reserved.clear()
            else:
                self._reserved.pop(device, None)

    # ── 快照与对账 ──────────────────────────────────────────────

    def _reserved_gb(self, device: int) -> float:
        with self._lock:
            return self._reserved.get(device, 0.0)

    def _ledger_gb(self, device: int) -> float:
        with self._lock:
            source = self._ledger_source
        if source is None:
            return 0.0
        try:
            return max(0.0, float(source(device)))
        except Exception:  # noqa: BLE001 - 台账源异常按 0，不阻断
            return 0.0

    def snapshot(self, device: int = 0) -> BudgetSnapshot:
        """组装单卡供需快照（读数失败返回 available=False 零值帧）。"""
        ok, free_b, total_b = read_physical_bytes(device)
        free_gb, total_gb = free_b / _GB, total_b / _GB
        ledger = self._ledger_gb(device) if ok else 0.0
        reserved = self._reserved_gb(device)
        used_gb = max(0.0, total_gb - free_gb)
        external = max(0.0, used_gb - ledger - reserved) if ok else 0.0
        anomaly_gb = self._external_anomaly_gb(device, external) if ok else 0.0
        return BudgetSnapshot(
            device=device,
            available=ok,
            total_gb=round(total_gb, 2),
            free_gb=round(free_gb, 2),
            used_gb=round(used_gb, 2),
            ledger_gb=round(ledger, 2),
            reserved_gb=round(reserved, 2),
            external_gb=round(external, 2),
            external_anomaly_gb=round(anomaly_gb, 2),
        )

    def _external_anomaly_gb(self, device: int, external: float) -> float:
        """账外异常余量 = external − 动态基线（F-4 次生修复）。

        基线规则：
          - 冷启动基线钳常态线以下（重启时孤儿已在跑不得被"学会"）；
          - 上行：**先判定后跟随**——超出基线 EXTERNAL_ANOMALY_GB 以上
            的部分先作为异常余量返回（孤儿跳升报警），基线仍上移
            （连续超线只报一次量级，不重复累积）；
          - 下行每次调用至多回落 0.5GB（孤儿消失后缓慢回归）。
        """
        with self._lock:
            base = self._external_baseline.get(device)
            if base is None:
                base = min(external, EXTERNAL_ANOMALY_GB)
                self._external_baseline[device] = base
            if external > base:
                anomaly = external - base
                self._external_baseline[device] = external
                return anomaly
            new_base = max(external, base - 0.5)
            self._external_baseline[device] = new_base
            return max(0.0, external - new_base)

    def reconcile(
        self, snap: BudgetSnapshot | None = None, device: int = 0
    ) -> bool:
        """对账：账外占用超线 → 按注册序触发收账钩子。

        返回是否触发。available=False（账本失明）不触发——无读数
        即无对账依据，保持现状行为。
        """
        if snap is None:
            snap = self.snapshot(device)
        if not snap.available \
                or snap.external_anomaly_gb < EXTERNAL_ANOMALY_GB:
            return False
        with self._lock:
            hooks = tuple(self._reconcile_hooks)
        for hook in hooks:
            try:
                hook(snap)
            except Exception:  # noqa: BLE001 - 单钩子失败不阻断其余
                pass
        return True

    # ── 准入编排（批2 顾问模式）────────────────────────────────

    def request(
        self,
        feature: str,
        need_gb: float = 0.0,
        *,
        device: int = 0,
        note: str = "",
        advisory: bool = True,
        allow_yield: bool = True,
    ) -> RequestResult:
        """准入判定。

        批2 唯一启用面=顾问模式（advisory=True）：只判定 + 登记
        busy + 日志（[gpu-budget] 帧），**不执行让位、不拦截、不记
        预留**——现有行为零变化，帧日志供实弹观察与批3 硬闸翻闸
        依据（D3=A）。非顾问模式（advisory=False）为批3 预留：
        GRANTED 时额外记逻辑预留（release 归还）。

        判定：budget_gb（空闲−预留）≥ need_gb → GRANTED；不足时
        allow_yield=True → WAIT（附建议让位阶梯，仅提示）；False →
        DENY。读数失明（available=False）按 GRANTED 放行——账本
        失明不阻断业务（等价现状行为）。
        """
        snap = self.snapshot(device)
        if not snap.available:
            token = get_busy_registry().register(feature, "local", note)
            result = RequestResult(
                verdict=Verdict.GRANTED, feature=feature, need_gb=float(need_gb),
                device=device, advisory=advisory, snapshot=snap,
                reason="账本读数失明，按现状行为放行", busy_token=token)
            log.info(
                "[gpu-budget] %s verdict=granted(盲) note=%s "
                "（读数失明不阻断，等价现状）", feature, note)
            return result
        if need_gb <= snap.budget_gb:
            token = get_busy_registry().register(feature, "local", note)
            if not advisory:
                self.reserve(device, need_gb)
            result = RequestResult(
                verdict=Verdict.GRANTED, feature=feature, need_gb=float(need_gb),
                device=device, advisory=advisory, snapshot=snap,
                reason=(f"额度足够（可用 {snap.budget_gb:.1f}GB ≥ "
                        f"需求 {need_gb:.1f}GB）"), busy_token=token)
            log.info(
                "[gpu-budget] %s verdict=granted need=%.1fG free=%.1fG "
                "budget=%.1fG ledger=%.1fG external=%.1fG note=%s",
                feature, need_gb, snap.free_gb, snap.budget_gb,
                snap.ledger_gb, snap.external_gb, note)
            return result
        deficit = round(float(need_gb) - snap.budget_gb, 2)
        if allow_yield:
            reason = (f"额度不足（可用 {snap.budget_gb:.1f}GB < 需求 "
                      f"{need_gb:.1f}GB，差 {deficit:.1f}GB）；建议让位："
                      + self._suggest_ladder(snap))
            verdict = Verdict.WAIT
        else:
            reason = (f"额度不足（差 {deficit:.1f}GB）且无可让位项；"
                      "出路：等待在途任务结束 / 切云端槽位 / 选更小档位")
            verdict = Verdict.DENY
        result = RequestResult(
            verdict=verdict, feature=feature, need_gb=float(need_gb),
            device=device, advisory=advisory, snapshot=snap, reason=reason)
        log.info(
            "[gpu-budget] %s verdict=%s need=%.1fG free=%.1fG "
            "budget=%.1fG external=%.1fG note=%s（%s）",
            feature, verdict.value, need_gb, snap.free_gb, snap.budget_gb,
            snap.external_gb, note, reason)
        return result

    def release(
        self,
        result: RequestResult | None = None,
        *,
        token: str = "",
        feature: str = "",
        note: str = "",
    ) -> None:
        """归还：注销 busy +（非顾问）释放逻辑预留 + 结束帧日志。

        result 与 token 二选一传入（token 优先）；两者皆空为 no-op
        （对未走 request 的任务安全）。feature 供仅持 token 的调用方
        （队列）标注帧日志归属（测试发现 F-2 修复，2026-09-10）。
        """
        _token = token or (result.busy_token if result else "")
        if _token:
            get_busy_registry().unregister(_token)
        if result is not None and not result.advisory:
            self.release_reservation(result.device, result.need_gb)
        device = result.device if result is not None else 0
        _feature = (result.feature if result is not None else "") or feature
        snap = self.snapshot(device)
        log.info(
            "[gpu-budget] release %s 收尾帧: free=%.1fG budget=%.1fG "
            "external=%.1fG note=%s",
            _feature or "?", snap.free_gb, snap.budget_gb,
            snap.external_gb, note)

    def _suggest_ladder(self, snap: BudgetSnapshot) -> str:
        """按当前帧构造建议让位阶梯（顾问模式仅提示不执行）。

        L0 对账（账外占用超线先收孤儿）→ L1 台账空闲项回收 →
        L2 vLLM 让渡（Windows 语义=停子进程）。成本从零到高排列，
        与方案 §3.3 阶梯一致；执行能力批3 接入。
        """
        parts: list[str] = []
        if snap.external_anomaly_gb >= EXTERNAL_ANOMALY_GB:
            parts.append(f"L0 先对账（账外 {snap.external_gb:.1f}GB 疑孤儿）")
        if snap.ledger_gb > 0:
            parts.append(f"L1 台账在载 {snap.ledger_gb:.1f}GB（空闲项可回收）")
        try:  # 延迟 import 防循环（vllm_service 反向依赖本模块）
            from ...engines.vllm_service import get_vllm_service

            if get_vllm_service().is_running():
                parts.append("L2 vLLM 在跑（可让渡 ~12GB，冷启成本 ~157s）")
        except Exception:  # noqa: BLE001 - 探测失败不列该项
            pass
        return "；".join(parts) or "无低代价让位项（须等待或降档）"


@dataclass(frozen=True)
class BusyEntry:
    """忙碌登记条目（本地重型任务 / 云端任务统一格式）。"""

    feature: str
    """功能域：dialog / paint / video_gen / training / …"""

    kind: str
    """\"local\"（占本地 GPU）| \"cloud\"（不占本地 GPU，但系统非空闲）。"""

    since: float
    """time.monotonic() 登记时刻。"""

    note: str
    """人话备注（如任务 id/来源），仅展示用。"""


class BusyRegistry:
    """忙碌登记簿雏形（方案 §3.6）。

    云端任务不取本地功能锁（现状保留，云端API接入方案铁律），但
    「系统空闲」判定（空闲回收/对话空闲看门狗/预加载）必须看见它
    ——本登记簿即那个「看见」的通道。批2 起本地队列任务经
    request/release 登记；批5 接云提交点与空闲消费方。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._entries: dict[str, BusyEntry] = {}

    def register(
        self, feature: str, kind: str = "local", note: str = ""
    ) -> str:
        """登记一条忙碌 → 返回注销令牌（token）。"""
        with self._lock:
            self._seq += 1
            token = f"{feature}#{self._seq}"
            self._entries[token] = BusyEntry(
                feature=feature,
                kind=kind,
                since=time.monotonic(),
                note=note,
            )
            return token

    def unregister(self, token: str) -> bool:
        """按令牌注销；令牌不存在返回 False（幂等安全）。"""
        with self._lock:
            return self._entries.pop(token, None) is not None

    def clear(self) -> None:
        """清空全部登记（测试/运维复位用）。"""
        with self._lock:
            self._entries.clear()

    def entries(self) -> tuple[BusyEntry, ...]:
        """当前全部登记（快照元组）。"""
        with self._lock:
            return tuple(self._entries.values())

    def is_busy(
        self, feature: str | None = None, kind: str | None = None
    ) -> bool:
        """是否有匹配登记（None = 该维度不限）。

        例：is_busy(kind="cloud") —— 云任务在跑则空闲判定必须让位；
        is_busy("paint") —— paint 域任意本地/云任务在跑。
        """
        with self._lock:
            for entry in self._entries.values():
                if feature is not None and entry.feature != feature:
                    continue
                if kind is not None and entry.kind != kind:
                    continue
                return True
            return False


def heavy_generation_idle() -> bool:
    """重型生成域是否全空闲（去抖唤醒的默认判定，批3）。

    判据：无 paint/video_gen/training 功能锁持有，且忙碌登记簿无
    本地 paint/video_gen 任务（队列任务批2 起登记）。keyframe 等
    无队列内部状态的编排点用它做 is_idle。
    """
    try:
        from ...middleware.feature_lock import get_feature_lock

        if get_feature_lock().active_feature in ("paint", "video_gen",
                                                 "training"):
            return False
    except Exception:  # noqa: BLE001 - 锁查询失败交由登记簿判定
        pass
    registry = get_busy_registry()
    return not (registry.is_busy("paint") or registry.is_busy("video_gen"))


class VramYieldCoordinator:
    """vLLM 生成期让渡/唤醒的单一编排者（显存调度机制批3，2026-09-10）。

    历史（方案 §1.2 病灶③）：4 个编排点（image_queue / video_queue /
    keyframe 一致性重抽 / common 共享层）各自 sleep/wake 同一个 vLLM
    子进程——video_queue 排空即唤醒无去抖（image_queue 有 V9-β 10s
    去抖），唤醒重启会被紧邻下一任务的功能锁门禁拒绝且无人重试
    （2026-09-08 15:37:04 实测撞锁）。

    本协调器收编「何时唤醒」的判定（去抖 + 空闲 + 跨 source 只认
    最新排空）；「怎么唤醒」可注入（两条队列注入自身方法保持 V9-β
    哨兵测试可 mock 性，keyframe 等用默认 wake_now 直调 vLLM）。
    best-effort 铁律不变：一切失败只记日志，绝不阻断生成任务。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_drain: dict[str, float] = {}
        self._timer_seq = 0

    def sleep_for_generation(self, source: str) -> None:
        """生成期让渡：vLLM 停子进程让显存（幂等 best-effort）。"""
        try:
            from ...engines.vllm_service import get_vllm_service

            get_vllm_service().sleep_for_paint()
        except Exception as exc:  # noqa: BLE001 - 让渡失败不阻断生成
            log.warning("vLLM 让渡协商失败（source=%s，不阻断生成）: %s",
                        source, exc)

    def wake_now(self, source: str = "") -> None:
        """立即唤醒 vLLM（幂等 best-effort；引擎侧已就绪时快速返回）。"""
        try:
            from ...engines.vllm_service import get_vllm_service

            get_vllm_service().wake_from_paint()
        except Exception as exc:  # noqa: BLE001 - 唤醒失败不影响生成结果
            log.warning("vLLM 唤醒协商失败（source=%s）: %s", source, exc)

    def schedule_wake_if_idle(
        self,
        source: str,
        is_idle: Callable[[], bool],
        *,
        debounce_s: float | None = None,
        wake: Callable[[], None] | None = None,
    ) -> None:
        """排空后去抖唤醒 vLLM（V9-β 2026-09-09 泛化版，批3 单源）。

        语义（与 image_queue 实测版逐条对齐，并泛化到跨 source）：
          - 每次排空记录 source 时间戳并起一个 debounce 定时器；
          - 到点时本 source 仍是**全队最新**排空（无新排空覆盖）且
            is_idle()（无在跑/排队本地任务）才真唤醒；
          - 窗口内出现新排空/新任务 → 本次跳过（那次排空自会再排）。

        Args:
            source: 编排点标识（image_queue / video_queue / keyframe:xx）
            is_idle: 到点时的空闲判定（队列内部状态注入）
            debounce_s: 去抖窗（默认 vram_policy.WAKE_DEBOUNCE_S）
            wake: 唤醒动作注入（None=协调器 wake_now 直调 vLLM；
                队列注入自身方法保持 V9-β 哨兵可 mock 性）
        """
        if debounce_s is None:
            from ..vram_policy import WAKE_DEBOUNCE_S

            debounce_s = WAKE_DEBOUNCE_S
        my_ts = time.monotonic()  # 本次排空时刻快照（定时器让位判据）
        with self._lock:
            self._last_drain[source] = my_ts
            self._timer_seq += 1
            seq = self._timer_seq

        def _fire() -> None:
            try:
                time.sleep(debounce_s)  # type: ignore[arg-type]
                with self._lock:
                    latest = max(self._last_drain.values(), default=my_ts)
                if my_ts < latest:
                    return  # 窗口内有更新排空（同/跨 source），由那次负责
                if not is_idle():
                    log.info("vLLM 去抖唤醒窗口内非空闲（source=%s），"
                             "跳过本次唤醒（下次排空再排）", source)
                    return
                log.info("生成队列排空 %.0fs 无新任务，唤醒 vLLM"
                         "（批3 去抖单源，source=%s）", debounce_s, source)
                if wake is not None:
                    wake()
                else:
                    self.wake_now(source)
            except Exception as exc:  # noqa: BLE001 - 定时器永不抛出
                log.warning("vLLM 去抖唤醒定时器异常: %s", exc)

        threading.Thread(
            target=_fire, daemon=True,
            name=f"vram-wake-debounce-{seq}").start()


# ── 单例 ────────────────────────────────────────────────────────
_gpu_budget: GpuBudget | None = None
_busy_registry: BusyRegistry | None = None
_yield_coordinator: VramYieldCoordinator | None = None
_singleton_lock = threading.Lock()


def get_gpu_budget() -> GpuBudget:
    """账本单例（双重检查锁）。"""
    global _gpu_budget
    if _gpu_budget is None:
        with _singleton_lock:
            if _gpu_budget is None:
                _gpu_budget = GpuBudget()
    return _gpu_budget


def get_busy_registry() -> BusyRegistry:
    """登记簿单例（双重检查锁）。"""
    global _busy_registry
    if _busy_registry is None:
        with _singleton_lock:
            if _busy_registry is None:
                _busy_registry = BusyRegistry()
    return _busy_registry


def get_yield_coordinator() -> VramYieldCoordinator:
    """让渡协调器单例（双重检查锁）。"""
    global _yield_coordinator
    if _yield_coordinator is None:
        with _singleton_lock:
            if _yield_coordinator is None:
                _yield_coordinator = VramYieldCoordinator()
    return _yield_coordinator
