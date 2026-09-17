"""意图与决策探针（DF v0.9.2 安全盾四件套之二，2026-09-17 移植）。

在动作下发执行之前实时扫描候选动作，借 SafetyCheck 判断是否包含
越权、获取高权限指令或探索系统边界等逃逸倾向，命中即拦截。
判定历史环形留存（供审计溯源与后续基线检测）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .safety_check import (
    RuleBasedSafetyCheck,
    SafetyCheck,
    SafetyVerdict,
    Verdict,
)


@dataclass
class AuditedAction:
    """一条经过审计的动作：原始动作 + 判定结论 + 时间戳（审计四元组的决策依据部分）。"""

    action: dict[str, Any]
    verdict: SafetyVerdict
    t: float = field(default_factory=time.time)


class IntentProbe:
    """意图探针：动作下发前的审计面（与 DF 原版语义一致）。

    用法：
        probe = IntentProbe()
        audited = probe.inspect({"tool": "db_write", ...})
        if audited.verdict.verdict is Verdict.DENY:
            raise RuntimeError(f"意图审计拦截: {audited.verdict.reasons}")
    """

    def __init__(self, checker: SafetyCheck | None = None,
                 history_capacity: int = 256) -> None:
        self.checker = checker or RuleBasedSafetyCheck()
        self.history_capacity = history_capacity
        self.history: list[AuditedAction] = []
        self.total_inspected = 0
        self.total_denied = 0

    def inspect(self, action: dict[str, Any]) -> AuditedAction:
        """审计一条候选动作：判定 → 记入历史（供审计溯源/基线检测）。"""
        verdict = self.checker.check(action)
        audited = AuditedAction(action=action, verdict=verdict)
        self.history.append(audited)
        if len(self.history) > self.history_capacity:
            self.history.pop(0)
        self.total_inspected += 1
        if verdict.verdict is Verdict.DENY:
            self.total_denied += 1
        return audited

    def stats(self) -> dict[str, Any]:
        return {"inspected": self.total_inspected,
                "denied": self.total_denied,
                "history": len(self.history)}
