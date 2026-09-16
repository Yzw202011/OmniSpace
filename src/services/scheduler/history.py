"""OmniSpace AI v2.3.1 协同调度历史学习引擎（文档B 第四章引擎清单 3）。

文档B 原文：「协同调度历史学习引擎 — 记录每次调度决策与效果，
自适应调整预加载阈值」（另见 §8.3.4 模型管理器实现细节同名条目）。

职责：
  1. 决策落库：每次协同模式切换（含紧急直通）写入 schedule_history 表
     （建表走 src.data.database.get_db() 初始化路径，IF NOT EXISTS，
     首启自动建库；时间列按文档D 数据库规范存 TEXT ISO 8601）。
  2. 效果分析：近 N 次切换成功率 / 平均滞回等待 / 触发源分布，
     并给出确定性的阈值自调整建议（规则见 analyze() docstring）。

约束：数据库不可用时降级为内存环形缓冲（容量 500），功能不中断；
所有写库异常仅记日志不上抛（调度循环不能被学习引擎拖垮）。
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("omnispace.scheduler.history")

# 内存降级环形缓冲容量（数据库不可用时）
_MEM_BUFFER_CAP = 500


def _utc_now_iso() -> str:
    """ISO 8601 UTC 时间戳（文档D：如 2026-08-07T00:45:00Z）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ScheduleHistory:
    """协同调度历史学习引擎——决策落库 + 效果回归分析。"""

    def __init__(self) -> None:
        self._mem: deque[dict] = deque(maxlen=_MEM_BUFFER_CAP)
        self._db_failed = False  # 写库失败一次后转内存模式，避免逐 tick 重试

    # ── 记录 ────────────────────────────────────────────────────

    def record(
        self,
        mode_before: str,
        mode_after: str,
        trigger: str,
        vram_free_mb: int = 0,
        wait_ms: int = 0,
        success: bool = True,
    ) -> None:
        """记录一次调度决策（模式切换）。

        Args:
            mode_before:  切换前协同模式（SynergyMode.value）
            mode_after:   切换后协同模式
            trigger:      触发源（hysteresis 滞回确认 / emergency 紧急直通）
            vram_free_mb: 决策时刻空闲显存（MB）
            wait_ms:      候选模式滞回等待时长（毫秒）
            success:      策略执行体是否无异常完成
        """
        row = {
            "ts": _utc_now_iso(),
            "mode_before": mode_before,
            "mode_after": mode_after,
            "trigger": trigger,
            "vram_free_mb": int(vram_free_mb),
            "wait_ms": int(wait_ms),
            "success": 1 if success else 0,
        }
        if self._db_failed:
            self._mem.append(row)
            return
        try:
            from ...data.database import get_db
            get_db().insert("schedule_history", row)
        except Exception as exc:  # noqa: BLE001 - 学习引擎不得影响调度主链
            log.warning("调度历史落库失败，降级内存缓冲: %s", exc)
            self._db_failed = True
            self._mem.append(row)

    # ── 查询 ────────────────────────────────────────────────────

    def recent(self, limit: int = 100) -> list[dict]:
        """读取最近 N 条调度历史（新→旧）。"""
        limit = max(1, min(int(limit), 1000))
        if not self._db_failed:
            try:
                from ...data.database import get_db
                rows = get_db().query(
                    "SELECT ts, mode_before, mode_after, trigger,"
                    " vram_free_mb, wait_ms, success"
                    " FROM schedule_history ORDER BY id DESC LIMIT ?",
                    (limit,))
                if rows or not self._mem:
                    return rows
            except Exception as exc:  # noqa: BLE001
                log.warning("调度历史读取失败，使用内存缓冲: %s", exc)
                self._db_failed = True
        return list(self._mem)[-limit:][::-1]

    # ── 分析（学习）─────────────────────────────────────────────

    def analyze(self, limit: int = 100) -> dict[str, Any]:
        """近 N 次调度效果回归 + 阈值自调整建议。

        返回:
            total:            样本数
            success_rate:     策略执行成功率（0~1）
            avg_wait_ms:      平均滞回等待（毫秒）
            transition_stats: 各 mode_after 出现次数
            suggestions:      阈值自调整建议（中文字符串列表，确定性规则）

        自调整规则（文档B 第四章引擎3「自适应调整预加载阈值」的确定性实现）：
          R1 样本 <10 → 不调整（统计意义不足）；
          R2 成功率 <90% → 调度执行体不稳定，建议滞回时长 +50% 并排查
             force_unload/preload 失败日志；
          R3 ALL_TENSE 占比 >20% → 资源长期紧张，建议提前干预
             （显存预警线 -5pt，如 0.90→0.85）；
          R4 全部样本成功且从未出现紧张模式 → 资源充裕，可放宽预警线
             +2pt 以减少误切换；
          R5 平均滞回等待 >60s → 模式振荡迹象，建议检查阈值间距。
        """
        rows = self.recent(limit)
        total = len(rows)
        if total == 0:
            return {
                "total": 0, "success_rate": None, "avg_wait_ms": None,
                "transition_stats": {}, "suggestions": ["暂无调度历史样本"],
            }

        success_n = sum(1 for r in rows if r.get("success"))
        success_rate = round(success_n / total, 4)
        avg_wait = round(sum(int(r.get("wait_ms", 0) or 0) for r in rows) / total)
        stats: dict[str, int] = {}
        for r in rows:
            key = str(r.get("mode_after", "") or "unknown")
            stats[key] = stats.get(key, 0) + 1

        suggestions: list[str] = []
        tense_ratio = stats.get("all_tense", 0) / total
        if total < 10:
            suggestions.append("样本不足 10 次，暂不调整阈值")
        else:
            if success_rate < 0.9:
                suggestions.append(
                    "调度成功率 %.0f%% 低于 90%%，建议将滞回时长提高 50%% "
                    "并排查模型卸载/预加载失败日志" % (success_rate * 100))
            if tense_ratio > 0.2:
                suggestions.append(
                    "ALL_TENSE 占比 %.0f%% 超过 20%%，资源长期紧张，"
                    "建议显存预警阈值下调 5 个百分点以提前干预" % (tense_ratio * 100))
            if success_rate >= 0.99 and tense_ratio == 0 and "cpu_assist" not in stats:
                suggestions.append(
                    "全部样本成功且未出现资源紧张，资源充裕，"
                    "预警阈值可上浮 2 个百分点以减少误切换")
            if avg_wait > 60000:
                suggestions.append(
                    "平均滞回等待 %.1fs 超过 60s，存在模式振荡迹象，"
                    "建议加大预警/临界阈值间距" % (avg_wait / 1000.0))
        if not suggestions:
            suggestions.append("调度表现正常，维持当前阈值")

        return {
            "total": total,
            "success_rate": success_rate,
            "avg_wait_ms": avg_wait,
            "transition_stats": stats,
            "suggestions": suggestions,
            "storage": "memory" if self._db_failed else "sqlite",
        }


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_history_instance: ScheduleHistory | None = None
_history_lock = threading.Lock()


def get_schedule_history() -> ScheduleHistory:
    """获取调度历史学习引擎全局单例。"""
    global _history_instance
    if _history_instance is None:
        with _history_lock:
            if _history_instance is None:
                _history_instance = ScheduleHistory()
    return _history_instance
# 本项目仅供学习使用，商业授权请+Q 3559331368
