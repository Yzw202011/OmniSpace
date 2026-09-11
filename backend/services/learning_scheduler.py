"""OmniSpace AI v2.3 学习调度器（TASK-014）。

职责：
  - get_resource_quota(context)：文档配额矩阵的纯函数实现（便于单测）。
  - LearningScheduler：触发器注册（手动/定时/空闲>5分钟/项目驱动/对话缺口）、
    与 feature_lock 状态联动（创作活跃→暂停浏览器学习；空闲→恢复）、
    网络检测（socket 连 223.5.5.5:53 或 HTTP HEAD，3s 超时，失败即离线）。

资源配额矩阵（需求文档 TASK-014）：
  用户空闲+联网 : ≤5 标签, <20% CPU, <1536MB(1.5GB) 内存, <500KB/s
  用户创作+联网 : ≤2 标签, <10% CPU, <600MB 内存, <200KB/s
  AI 推理中     : 全 0（暂停）
  断网          : 0（关闭网络学习）
  电池模式      : 0（暂停）
  内存 <8GB     : ≤1 标签, <5% CPU, <300MB, <100KB/s
"""
from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

from .priority import Priority

log = logging.getLogger("omnispace.learning_scheduler")

# ═══════════════════════════════════════════════════════════════════
#  资源配额矩阵（纯函数）
# ═══════════════════════════════════════════════════════════════════

# 配额字段：max_tabs / max_cpu_percent / max_memory_mb / max_network_kbps
_QUOTA_IDLE = {"max_tabs": 5, "max_cpu_percent": 20,
               "max_memory_mb": 1536, "max_network_kbps": 500}
_QUOTA_CREATING = {"max_tabs": 2, "max_cpu_percent": 10,
                   "max_memory_mb": 600, "max_network_kbps": 200}
_QUOTA_LOW_MEMORY = {"max_tabs": 1, "max_cpu_percent": 5,
                     "max_memory_mb": 300, "max_network_kbps": 100}
_QUOTA_ZERO = {"max_tabs": 0, "max_cpu_percent": 0,
               "max_memory_mb": 0, "max_network_kbps": 0}


def get_resource_quota(context: dict) -> dict:
    """根据显式状态字典计算资源配额（纯函数，无副作用，便于单测）。

    context 键：
      user_state       "idle" | "creating"（用户空闲/创作中）
      online           bool（联网状态）
      ai_inferring     bool（AI 推理中）
      battery_mode     bool（电池模式）
      total_memory_gb  float（物理内存总量）
      tier_learn_tabs  int（可选，文档B §4.2 硬件等级学习标签配额，
                       提供时将 max_tabs 封顶到该值，审计 BK-011）

    返回：{max_tabs, max_cpu_percent, max_memory_mb, max_network_kbps,
           learning_enabled, reason}
    判定优先级：AI推理中 > 断网 > 电池 > 低内存 > 创作 > 空闲。
    """
    user_state = str(context.get("user_state", "idle") or "idle")
    online = bool(context.get("online", True))
    ai_inferring = bool(context.get("ai_inferring", False))
    battery_mode = bool(context.get("battery_mode", False))
    try:
        total_memory_gb = float(context.get("total_memory_gb", 16.0) or 16.0)
    except (TypeError, ValueError):
        total_memory_gb = 16.0

    def _quota(base: dict, enabled: bool, reason: str) -> dict:
        q = dict(base)
        # 硬件等级学习标签配额封顶（文档B §4.2：5090/4090→5，4070Ti→3，
        # 3060→2，RX6600/纯CPU→1）；未提供时保持模式配额不变
        try:
            tier_tabs = context.get("tier_learn_tabs")
            if tier_tabs is not None:
                q["max_tabs"] = min(q["max_tabs"], max(0, int(tier_tabs)))
        except (TypeError, ValueError):
            pass
        q["learning_enabled"] = enabled
        q["reason"] = reason
        return q

    if ai_inferring:
        return _quota(_QUOTA_ZERO, False, "ai_inferring")
    if not online:
        return _quota(_QUOTA_ZERO, False, "offline")
    if battery_mode:
        return _quota(_QUOTA_ZERO, False, "battery")
    if total_memory_gb < 8.0:
        return _quota(_QUOTA_LOW_MEMORY, True, "low_memory")
    if user_state == "creating":
        return _quota(_QUOTA_CREATING, True, "creating")
    return _quota(_QUOTA_IDLE, True, "idle")


# ═══════════════════════════════════════════════════════════════════
#  学习调度器
# ═══════════════════════════════════════════════════════════════════

# 学习时机触发器（TASK-014）：手动/定时/空闲>5分钟/项目驱动/对话缺口
TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULED = "scheduled"
TRIGGER_IDLE = "idle"
TRIGGER_PROJECT = "project"
TRIGGER_DIALOG_GAP = "dialog_gap"
VALID_TRIGGERS = (TRIGGER_MANUAL, TRIGGER_SCHEDULED, TRIGGER_IDLE,
                  TRIGGER_PROJECT, TRIGGER_DIALOG_GAP)

IDLE_THRESHOLD_S = 300.0          # 空闲 >5 分钟触发自主学习
NETWORK_CHECK_TIMEOUT_S = 3.0     # 网络检测 3s 超时

# R2-B02/R2-B03 周期评估节流：调度引擎 tick 为 1s 基频，触发器与自动
# 微调检查无需每秒执行，内部按该间隔节流
TRIGGER_EVAL_INTERVAL_S = 30.0
# 自动微调周期映射（learning_settings.auto_finetune_frequency:
# off/daily/weekly/monthly；文档B §4.4 四档，"手动"=off）
_AUTO_FINETUNE_PERIOD_S = {"daily": 86400.0, "weekly": 604800.0,
                           "monthly": 2592000.0}
# 自动微调被拒绝（条件不满足）后的重试节流，避免每拍重复探测
_AUTO_FINETUNE_RETRY_S = 600.0

# feature_lock 功能 → 学习视角的状态映射
_CREATING_FEATURES = ("paint", "video_gen", "training")
_INFERRING_FEATURES = ("dialog",)

# ── 全局优先级视角（文档 §8.4.2，services/priority.py）──────────────
# 浏览器学习为 P3：任何更高优先级（P0 用户操作 / P1 预加载 / P2 训练）
# 占用资源时学习必须让行（暂停或降配额）。
LEARNING_PRIORITY = Priority.P3_BROWSER_LEARN

# feature_lock 持有功能 → 全局优先级（未列出的后台功能视为不抢占）
_FEATURE_PRIORITY: dict[str, Priority] = {
    "dialog": Priority.P0_USER,
    "paint": Priority.P0_USER,
    "video_gen": Priority.P0_USER,
    "manga": Priority.P0_USER,
    "training": Priority.P2_TRAINING,
}


# ═══════════════════════════════════════════════════════════════════
#  LEARN-056 会话等待队列（61008 冲突时 enqueue=true 入队，
#  当前会话结束/资源允许后由 tick 自动启动队首）
# ═══════════════════════════════════════════════════════════════════

_WAITING_QUEUE_KEY = "session_waiting_queue"
_MAX_WAITING = 20


def _read_waiting_queue() -> list[dict]:
    from ..data.database import get_db_safe, parse_json
    db = get_db_safe()
    if db is None:
        return []
    try:
        row = db.query_one(
            "SELECT value FROM learning_settings WHERE key=?",
            (_WAITING_QUEUE_KEY,))
        items = parse_json(row["value"], []) if row else []
        return items if isinstance(items, list) else []
    except Exception:  # noqa: BLE001
        return []


def _write_waiting_queue(items: list[dict]) -> None:
    import json as _json

    from ..data.database import get_db_safe
    db = get_db_safe()
    if db is None:
        return
    try:
        db.sql(
            "INSERT INTO learning_settings (key, value, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (_WAITING_QUEUE_KEY, _json.dumps(items, ensure_ascii=False),
             time.time()))
    except Exception as exc:  # noqa: BLE001
        log.warning("等待队列落库失败: %s", exc)


def enqueue_waiting_session(topic_id: str, budget: dict) -> int:
    """入队等待学习会话，返回队列位置（1 起）；队列满返回 0。"""
    items = _read_waiting_queue()
    if len(items) >= _MAX_WAITING:
        return 0
    items.append({"topic_id": topic_id, "budget": budget or {},
                  "enqueued_at": time.time()})
    _write_waiting_queue(items)
    return len(items)


def list_waiting_sessions() -> list[dict]:
    """等待队列快照（供状态查询）。"""
    return _read_waiting_queue()


def pop_waiting_session() -> dict | None:
    """弹出队首；空队列返回 None。"""
    items = _read_waiting_queue()
    if not items:
        return None
    head = items.pop(0)
    _write_waiting_queue(items)
    return head

# §4.3 自适应：功能名 → 学习主题匹配关键词（中文别名 + 原名）
_COLD_FEATURE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "dialog": ("dialog", "对话"),
    "paint": ("paint", "绘画", "画图", "画画"),
    "video_gen": ("video_gen", "video", "视频"),
    "manga": ("manga", "漫画", "漫剧"),
    "training": ("training", "训练", "微调"),
    "browser_learning": ("browser_learning", "浏览器学习", "网络学习"),
    "behavior_learning": ("behavior_learning", "行为学习"),
}

# §4.3 自适应：主题选择候选数（按 updated_at 排序取前 N 个参与降权挑选）
_TOPIC_CANDIDATES = 5


def _feature_priority(feature: str | None) -> Priority | None:
    """查询功能的全局优先级；未知/空闲返回 None。"""
    if not feature:
        return None
    return _FEATURE_PRIORITY.get(str(feature).strip())


class LearningScheduler:
    """学习调度器：触发器注册 + feature_lock 联动 + 网络检测。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._triggers: dict[str, Callable[[dict], None] | None] = {
            name: None for name in VALID_TRIGGERS}
        self._last_user_activity_at = time.time()
        self._paused_due_to_creation = False
        self._network_cache: tuple[bool, float] = (True, 0.0)
        self._network_cache_ttl = 10.0   # 网络检测结果缓存 10s
        # R2-B02/R2-B03 周期评估状态
        self._last_trigger_eval_at = 0.0      # 上次触发器评估时间
        self._last_auto_finetune_at = 0.0     # 上次自动微调触发/尝试时间
        self._auto_finetune_inflight = False  # 自动微调防重入旗标
        # ── auto 学习环境退避（2026-08-20 资源爆满事故修复）──
        # Chromium 缺失时 auto 触发会话必然 browser_unavailable，
        # 调度器 30s 后再触发 → 死循环（历史 4 天 3.7 万次）。前置
        # 环境检查 + 连续失败熔断：连续 3 次环境不可用即停 auto，
        # 成功启动一次会话后自动恢复计数。
        self._auto_env_fail_streak = 0        # 连续环境不可用次数
        self._auto_circuit_opened = False     # 熔断已打开（已打日志）
        # R2-B02 接线：注册默认触发器回调（可被 register_trigger 覆盖）
        self._triggers[TRIGGER_IDLE] = self._default_learn_trigger
        self._triggers[TRIGGER_SCHEDULED] = self._default_learn_trigger
        # R2-B01 配套：对话缺口触发器默认动作（沉淀缺口主题供后续学习）
        self._triggers[TRIGGER_DIALOG_GAP] = self._default_dialog_gap_trigger

    # ── 触发器注册 ────────────────────────────────────────────────────

    def register_trigger(self, name: str,
                         callback: Callable[[dict], None] | None = None
                         ) -> dict:
        """注册学习触发器（手动/定时/空闲/项目驱动/对话缺口）。"""
        if name not in VALID_TRIGGERS:
            raise ValueError(f"无效触发器: {name}（可选: {VALID_TRIGGERS}）")
        with self._lock:
            self._triggers[name] = callback
        log.info("学习触发器已注册: %s", name)
        return {"trigger": name, "registered": True,
                "has_callback": callback is not None}

    def list_triggers(self) -> list[dict]:
        with self._lock:
            return [{"trigger": n, "has_callback": c is not None}
                    for n, c in self._triggers.items()]

    def fire_trigger(self, name: str, payload: dict | None = None) -> bool:
        """触发指定学习时机（已注册回调则调用）。"""
        with self._lock:
            callback = self._triggers.get(name)
        if callback is None:
            return False
        try:
            callback(payload or {})
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("触发器 %s 回调异常: %s", name, exc)
            return False

    # ── 用户活动与空闲 ─────────────────────────────────────────────────

    def notify_user_activity(self) -> None:
        """上报用户活动（刷新空闲计时）。"""
        with self._lock:
            self._last_user_activity_at = time.time()

    def idle_seconds(self) -> float:
        with self._lock:
            return time.time() - self._last_user_activity_at

    def is_user_idle(self) -> bool:
        """空闲 >5 分钟（TASK-014 自主探索触发条件）。"""
        return self.idle_seconds() > IDLE_THRESHOLD_S

    # ── 网络检测 ───────────────────────────────────────────────────────

    def check_network(self, use_cache: bool = True) -> bool:
        """检测联网状态：socket 连 223.5.5.5:53，失败回退 HTTP HEAD。

        3s 超时，两者都失败即离线。结果缓存 10s 避免频繁探测。
        """
        now = time.time()
        if use_cache and now - self._network_cache[1] < self._network_cache_ttl:
            return self._network_cache[0]
        online = False
        try:
            sock = socket.create_connection(("223.5.5.5", 53),
                                            timeout=NETWORK_CHECK_TIMEOUT_S)
            sock.close()
            online = True
        except OSError:
            try:
                req = urllib.request.Request(
                    "https://www.baidu.com", method="HEAD")
                with urllib.request.urlopen(
                        req, timeout=NETWORK_CHECK_TIMEOUT_S):
                    online = True
            except Exception:  # noqa: BLE001
                online = False
        self._network_cache = (online, now)
        if not online:
            log.warning("网络检测失败：判定为离线，网络学习将暂停")
        return online

    # ── feature_lock 联动 ──────────────────────────────────────────────

    def _feature_lock_state(self) -> str | None:
        """读取功能锁当前持有功能（不可用时返回 None）。"""
        try:
            from ..middleware.feature_lock import get_feature_lock
            return get_feature_lock().active_feature
        except Exception:  # noqa: BLE001
            return None

    def build_context(self, user_state: str | None = None,
                      total_memory_gb: float | None = None) -> dict:
        """采集当前运行状态，构造配额计算上下文。

        user_state 未显式给出时按 feature_lock 与空闲时间推导：
          创作类功能活跃（paint/video_gen/training）→ creating
          否则 → idle
        """
        active = self._feature_lock_state()
        ai_inferring = active in _INFERRING_FEATURES
        if user_state is None:
            user_state = ("creating" if active in _CREATING_FEATURES
                          else "idle")
        if total_memory_gb is None:
            try:
                import psutil
                total_memory_gb = round(
                    psutil.virtual_memory().total / 1073741824, 1)
            except Exception:  # noqa: BLE001
                total_memory_gb = 16.0
        # 硬件等级学习标签配额（文档B §4.2，审计 BK-011）：
        # 复用 browser_service 的档位探测（内部已缓存），失败则不封顶
        tier_learn_tabs: int | None = None
        try:
            from .browser_service import get_max_tabs
            tier_learn_tabs = get_max_tabs()
        except Exception:  # noqa: BLE001
            tier_learn_tabs = None
        return {
            "user_state": user_state,
            "online": self.check_network(),
            "ai_inferring": ai_inferring,
            "battery_mode": False,   # 桌面工作站无电池，保留接口
            "total_memory_gb": total_memory_gb,
            "tier_learn_tabs": tier_learn_tabs,
            "active_feature": active,
            "idle_seconds": round(self.idle_seconds(), 1),
        }

    def current_quota(self, context: dict | None = None) -> dict:
        """当前资源配额快照（context 缺省时自动采集）。"""
        ctx = context if context is not None else self.build_context()
        quota = get_resource_quota(ctx)
        quota["context"] = ctx
        return quota

    # ── 暂停/恢复决策（TASK-014 与调度引擎协同）─────────────────────────

    def should_pause_learning(self, context: dict | None = None) -> tuple[bool, str]:
        """是否应暂停浏览器学习（P3，文档 §8.4.2 优先级让行）：
          - 更高优先级功能占用（P0 用户创作 / P2 训练持有 feature_lock）
          - 配额为 0（AI推理/断网/电池）
          - CPU 利用率 ≥ 临界线（文档B §4.1：CPU>90% 暂停后台学习）
        """
        ctx = context if context is not None else self.build_context()
        active = ctx.get("active_feature")
        # 优先级比较替代硬编码名单：占用功能优先级高于 P3 即让行
        active_priority = _feature_priority(active)
        if active_priority is not None and active_priority < LEARNING_PRIORITY:
            return True, f"higher_priority_active:{active}({active_priority.name})"
        quota = get_resource_quota(ctx)
        if not quota["learning_enabled"]:
            return True, quota["reason"]
        # 文档B §4.1：CPU > 90% → 暂停后台学习（真实采样，psutil 非阻塞读数）
        cpu_crit = self._cpu_critical_threshold()
        cpu_now = self._cpu_util_percent()
        if cpu_now is not None and cpu_now >= cpu_crit * 100.0:
            return True, f"cpu_critical:{cpu_now:.0f}%>={cpu_crit * 100:.0f}%"
        return False, ""

    @staticmethod
    def _cpu_critical_threshold() -> float:
        """读取 CPU 临界阈值（比率 0~1）；配置缺失时取文档B §4.1 的 0.90。"""
        try:
            from ..config import THRESHOLDS
            return float(THRESHOLDS.get("cpu_util_critical", 0.90))
        except Exception:  # noqa: BLE001
            return 0.90

    @staticmethod
    def _cpu_util_percent() -> float | None:
        """非阻塞读取当前 CPU 利用率（%）；psutil 不可用返回 None。"""
        try:
            import psutil
            return float(psutil.cpu_percent(interval=None))
        except Exception:  # noqa: BLE001
            return None

    def should_resume_learning(self, context: dict | None = None) -> bool:
        """是否应恢复学习：无创作占用且配额允许。"""
        pause, _ = self.should_pause_learning(context)
        return not pause

    def evaluate(self, context: dict | None = None) -> dict:
        """综合评估当前应采取的调度动作。

        返回 {action, reason, quota, priority}：
          action ∈ start / resume / pause / stop / none
          - 更高优先级功能活跃或配额为 0 且学习进行中 → pause（置 _paused_due_to_creation）
          - 之前因创作暂停、现已空闲且配额允许 → resume
          - 空闲 >5 分钟且联网且配额允许 → start（自主探索时机）
          priority 为本学习的全局优先级（文档 §8.4.2，恒为 P3）。
        """
        ctx = context if context is not None else self.build_context()
        quota = get_resource_quota(ctx)
        pause, reason = self.should_pause_learning(ctx)
        result: dict[str, Any] = {"priority": LEARNING_PRIORITY.name}
        with self._lock:
            if pause:
                self._paused_due_to_creation = True
                result.update({"action": "pause", "reason": reason, "quota": quota})
                return result
            if self._paused_due_to_creation:
                self._paused_due_to_creation = False
                result.update({"action": "resume", "reason": "user_idle",
                               "quota": quota})
                return result
            if (ctx.get("idle_seconds", 0) > IDLE_THRESHOLD_S
                    and quota["learning_enabled"]):
                result.update({"action": "start", "reason": "idle_explore",
                               "quota": quota})
                return result
        result.update({"action": "none", "reason": "", "quota": quota})
        return result

    # ── 周期 tick：触发器评估 + 自动微调（R2-B02 / R2-B03）─────────────

    def tick(self) -> None:
        """周期评估入口（由调度引擎 1s tick 调用，内部按 30s 节流）。

        R2-B02 触发器接线：满足条件时 fire 已注册触发器——
          空闲 >5 分钟且配额允许 → TRIGGER_IDLE（默认动作：启动学习会话）
          当前时刻落在学习时段  → TRIGGER_SCHEDULED（同上）
        R2-B03 自动微调：知识点达阈值 + GPU 空闲 + 超过设定周期 → 排队微调。
        任何子步骤异常仅记日志，不影响调度主循环。
        """
        now = time.time()
        if now - self._last_trigger_eval_at < TRIGGER_EVAL_INTERVAL_S:
            return
        self._last_trigger_eval_at = now
        try:
            ctx = self.build_context()
        except Exception as exc:  # noqa: BLE001
            log.debug("触发器评估上下文采集失败: %s", exc)
            return
        quota = get_resource_quota(ctx)
        if quota["learning_enabled"]:
            if self.is_user_idle():
                self.fire_trigger(TRIGGER_IDLE, {"context": ctx})
            if self._in_schedule_window():
                self.fire_trigger(TRIGGER_SCHEDULED, {"context": ctx})
        try:
            self._maybe_auto_finetune()
        except Exception as exc:  # noqa: BLE001 - 自动微调不影响主链
            log.warning("自动微调检查异常: %s", exc)
        try:
            self._drain_session_waiting_queue()
        except Exception as exc:  # noqa: BLE001 - 队列消费不影响主链
            log.debug("等待队列消费跳过: %s", exc)

    def _drain_session_waiting_queue(self) -> None:
        """LEARN-056：无活跃会话且配额允许时，自动启动等待队列队首。"""
        if not self._auto_browser_env_ok():
            return
        if not _read_waiting_queue():
            return
        from .browser_agent_service import (
            ensure_learning_tables,
            get_browser_agent_service,
            get_learning_settings,
        )
        settings = get_learning_settings()
        if not settings.get("enabled", True):
            return
        agent = get_browser_agent_service()
        if agent.active_session() is not None:
            return
        pause, _reason = self.should_pause_learning()
        if pause:
            return
        head = pop_waiting_session()
        if head is None:
            return
        topic_id = str(head.get("topic_id") or "")
        from ..data.database import get_db_safe
        db = get_db_safe()
        if db is None:
            return
        ensure_learning_tables()
        row = db.query_one(
            "SELECT id, name, seed_urls, max_pages FROM learning_topics"
            " WHERE id=? AND status='active'", (topic_id,))
        if row is None:
            log.info("等待队列队首主题已失效，丢弃: %s", topic_id[:8])
            return
        budget = dict(head.get("budget") or {})
        budget.setdefault("max_time_minutes",
                          int(settings.get("max_time_minutes", 30) or 30))
        budget.setdefault("max_pages",
                          int(row.get("max_pages") or
                              settings.get("max_pages", 20) or 20))
        from ..data.database import parse_json as _pj
        seeds = _pj(row.get("seed_urls"), [])
        if seeds:
            budget["seed_urls"] = seeds
        session = agent.create_session(row["id"], row["name"], budget=budget)
        agent.start_session(session)
        log.info("等待队列自动启动学习会话: %s（主题: %s）",
                 session.session_id[:8], row["name"])

    def _in_schedule_window(self) -> bool:
        """当前时刻是否落在任一学习时段（schedule_windows ["HH:MM-HH:MM"]，
        支持跨午夜时段；空列表表示未配置定时学习）。"""
        try:
            from .browser_agent_service import get_learning_settings
            windows = get_learning_settings().get("schedule_windows") or []
        except Exception:  # noqa: BLE001
            return False
        now_min = time.localtime().tm_hour * 60 + time.localtime().tm_min
        for w in windows:
            try:
                start_s, end_s = str(w).split("-", 1)
                sh, sm = (int(x) for x in start_s.strip().split(":"))
                eh, em = (int(x) for x in end_s.strip().split(":"))
                start, end = sh * 60 + sm, eh * 60 + em
                if start <= end:
                    if start <= now_min <= end:
                        return True
                elif now_min >= start or now_min <= end:   # 跨午夜
                    return True
            except (ValueError, AttributeError):
                continue
        return False

    def _auto_browser_env_ok(self) -> bool:
        """auto 学习环境闸门（2026-08-20 事故修复）。

        前置检查浏览器组件可用性：playwright 缺失或 Chromium 二进制
        缺失（环境级熔断已打开）时拦截 auto 触发；连续 3 次不可用打
        开调度层熔断并只打一次警告，杜绝 30s 死循环。手动触发
        （start_session 由 API 直接调用）不受此闸门影响。
        """
        from .browser_service import chromium_env_broken, playwright_available
        if playwright_available() and not chromium_env_broken():
            self._auto_env_fail_streak = 0
            return True
        self._auto_env_fail_streak += 1
        if self._auto_env_fail_streak >= 3 and not self._auto_circuit_opened:
            self._auto_circuit_opened = True
            log.warning(
                "浏览器组件不可用连续 %d 次，auto 学习触发已熔断（安装"
                " Chromium 并重启后端后自动恢复；手动学习不受影响）",
                self._auto_env_fail_streak)
        return False

    def _auto_resource_ok(self) -> bool:
        """auto 学习资源闸门（2026-08-23 后端 OOM 死亡事故修复）。

        学习会话的 Chromium 爬虫常驻数 GB RAM；当重资源功能（绘画/
        对话推理等）持功能锁运行、或系统可用 RAM 水位不足时，叠加
        装载会导致 RAM 逼近 100% → 后端进程被系统杀死（本次事故：
        qwen GGUF 12.31GB 常驻 + auto 学习 Chromium → RAM 99%）。

        auto 触发仅是"锦上添花"，遇资源紧张一律让位跳过；手动触发
        （start_session 由 API 直接调用）不受此闸门影响。
        """
        active = self._feature_lock_state()
        if active is not None:
            log.debug("auto 学习跳过：功能锁被 %s 持有（重资源任务"
                      "进行中）", active)
            return False
        try:
            import psutil
            available_gb = psutil.virtual_memory().available / 1024 ** 3
            if available_gb < 8:
                log.debug("auto 学习跳过：可用 RAM 仅 %.1fGB（阈值 8GB）",
                          available_gb)
                return False
        except Exception:  # noqa: BLE001 - psutil 缺失保守放行
            pass
        # 显存闸门（2026-08-23）：Chromium 硬件合成会挤占 WDDM 共享
        # 预算，重模型驻留期（vRAM>88%）拉起爬虫易把推理 GPU 工作集
        # 挤爆（device lost）；auto 让位，手动触发不受限
        try:
            from .model_manager import get_model_manager
            gpu = get_model_manager().get_gpu_status()
            used_mb = int(gpu.get("vram_used_mb", 0))
            total_mb = int(gpu.get("vram_total_mb", 0))
            if total_mb > 0 and used_mb / total_mb > 0.88:
                log.debug("auto 学习跳过：显存占用 %.0f%%（阈值 88%%）",
                          used_mb / total_mb * 100)
                return False
        except Exception:  # noqa: BLE001 - 探测失败保守放行
            pass
        return True

    def _default_learn_trigger(self, payload: dict) -> None:
        """默认触发动作（R2-B02）：无活跃会话且学习开关开启时，
        为最近活跃主题启动一个学习会话（复用学习会话入口逻辑）。"""
        trigger = str((payload or {}).get("trigger") or "auto")
        if not self._auto_browser_env_ok():
            return
        if not self._auto_resource_ok():
            return
        try:
            from .browser_agent_service import (
                ensure_learning_tables,
                get_browser_agent_service,
                get_learning_settings,
            )
            settings = get_learning_settings()
            if not settings.get("enabled", True):
                return
            # 流量配额预检（2026-08-29 E2E 热循环修复）：会话内的
            # traffic_limit 只掐进行中的会话——配额耗尽后 auto 触发器
            # 每 30s「启动→秒掐→再触发」死循环（实测 20min，每轮创建/
            # 销毁一个 Chromium 上下文）。预检配额满 → 当日静默。
            try:
                from .browser_agent_service import get_traffic_today
                limit_mb = float(settings.get(
                    "daily_traffic_limit_mb", 50) or 50)
                if get_traffic_today() >= limit_mb * 1048576:
                    log.debug("触发器 %s：每日流量配额已用尽，自动学习静默",
                              trigger)
                    return
            except Exception:  # noqa: BLE001 - 预检失败不阻断正常触发
                pass
            agent = get_browser_agent_service()
            if agent.active_session() is not None:
                return
            pause, reason = self.should_pause_learning()
            if pause:
                log.debug("触发器 %s 动作跳过：%s", trigger, reason)
                return
            # 取最近更新的活跃主题作为自主学习目标
            # §4.3 自适应：取前 N 个候选，冷门功能相关主题降权排后
            from ..data.database import get_db_safe
            db = get_db_safe()
            if db is None:
                return
            ensure_learning_tables()
            rows = db.query(
                "SELECT id, name, keywords FROM learning_topics "
                "WHERE status='active' ORDER BY updated_at DESC LIMIT ?",
                (_TOPIC_CANDIDATES,))
            if not rows:
                log.debug("触发器 %s：无活跃学习主题，跳过自动学习", trigger)
                return
            row = self._pick_topic_skip_cold(rows)
            budget = {"max_time_minutes": int(settings.get(
                          "max_time_minutes", 30) or 30),
                      "max_pages": int(settings.get("max_pages", 20) or 20)}
            session = agent.create_session(row["id"], row["name"],
                                           budget=budget)
            agent.start_session(session)
            log.info("触发器 %s 自动启动学习会话: %s（主题: %s）",
                     trigger, session.session_id[:8], row["name"])
        except Exception as exc:  # noqa: BLE001 - 默认动作失败不影响调度
            log.warning("触发器 %s 默认动作异常: %s", trigger, exc)

    def _pick_topic_skip_cold(self, rows: list[dict]) -> dict:
        """§4.3 自适应：候选主题（已按 updated_at DESC 排序）中，关键词
        匹配冷门功能的主题降权排后，返回首个非冷门主题；全部命中时保持
        原序返回首个（仅降权不禁学）。无使用统计时原样返回 rows[0]。
        """
        try:
            from .model_manager import get_model_manager
            cold = get_model_manager().predictor.get_cold_features()
        except Exception:  # noqa: BLE001 - 统计不可用时不降权
            cold = []
        if not cold:
            return rows[0]
        cold_kws = [kw for f in cold
                    for kw in _COLD_FEATURE_KEYWORDS.get(f, (f,))]

        def _hits_cold(row: dict) -> bool:
            text = str(row.get("name") or "").lower()
            kws = row.get("keywords")
            if isinstance(kws, str):      # JSON 文本列
                text += " " + kws.lower()
            elif isinstance(kws, (list, tuple)):
                text += " " + " ".join(str(k).lower() for k in kws)
            return any(kw.lower() in text for kw in cold_kws)

        for row in rows:
            if _hits_cold(row):
                log.debug("§4.3 自适应：主题「%s」命中冷门功能 %s，"
                          "学习优先级降权排后", row.get("name"), cold)
                continue
            return row
        return rows[0]

    def _default_dialog_gap_trigger(self, payload: dict) -> None:
        """对话缺口触发器默认动作（R2-B01 配套，TASK-014 对话缺口时机）。

        把对话被动补全发现的缺口关键词沉淀为学习主题（source=auto），
        后续由空闲/定时触发器按正常门控（配额/优先级/开关）拾起学习。
        不在此同步启动会话——对话进行中学习为 P3 必须让行（§8.4.2）。
        同名活跃主题已存在时仅刷新 updated_at（置顶为下次学习目标）。
        """
        try:
            keywords = [str(k).strip() for k in (payload or {}).get("keywords")
                        or [] if str(k).strip()][:4]
            if not keywords:
                return
            from .browser_agent_service import ensure_learning_tables, get_learning_settings
            settings = get_learning_settings()
            if not settings.get("enabled", True):
                return
            from ..data.database import get_db_safe
            db = get_db_safe()
            if db is None:
                return
            ensure_learning_tables()
            name = "对话缺口：" + " ".join(keywords[:3])
            now = time.time()
            row = db.query_one(
                "SELECT id FROM learning_topics WHERE name=? AND "
                "status='active'", (name,))
            if row is not None:
                db.update("learning_topics", {"updated_at": now},
                          "id=?", (row["id"],))
                log.debug("对话缺口主题已存在，刷新优先级: %s", name)
                return
            db.insert("learning_topics", {
                "id": uuid.uuid4().hex, "name": name,
                "keywords": keywords, "status": "active",
                "progress": 0.0, "knowledge_count": 0,
                "source": "auto", "created_at": now, "updated_at": now,
            })
            log.info("对话缺口已沉淀为学习主题: %s（待空闲/定时触发学习）",
                     name)
        except Exception as exc:  # noqa: BLE001 - 沉淀失败不影响对话主链
            log.warning("对话缺口主题沉淀异常: %s", exc)

    def _maybe_auto_finetune(self) -> None:
        """自动微调执行器（R2-B03）：消费 auto_finetune_frequency 设置。

        触发条件（全部满足）：
          - auto_finetune_frequency ≠ off（daily=24h / weekly=7d 周期）
          - 距上次微调产出超过设定周期（以最新 LoRA 版本 meta 创建时间为准）
          - 知识点数达到微调阈值（config.yaml scheduler.thresholds.
            finetune_min_knowledge，缺省回退规格 §3.3 的 100 条）
          - GPU 空闲：功能锁未占用 且 空闲显存 ≥ 训练下限
          - 防重入：一次自动微调进行中不再触发（服务层二次兜底）
        """
        if self._auto_finetune_inflight:
            return
        from .browser_agent_service import get_learning_settings
        settings = get_learning_settings()
        # §4.3 自适应：连续质量下降已置暂停旗标时不再自动触发
        # （需用户在学习设置中重新开启 auto_finetune_frequency 解除）
        if settings.get("auto_train_paused"):
            log.debug("自动微调跳过：auto_train_paused 已置位（§4.3 自适应）")
            return
        freq = str(settings.get("auto_finetune_frequency", "daily") or "daily")
        period = _AUTO_FINETUNE_PERIOD_S.get(freq)
        if period is None:                      # off
            return
        now = time.time()
        if now - self._last_auto_finetune_at < _AUTO_FINETUNE_RETRY_S:
            return
        from .lora_training_service import (
            MIN_FREE_VRAM_GB,
            MIN_TRAINING_SAMPLES,
            get_lora_training_service,
        )
        svc = get_lora_training_service()
        # §4.3 自适应：连续 3 次训练质量下降 → 暂停自动训练并通知用户
        if svc.check_quality_decline_pause():
            self._pause_auto_train()
            return
        # 周期判定：以最新版本创建时间为上次微调时间（无版本视为已到期）
        last_trained_at = 0.0
        try:
            for v in svc.list_versions():
                last_trained_at = max(last_trained_at,
                                      float(v.get("created_at", 0) or 0))
        except Exception:  # noqa: BLE001
            pass
        if last_trained_at and now - last_trained_at < period:
            return
        # 知识点阈值（config.yaml 可配，缺省 = 规格 §3.3 最少训练样本数）
        threshold = MIN_TRAINING_SAMPLES
        try:
            from ..config import THRESHOLDS
            threshold = int(THRESHOLDS.get("finetune_min_knowledge",
                                           MIN_TRAINING_SAMPLES))
        except Exception:  # noqa: BLE001
            pass
        try:
            from .knowledge_service import get_knowledge_service
            knowledge_count = get_knowledge_service().count()
        except Exception:  # noqa: BLE001
            knowledge_count = 0
        if knowledge_count < threshold:
            log.debug("自动微调跳过：知识点 %d < 阈值 %d",
                      knowledge_count, threshold)
            return
        # GPU 空闲：功能锁未占用 + 空闲显存达标（不抢占、不 bypass）
        from ..middleware.feature_lock import get_feature_lock
        if get_feature_lock().active_feature is not None:
            return
        try:
            import torch
            if not torch.cuda.is_available():
                return
            free_gb = torch.cuda.mem_get_info()[0] / (1024 ** 3)
        except Exception:  # noqa: BLE001
            return
        if free_gb < MIN_FREE_VRAM_GB:
            log.debug("自动微调跳过：空闲显存 %.1fGB < %.1fGB",
                      free_gb, MIN_FREE_VRAM_GB)
            return
        self._auto_finetune_inflight = True
        try:
            task_id = svc.trigger_finetune(priority="background")
            self._last_auto_finetune_at = now
            if task_id:
                log.info("自动微调已触发: task=%s（知识点 %d ≥ %d，"
                         "频率 %s，空闲显存 %.1fGB）",
                         task_id, knowledge_count, threshold, freq, free_gb)
            else:
                log.debug("自动微调条件未通过服务层复核（数据/锁/队列）")
        finally:
            self._auto_finetune_inflight = False

    def _pause_auto_train(self) -> None:
        """§4.3 自适应：连续质量下降时暂停自动训练——置学习设置
        auto_train_paused=True（持久化），并 log.warning + ws_hub 通知用户。
        已暂停时直接返回，避免每拍重复写库/重复通知。
        """
        from .browser_agent_service import get_learning_settings, update_learning_settings
        if get_learning_settings().get("auto_train_paused"):
            return
        update_learning_settings({"auto_train_paused": True})
        log.warning("§4.3 自适应：LoRA 训练连续 3 次质量下降，"
                    "已暂停自动训练（auto_train_paused=True），"
                    "用户可在学习设置中重新开启自动微调")
        try:
            from .ws_hub import get_ws_hub
            get_ws_hub().broadcast({
                "type": "notification",
                "data": {
                    "event": "auto_train_paused",
                    "level": "warning",
                    "message": "LoRA 自动训练已连续 3 次质量下降，已自动暂停；"
                               "可在学习设置中重新开启自动微调频率以恢复。",
                },
            })
        except Exception:  # noqa: BLE001 - 通知失败不影响暂停生效
            pass


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_scheduler: LearningScheduler | None = None
_scheduler_lock = threading.Lock()


def get_learning_scheduler() -> LearningScheduler:
    """获取学习调度器单例。"""
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                _scheduler = LearningScheduler()
    return _scheduler
# 本项目仅供学习使用，商业授权请+Q 3559331368
