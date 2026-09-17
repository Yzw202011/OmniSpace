"""插件运行时安全盾（DistributedFormer v0.9.2 SecurityMonitor 移植，2026-09-17 用户拍板）。

四件套 + 组合面，补 sandbox.py（物理隔离：超时杀/内存杀/崩溃隔离）之上
缺失的**行为层**：插件进门后干了什么、干得正不正常、有没有越权。

- safety_check.py          判定接口 + 规则版（ALLOW/REVIEW/DENY 三态）
- intent_probe.py          意图探针（判定 + 历史环形留存）
- circuit_breaker.py       高危熔断（REVIEW 挂起；fail-closed）
- behavioral_fingerprint.py 行为指纹（频率突发/参数熵/无关底层指令）
- action_tracer.py         全链路审计（四元组绑定 + 多维回溯）
- monitor.py               组合面：gate() = 执行前唯一必经检查点

宿主接线在 plugin_runtime/security_gate.py（单例 + config 总闸 +
两条调用链的接入），本包保持与上游同构便于后续同步。
初始规则集与 OmniSpace 现有插件名/路由主题零交集 = 默认全放行。
"""
from __future__ import annotations

from .action_tracer import ActionTrace, ActionTracer
from .behavioral_fingerprint import BehavioralFingerprint, FingerprintResult
from .circuit_breaker import CriticalActionCircuitBreaker, PendingReview
from .intent_probe import AuditedAction, IntentProbe
from .monitor import SecurityMonitor
from .safety_check import (
    RuleBasedSafetyCheck,
    SafetyCheck,
    SafetyVerdict,
    Verdict,
)

__all__ = [
    "ActionTrace", "ActionTracer",
    "AuditedAction", "BehavioralFingerprint", "CriticalActionCircuitBreaker",
    "FingerprintResult", "IntentProbe", "PendingReview",
    "RuleBasedSafetyCheck", "SafetyCheck", "SafetyVerdict",
    "SecurityMonitor", "Verdict",
]
