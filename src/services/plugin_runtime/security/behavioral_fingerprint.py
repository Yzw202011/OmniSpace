"""行为指纹与异常基线检测（DF v0.9.2 安全盾四件套之四，2026-09-17 移植）。

记录正常业务下的标准行为模式建立"安全基线"；实时监控调用频率、
参数熵值及请求链路的逻辑关联性，对以下逃逸行为立即标记异常：

- 大规模并发试探 / 频率突发（frequency-burst）
- 非逻辑的端口扫描式调用（载荷熵值异乎寻常之高， high-entropy-params）
- 与当前任务无关的底层系统指令（unrelated-low-level）

基线通过 learn() 在正常业务流量上累积；check() 在动作下发前基于
当前实时窗口内的指标做偏差判定。**指纹异常仅留痕告警，不硬拦截**
（与 DF 原版一致：硬拦截只来自意图判定 DENY/REVIEW）。
"""
from __future__ import annotations

import math
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# 底层系统指令集合（与业务工具无关时即视为逃逸倾向）
_LOW_LEVEL_TOOLS: tuple[str, ...] = (
    "chmod", "chown", "kill", "mount", "umount", "iptables", "ifconfig",
    "ptrace", "reboot", "shutdown", "mknod", "setcap",
)

# 参与熵值计算的载荷字段名（逐个尝试，优先可变的参数部分）
_PARAMS_KEYS: tuple[str, ...] = ("params", "parameters", "args")


@dataclass
class FingerprintResult:
    """一次行为指纹判定的结论。

    - anomalous: 是否判定为异常行为
    - flags: 命中的异常模式标签
    - metrics: 本次实测指标（供审计留痕与调参）
    """

    anomalous: bool
    flags: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


class BehavioralFingerprint:
    """行为指纹探针：基线学习 + 实时偏差判定（纯内存，GIL 下计数安全）。"""

    def __init__(self,
                 window: float = 10.0,
                 freq_limit: int | None = 6,
                 entropy_threshold: float = 4.0,
                 deviation_factor: float = 3.0,
                 low_level_tools: tuple[str, ...] | None = None,
                 task_tools: dict[str, set[str]] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        """- window: 频率统计的时间窗（秒），也是基线速率的归一化单位
        - freq_limit: 单一工具在窗内的硬上限；None 表示仅依赖基线偏离
        - entropy_threshold: 载荷熵值超限判定为枚举/扫描式调用
        - deviation_factor: 实测频率超过基线速率的倍数即判定偏离
        - task_tools: 任务名 → 允许工具集合（供链路逻辑关联判定）
        """
        self.window = window
        self.freq_limit = freq_limit
        self.entropy_threshold = entropy_threshold
        self.deviation_factor = deviation_factor
        self.low_level_tools = set(low_level_tools or _LOW_LEVEL_TOOLS)
        self.task_tools = task_tools or {}
        self._clock = clock or time.monotonic

        # 实时滑动窗口内的全部调用 (t, tool)，供频率突发判定
        self._recent: deque[tuple[float, str]] = deque()
        # 基线: tool → 累计观察次数（速率 = 次数 / window，即每窗期望次数）
        self._baseline_count: Counter[str] = Counter()
        # 统计
        self.total_checks = 0
        self.total_anomalies = 0

    # ── 工具 ──────────────────────────────────────────────────
    @staticmethod
    def _tool_of(action: dict[str, Any]) -> str:
        return str(action.get("tool", action.get("topic", "")))

    def _params_of(self, action: dict[str, Any]) -> str:
        """抽取参与熵值计算的载荷字符串（优先可变的参数部分）。"""
        for k in _PARAMS_KEYS:
            if k in action and action[k]:
                return str(action[k])
        return self._tool_of(action)

    @staticmethod
    def _entropy(text: str) -> float:
        """Shannon 熵（bit/字符）；随机高熵载荷常显著高于自然业务文本。"""
        if not text:
            return 0.0
        freq = Counter(text)
        n = len(text)
        return -sum((c / n) * math.log2(c / n) for c in freq.values())

    def _prune(self, now: float) -> None:
        self._recent = deque(
            (t, x) for t, x in self._recent if now - t <= self.window)

    def _window_count(self, tool: str, now: float) -> int:
        self._prune(now)
        return sum(1 for t, x in self._recent
                   if x == tool and now - t <= self.window)

    def _baseline_rate(self, tool: str) -> float:
        """每时间窗内该工具的期望调用次数。"""
        return self._baseline_count.get(tool, 0) / self.window

    # ── 基线累积 ───────────────────────────────────────────────
    def observe(self, action: dict[str, Any],
                at: float | None = None) -> None:
        """实时窗口数据源：记录一次被放行调用的发生时刻（不改变基线）。"""
        now = at if at is not None else self._clock()
        tool = self._tool_of(action)
        if not tool:
            return
        self._recent.append((now, tool))
        self._prune(now)

    def learn(self, actions: list[dict[str, Any]]) -> None:
        """校准阶段建立基线：按观察次数累计每工具速率，不进入实时窗口。

        速率语义 = 观察次数 / window（即"每个时间窗内的期望调用次数"）；
        实时 check 用 deviation_factor 与之比较，偏离即判异常。
        """
        for action in actions:
            tool = self._tool_of(action)
            if tool:
                self._baseline_count[tool] += 1
        # 校准结束清空实时窗口，使基线速率与后续实时流量解耦
        self._recent.clear()

    # ── 实时判定 ───────────────────────────────────────────────
    def check(self, action: dict[str, Any],
              task: str | None = None,
              at: float | None = None) -> FingerprintResult:
        """判定给定动作相对当前基线是否异常（下发前调用）。"""
        now = at if at is not None else self._clock()
        tool = self._tool_of(action)
        flags: list[str] = []
        metrics: dict[str, float] = {}

        # 1) 频率突发 / 基线偏离：窗内实测次数 > 硬上限或偏差倍数之上限
        cnt = self._window_count(tool, now)
        baseline = self._baseline_rate(tool)
        metrics["window_count"] = float(cnt)
        metrics["baseline_rate"] = baseline
        limit: float | None = float(self.freq_limit) if self.freq_limit else None
        if baseline:
            limit = max(limit or 0.0, baseline * self.deviation_factor)
        metrics["freq_limit"] = limit if limit else 0.0
        if limit and cnt > limit:
            flags.append("frequency-burst")

        # 2) 参数熵值异常（端口扫描式枚举调用：高随机性载荷）
        entropy = self._entropy(self._params_of(action))
        metrics["entropy"] = entropy
        if entropy > self.entropy_threshold:
            flags.append("high-entropy-params")

        # 3) 链路逻辑关联：底层系统指令与当前任务上下文脱节
        unrelated = False
        if task and task in self.task_tools:
            allowed = self.task_tools[task]
            if tool not in allowed and tool in self.low_level_tools:
                unrelated = True
        elif not task and tool in self.low_level_tools:
            unrelated = True  # 无任务上下文时保守告警
        if unrelated:
            flags.append("unrelated-low-level")

        self.total_checks += 1
        anomalous = bool(flags)
        if anomalous:
            self.total_anomalies += 1
        return FingerprintResult(anomalous, flags, metrics)

    def reset(self) -> None:
        """清空窗口与基线，重建干净状态。"""
        self._recent.clear()
        self._baseline_count.clear()
        self.total_checks = 0
        self.total_anomalies = 0

    def stats(self) -> dict[str, Any]:
        return {"checks": self.total_checks,
                "anomalies": self.total_anomalies,
                "baseline_tools": len(self._baseline_count),
                "window_size": len(self._recent)}
