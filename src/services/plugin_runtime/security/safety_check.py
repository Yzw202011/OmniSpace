"""安全判定接口与规则版实现（DistributedFormer v0.9.2 安全盾移植，2026-09-17 用户拍板）。

单条候选动作的安全判定接口——四件套（意图探针/熔断/指纹/审计）共用的
判定基座。原版源 = DF src/security_monitor/safety_check.py（547 行四件套
之一），移植仅做类型现代化（py310 内建泛型），判定逻辑零改。

三态判定（Verdict）：
    ALLOW  放行：正常业务动作
    REVIEW 熔断：高危操作，挂起等审核后再决定
    DENY   拦截：命中逃逸倾向，下发前直接拒绝
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(Enum):
    """安全判定结果。"""

    ALLOW = "allow"
    REVIEW = "review"
    DENY = "deny"


@dataclass
class SafetyVerdict:
    """一次安全判定的完整结论。

    - verdict: 判定结果（ALLOW / REVIEW / DENY）
    - reasons: 命中的规则/探针说明（审计留痕用）
    - risk_tags: 风险标签，如 privilege-escalation / boundary-probing
    """

    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    risk_tags: list[str] = field(default_factory=list)


class SafetyCheck(ABC):
    """候选动作安全判定接口（宿主可注入学习型实现替换规则版）。"""

    @abstractmethod
    def check(self, action: dict[str, Any]) -> SafetyVerdict:
        """对一条候选动作（工具调用参数）做安全判定。"""


# 高危操作模式（REVIEW 熔断的初始规则集——关键词子串匹配 tool 名；
# 初始集来自 DF，与 OmniSpace 现有插件名/路由主题零交集=默认全放行）
_HIGH_RISK_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("modify-system-config", ("write_config", "set_env", "edit_registry")),
    ("access-core-database", ("db_drop", "db_write", "raw_sql")),
    ("download-external-code", ("curl", "wget", "pip_install", "fetch_url")),
)

# 逃逸倾向模式（DENY 拦截的初始规则集）
_ESCAPE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("privilege-escalation", ("sudo", "run_as_admin", "escalate")),
    ("boundary-probing", ("port_scan", "enum_files", "list_secrets")),
)


class RuleBasedSafetyCheck(SafetyCheck):
    """规则版最小实现：关键词规则先行，学习型分类器留待后续。"""

    def __init__(self,
                 high_risk: dict[str, tuple[str, ...]] | None = None,
                 escape: dict[str, tuple[str, ...]] | None = None) -> None:
        self.high_risk = dict(high_risk or _HIGH_RISK_PATTERNS)
        self.escape = dict(escape or _ESCAPE_PATTERNS)

    def check(self, action: dict[str, Any]) -> SafetyVerdict:
        tool = str(action.get("tool", action.get("topic", "")))
        tags: list[str] = []

        for tag, verbs in self.escape.items():
            if any(v in tool for v in verbs):
                tags.append(tag)
        if tags:
            return SafetyVerdict(Verdict.DENY,
                                 reasons=[f"命中逃逸倾向: {tool}"],
                                 risk_tags=tags)

        for tag, verbs in self.high_risk.items():
            if any(v in tool for v in verbs):
                tags.append(tag)
        if tags:
            return SafetyVerdict(Verdict.REVIEW,
                                 reasons=[f"高危操作, 触发熔断: {tool}"],
                                 risk_tags=tags)

        return SafetyVerdict(Verdict.ALLOW)
