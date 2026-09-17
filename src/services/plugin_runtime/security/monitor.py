"""SecurityMonitor 组合监控面（DF v0.9.2 安全盾组合入口，2026-09-17 移植）。

把意图探针、高危熔断、行为指纹与全链路审计组合成**执行前单一检查点**：
调用方在下发动作前调用 gate()；gate() 同时进行实时行为异常判定并将
全链路四元组写入审计存储。

与 DF 原版的差异（接线侧改造，判定逻辑零改）：DF 原版"骨架未接线"；
OmniSpace 侧由 security_gate.py（宿主适配层）把 gate() 接进
PluginRuntime.invoke 与内核 think 两条调用链。

返回 (放行与否, 审计记录, 被熔断挂起的审核单或 None)。
"""
from __future__ import annotations

from typing import Any

from .action_tracer import ActionTracer
from .behavioral_fingerprint import BehavioralFingerprint, FingerprintResult
from .circuit_breaker import CriticalActionCircuitBreaker, PendingReview
from .intent_probe import AuditedAction, IntentProbe
from .safety_check import RuleBasedSafetyCheck, SafetyCheck, SafetyVerdict, Verdict


class SecurityMonitor:
    """组合监控面：gate() 是执行层前唯一的必经检查点。

    实时判定结果存于 last_fingerprint，全链路审计写入 tracer。
    """

    def __init__(self, checker: SafetyCheck | None = None,
                 probe: IntentProbe | None = None,
                 breaker: CriticalActionCircuitBreaker | None = None,
                 fingerprint: BehavioralFingerprint | None = None,
                 tracer: ActionTracer | None = None) -> None:
        self.probe = probe or IntentProbe(checker=checker or RuleBasedSafetyCheck())
        self.breaker = breaker or CriticalActionCircuitBreaker()
        self.fingerprint = fingerprint or BehavioralFingerprint()
        self.tracer = tracer or ActionTracer()
        self.last_fingerprint: FingerprintResult | None = None

    def gate(self, action: dict[str, Any],
             thinking_state: dict[str, Any] | None = None,
             task: str | None = None) -> tuple[bool, AuditedAction,
                                               PendingReview | None]:
        """执行前门禁：ALLOW 放行 / REVIEW 熔断挂起 / DENY 拒绝。

        副作用：实时行为指纹判定；ALLOW 动作回填频率基线；
        每条动作写入全链路审计（四元组绑定）。
        """
        audited = self.probe.inspect(action)
        v = audited.verdict.verdict

        # 行为指纹：并列判定，与意图规则回执一并留痕（不下发前强拦截）
        fp = self.fingerprint.check(action, task=task)
        self.last_fingerprint = fp

        if v is Verdict.ALLOW:
            self.fingerprint.observe(action)  # 正常业务回填基线
            ok, review, outcome = True, None, "ALLOW"
        elif v is Verdict.REVIEW:
            review = self.breaker.trip(action, audited.verdict)
            ok, outcome = False, "REVIEW"
        else:
            review, outcome = None, "DENY"
            ok = False

        self.tracer.record(
            thinking_state=thinking_state,
            decision_basis=_basis_of(audited, fp),
            tool_params=action,
            execution_result={"allowed": ok, "outcome": outcome,
                              "fingerprint_flags": fp.flags},
            tags=list(fp.flags),
        )
        return ok, audited, review

    def stats(self) -> dict[str, Any]:
        return {"probe": self.probe.stats(),
                "breaker": self.breaker.stats(),
                "fingerprint": self.fingerprint.stats(),
                "tracer": self.tracer.stats()}


def _basis_of(audited: AuditedAction,
              fp: FingerprintResult | None) -> dict[str, Any]:
    """把意图判定结论与行为指纹结果合并为审计的决策依据。"""
    verdict: SafetyVerdict = audited.verdict
    return {
        "verdict": verdict.verdict.value,
        "reasons": verdict.reasons,
        "risk_tags": verdict.risk_tags,
        "fingerprint_flags": fp.flags if fp else [],
        "fingerprint_metrics": fp.metrics if fp else {},
    }
