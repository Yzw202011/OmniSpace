"""高危调用熔断器（DF v0.9.2 安全盾四件套之三，2026-09-17 移植）。

对高风险操作不直接交由执行层执行：拦截该调用挂起等待审核
（可升级人工介入），确认合规前拒绝下发。无审核者时默认全部拒绝
（fail-closed，安全默认）。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .safety_check import SafetyVerdict


@dataclass
class PendingReview:
    """一次被熔断挂起的调用。"""

    action: dict[str, object]
    verdict: SafetyVerdict
    t: float = field(default_factory=time.time)
    resolved: bool = False
    approved: bool | None = None


class CriticalActionCircuitBreaker:
    """熔断器：REVIEW 判定 → 挂起等待审核；审核通过与否另行回填。

    未提供审核回调（reviewer）时 resolve() 一律拒绝（fail-closed）。
    """

    def __init__(self, reviewer: Callable[[PendingReview], bool] | None = None,
                 pending_capacity: int = 128) -> None:
        self.reviewer = reviewer
        self.pending_capacity = pending_capacity
        self.pending: list[PendingReview] = []
        self.total_tripped = 0
        self.total_rejected = 0

    def trip(self, action: dict[str, object],
             verdict: SafetyVerdict) -> PendingReview:
        """熔断一条高危调用：记录挂起并立即拒绝下发，审核结果另行处理。"""
        review = PendingReview(action=action, verdict=verdict)
        self.pending.append(review)
        if len(self.pending) > self.pending_capacity:
            self.pending.pop(0)
        self.total_tripped += 1
        return review

    def resolve(self, review: PendingReview) -> bool:
        """执行审核并回填结论：无审核者时 fail-closed（拒绝）。"""
        review.approved = bool(self.reviewer(review)) if self.reviewer else False
        review.resolved = True
        if not review.approved:
            self.total_rejected += 1
        return review.approved

    def stats(self) -> dict[str, object]:
        return {"tripped": self.total_tripped,
                "rejected": self.total_rejected,
                "pending": len(self.pending)}
