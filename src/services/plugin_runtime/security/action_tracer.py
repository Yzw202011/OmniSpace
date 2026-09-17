"""全链路行为审计与溯源（DF v0.9.2 安全盾四件套之五，2026-09-17 移植）。

对"产生想法 → 执行动作"全流程做结构化日志留存——思考状态、决策依据、
工具参数、执行结果**四元组绑定**存储；安全事件发生时支持完整复盘溯源。

四元组：
    思考状态 thinking_state   决策现场（调用方上下文/意图）
    决策依据 decision_basis   安全判定结论（规则理由/风险标签/指纹标记）
    工具参数 tool_params      即将下发的候选动作载荷
    执行结果 execution_result 放行/拦截后的最终结果（含审核结论）

record() 生成全局唯一 action_id 并绑定四元组；query() 支持多维回溯；
replay() 重建单个动作的完整链路供事后复盘。内存环形容量 512
（落盘持久化为后续演进项——产品侧可经事件桥接日志系统）。
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActionTrace:
    """一条完整调用链的结构化审计记录（四元组绑定）。"""

    action_id: str
    t: float
    thinking_state: dict[str, Any] = field(default_factory=dict)
    decision_basis: dict[str, Any] = field(default_factory=dict)
    tool_params: dict[str, Any] = field(default_factory=dict)
    execution_result: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


class ActionTracer:
    """全链路审计存储：绑定四元组 + 多维查询 + 溯源复盘。"""

    def __init__(self, capacity: int = 512,
                 id_factory: Callable[[], str] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        """- capacity: 内存环形容量，超出后淘汰最旧记录
        - id_factory/clock: 可注入以支持确定性测试
        """
        self.capacity = capacity
        self._id = id_factory or _new_id
        self._clock = clock or time.time
        self.traces: list[ActionTrace] = []

    def record(self,
               thinking_state: dict[str, Any] | None = None,
               decision_basis: dict[str, Any] | None = None,
               tool_params: dict[str, Any] | None = None,
               execution_result: dict[str, Any] | None = None,
               action_id: str | None = None,
               tags: Iterable[str] | None = None) -> str:
        """绑定四元组并存储，返回本次动作的 action_id（溯源主键）。"""
        trace = ActionTrace(
            action_id=action_id or self._id(),
            t=self._clock(),
            thinking_state=dict(thinking_state or {}),
            decision_basis=dict(decision_basis or {}),
            tool_params=dict(tool_params or {}),
            execution_result=dict(execution_result or {}),
            tags=list(tags or []),
        )
        self.traces.append(trace)
        if len(self.traces) > self.capacity:
            self.traces.pop(0)
        return trace.action_id

    # ── 回溯查询 ───────────────────────────────────────────────
    def query(self,
              action_id: str | None = None,
              tool: str | None = None,
              tag: str | None = None,
              outcome: str | None = None,
              risk_tag: str | None = None,
              since: float | None = None,
              until: float | None = None) -> list[ActionTrace]:
        """多维过滤回溯；任一维度均视为 AND 条件（None 表示不过滤）。"""
        result: list[ActionTrace] = []
        for tr in self.traces:
            if action_id is not None and tr.action_id != action_id:
                continue
            if tool is not None and tr.tool_params.get("tool") != tool:
                continue
            if tag is not None and tag not in tr.tags:
                continue
            if outcome is not None and tr.execution_result.get("outcome") != outcome:
                continue
            if risk_tag is not None and risk_tag not in \
                    tr.decision_basis.get("risk_tags", []):
                continue
            if since is not None and tr.t < since:
                continue
            if until is not None and tr.t > until:
                continue
            result.append(tr)
        return result

    def replay(self, action_id: str) -> dict[str, Any]:
        """溯源复盘：返回该动作的四元组完整画像（未找到则返回空字典）。"""
        for tr in self.traces:
            if tr.action_id == action_id:
                return {
                    "action_id": tr.action_id,
                    "t": tr.t,
                    "thinking_state": tr.thinking_state,
                    "decision_basis": tr.decision_basis,
                    "tool_params": tr.tool_params,
                    "execution_result": tr.execution_result,
                    "tags": tr.tags,
                }
        return {}

    def iter_traces(self) -> Iterator[ActionTrace]:
        """按时间顺序遍历全部审计记录（合规导出用）。"""
        return iter(self.traces)

    def export(self) -> list[dict[str, Any]]:
        """导出结构化的全量审计日志（逐条四元组字典）。"""
        return [{
            "action_id": tr.action_id,
            "t": tr.t,
            "thinking_state": tr.thinking_state,
            "decision_basis": tr.decision_basis,
            "tool_params": tr.tool_params,
            "execution_result": tr.execution_result,
            "tags": tr.tags,
        } for tr in self.traces]

    def stats(self) -> dict[str, Any]:
        return {"traces": len(self.traces), "capacity": self.capacity}


def _new_id() -> str:
    return uuid.uuid4().hex
