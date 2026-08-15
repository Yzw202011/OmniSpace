"""OmniSpace AI v2.3.1 AI 浏览 Agent 决策循环（TASK-032/040/042/052）。

主循环：感知(perceive)→理解(understand)→决策(decide)→执行(execute)
       →知识提取(extract)→评估(evaluate)，每 5 分钟保存检查点。

分级决策（TASK-052 v2.3.1）：
  classify_page() 规则分类器（DOM结构/内容密度/交互特征/站点特征）→
  简单页走快速路径（跳过截图与视觉理解，轻量提供者 Qwen3-4B 或规则回退，
  目标 <500ms）；复杂页维持 DOM+视觉 双模慢速决策（<5s，准确率不降）。

解耦设计：
  - 决策大模型（Qwen3-VL-4B，由另一服务加载）通过"决策提供者注入"接入：
      set_decision_provider(callable(prompt)->str)        —— 慢速路径（VL）
      set_fast_decision_provider(callable(prompt)->str)   —— 快速路径（4B）
      set_page_understanding_provider(callable(prompt)->str)
    未注入时使用规则回退策略（搜索→点第一个结果→滚动→提取→翻页），
    保证循环可运转。
  - 知识入库按契约调用 knowledge_service（另一 agent 实现），
    import 失败时降级跳过提取，循环不中断。
  - 进度推送通过注入的 ws_broadcaster callable(dict)，未注入则跳过。

安全硬约束（对应 TC-S 用例）：
  - 检测登录墙/验证码/付费墙 → 立即离开并日志，绝不尝试绕过/破解
  - 绝不填写除搜索框外任何表单（browser_service 层强制）
  - 绝不点击下载（拦截层强制）
  - 导航前黑名单检查；操作间隔 ≥1s（browser_service 节流）
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, quote_plus, urlparse

from ..config import DATA_DIR
from . import browser_service as bs
from .browser_service import (
    BlacklistedDomainError,
    BrowserError,
    BrowserUnavailableError,
    InvalidUrlError,
    get_browser_service,
)

log = logging.getLogger("omnispace.browser_agent")

# ═══════════════════════════════════════════════════════════════════
#  提供者注入点（运行时由外部注入，未注入走规则回退）
# ═══════════════════════════════════════════════════════════════════

_decision_provider: Optional[Callable[[str], str]] = None
_fast_decision_provider: Optional[Callable[[str], str]] = None
_page_understanding_provider: Optional[Callable[[str], str]] = None
_ws_broadcaster: Optional[Callable[[dict], None]] = None


def set_decision_provider(fn: Optional[Callable[[str], str]]) -> None:
    """注入决策提供者：callable(prompt)->str，返回 A~G 决策文本。"""
    global _decision_provider
    _decision_provider = fn


def set_fast_decision_provider(fn: Optional[Callable[[str], str]]) -> None:
    """注入快速决策提供者（TASK-052 快速路径，轻量文本模型如 Qwen3-4B）。

    简单页面（博客/新闻/文档）跳过视觉理解，仅用 DOM 文本做快速决策。
    未注入时快速路径回退规则策略（<1ms，保证 <500ms 验收指标）。
    """
    global _fast_decision_provider
    _fast_decision_provider = fn


def set_page_understanding_provider(fn: Optional[Callable[[str], str]]) -> None:
    """注入页面理解提供者：callable(prompt)->str，返回页面摘要。"""
    global _page_understanding_provider
    _page_understanding_provider = fn


def set_ws_broadcaster(fn: Optional[Callable[[dict], None]]) -> None:
    """注入 WebSocket 进度推送：callable(dict)，未注入则跳过。"""
    global _ws_broadcaster
    _ws_broadcaster = fn


def _broadcast(payload: dict) -> None:
    """推送进度（未注入或推送异常时静默跳过）。"""
    if _ws_broadcaster is None:
        return
    try:
        _ws_broadcaster(payload)
    except Exception as exc:  # noqa: BLE001
        log.debug("进度推送失败（忽略）: %s", exc)


def _resolve_knowledge_service() -> Optional[Any]:
    """按契约解析知识服务（另一 agent 实现，容忍其不存在）。"""
    try:
        from backend.services.knowledge_service import get_knowledge_service
        return get_knowledge_service()
    except Exception:  # noqa: BLE001
        try:
            from .knowledge_service import get_knowledge_service  # type: ignore
            return get_knowledge_service()
        except Exception:  # noqa: BLE001
            return None


def _make_llm_extractor() -> Optional[Any]:
    """把对话引擎包装成知识提取 callable(prompt)->str。

    引擎未加载/不就绪时抛异常 —— knowledge_service 会自动回退规则提取，
    保证离线/显存紧张场景下学习不中断。
    """
    try:
        from .inference.dialog_engine import get_dialog_engine
    except Exception:  # noqa: BLE001
        return None
    engine = get_dialog_engine()

    def _extract(prompt: str) -> str:
        if not engine.is_ready:
            raise RuntimeError("dialog engine not ready")
        return engine.chat([{"role": "user", "content": prompt}],
                           temperature=0.3, max_new_tokens=1024)

    return _extract


def _ensure_llm_extractor() -> None:
    """幂等注入 LLM 提取器（学习会话启动时调用一次即可）。"""
    ks = _resolve_knowledge_service()
    if ks is None or callable(getattr(ks, "_llm_extractor", None)):
        return
    extractor = _make_llm_extractor()
    if extractor is not None:
        try:
            ks.set_llm_extractor(extractor)
            log.info("已向 knowledge_service 注入 LLM 提取器（对话引擎）")
        except Exception as exc:  # noqa: BLE001
            log.warning("注入 LLM 提取器失败（将使用规则提取）: %s", exc)


# ═══════════════════════════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════════════════════════

CHECKPOINT_DIR = DATA_DIR / "checkpoints"
CHECKPOINT_INTERVAL_S = 300        # 每 5 分钟保存检查点（TASK-040）
SESSION_HARD_CAP_MINUTES = 60      # 单会话硬上限 60 分钟
MAX_OPERATIONS = 200               # 单会话操作上限（循环防护）
RECENT_ACTIONS_WINDOW = 10         # 循环检测窗口：最近 10 次操作
SAME_ACTION_THRESHOLD = 3          # 连续 3 次相同操作 → 强制换方向
PAGE_VISIT_THRESHOLD = 3           # 同页访问 >3 次 → 跳过
WALL_CONSECUTIVE_LIMIT = 5         # 连续 5 次撞墙 → 判定"所有来源需登录"
DECISION_TIMEOUT_S = 5.0           # 决策超时（<5 秒，TC-A-001）
FAST_DECISION_TIMEOUT_S = 2.0      # 快速路径决策硬超时（TASK-052）
FAST_DECISION_TARGET_S = 0.5       # 简单页面决策目标 <500ms（TASK-052 验收）
COMPLEX_SCORE_THRESHOLD = 3.0      # 页面复杂度评分 ≥3 → 复杂页（慢速路径）
COVERAGE_DONE = 0.9                # 覆盖度 >90% 视为目标达成

SEARCH_ENGINES = {
    "bing": "https://www.bing.com/search?q={q}",
    "baidu": "https://www.baidu.com/s?wd={q}",
    "sogou": "https://www.sogou.com/web?query={q}",
    "so360": "https://www.so.com/s?q={q}",
}
DEFAULT_SEARCH_ENGINE = "bing"

# 学习设置默认值（持久化到 learning_settings 表）
DEFAULT_LEARNING_SETTINGS: dict[str, Any] = {
    "enabled": True,                    # 联网学习开关
    "schedule_windows": [],             # 学习时段 ["22:00-07:00"]
    "max_time_minutes": 30,             # 单会话时长上限
    "max_pages": 20,                    # 单会话页数上限
    "search_engine": DEFAULT_SEARCH_ENGINE,
    "domain_whitelist": [],             # 域名白名单（空=不限制）
    "domain_blacklist": [],             # 域名黑名单（TC-S-010）
    "ad_filter_enabled": True,          # 广告过滤开关
    "sensitive_content_handling": "mask",   # 敏感内容处理 skip/mask/stop（文档B §4.4 默认标记待审核，审计 R2-B13）
    "show_browser": True,               # 显示浏览器窗口（文档B §4.4 默认开启，审计 R2-B13）
    "daily_traffic_limit_mb": 50,       # 单日流量上限（TC-S-009，默认50MB）
    "auto_finetune_frequency": "weekly", # 自动微调频率 off/daily/weekly/monthly（文档B §4.4 默认每周，审计 R2-B13）
    "auto_train_paused": False,         # §4.3 自适应：连续质量下降自动暂停旗标（重新开启频率时清除）
    "behavior_learning_enabled": True,  # 行为学习开关
    # LEARN-037 资源阈值（学习会话运行期间的资源占用上限；
    # 配额矩阵读这些值，用户可调）
    "cpu_percent_limit": 20.0,          # 学习期 CPU 占用上限 %
    "mem_percent_limit": 30.0,          # 学习期内存占用上限 %
    "bandwidth_mbps_limit": 4.0,        # 学习期带宽上限 Mbps
}

# 学习域错误码（6xxxx 段：61001-61008）
ERR_TOPIC_NOT_FOUND = 61001
ERR_SESSION_NOT_FOUND = 61002
ERR_SESSION_STATE = 61003
ERR_LEARN_UNAVAILABLE = 61004
ERR_QUOTA_DENIED = 61005
ERR_TRAFFIC_LIMIT = 61006
ERR_TOPIC_LIMIT = 61007
ERR_SESSION_RUNNING = 61008


class LearningError(Exception):
    """学习域业务异常，携带统一错误码。"""

    def __init__(self, code: int, message: str, detail: Any = None):
        self.code = code
        self.message = message
        self.detail = detail if detail is not None else {}
        super().__init__(message)


# ═══════════════════════════════════════════════════════════════════
#  决策提示词模板（TASK-032 规格原文）
# ═══════════════════════════════════════════════════════════════════

DECISION_PROMPT = """你是一个网络学习Agent。

【学习目标】{goal}
【已学知识摘要】{learned_summary}（共{learned_count}条）
【当前页面标题】{page_title}
【当前页面内容摘要】{page_content_summary}
【当前页面链接】{page_links}（共{link_count}个）
【已浏览页数】{pages_visited} / {max_pages}
【已用时】{elapsed} / {budget}
【当前页面是否读完】{page_fully_read}

请选择下一步操作（只选一个）：
A. 继续滚动阅读当前页面
B. 点击链接：[列出最相关的3个链接]
C. 返回上一页/搜索结果
D. 搜索新关键词：（给出关键词）
E. 打开新标签页
F. 结束本次学习（目标已达成）
G. 调整学习方向：（说明新方向）

同时给出理由（一句话）。"""


def build_search_url(keyword: str, engine: str = DEFAULT_SEARCH_ENGINE) -> str:
    """构造搜索引擎 URL（直接导航，无需填写表单，TC-S-012 友好）。"""
    template = SEARCH_ENGINES.get(engine, SEARCH_ENGINES[DEFAULT_SEARCH_ENGINE])
    return template.format(q=quote_plus(keyword))


# ═══════════════════════════════════════════════════════════════════
#  数据类
# ═══════════════════════════════════════════════════════════════════

@dataclass
class SubGoal:
    """学习子目标（plan_goal 输出）。"""
    id: str
    title: str
    keywords: list[str] = field(default_factory=list)
    done: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title,
                "keywords": list(self.keywords), "done": self.done}

    @classmethod
    def from_dict(cls, d: dict) -> "SubGoal":
        return cls(id=str(d.get("id", uuid.uuid4().hex[:8])),
                   title=str(d.get("title", "")),
                   keywords=list(d.get("keywords", []) or []),
                   done=bool(d.get("done", False)))


@dataclass
class LearningBudget:
    """学习预算：时间/页数上限（TC-A-003）。"""
    max_time_minutes: int = 30
    max_pages: int = 20

    def to_dict(self) -> dict:
        return {"max_time_minutes": self.max_time_minutes,
                "max_pages": self.max_pages}

    @classmethod
    def from_dict(cls, d: dict | None) -> "LearningBudget":
        d = d or {}
        return cls(max_time_minutes=int(d.get("max_time_minutes", 30) or 30),
                   max_pages=int(d.get("max_pages", 20) or 20))


@dataclass
class LearningSession:
    """学习会话（TASK-032 规格）。"""
    session_id: str
    topic_id: str
    goal: str
    sub_goals: list[SubGoal] = field(default_factory=list)
    budget: LearningBudget = field(default_factory=LearningBudget)
    status: str = "pending"     # pending/running/paused/completed/interrupted
    pages_visited: int = 0
    knowledge_extracted: int = 0
    checkpoint: dict = field(default_factory=dict)
    logs: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    last_checkpoint_at: float = 0.0
    # ── 运行态（不入检查点的控制标志）──
    stop_requested: bool = False
    pause_requested: bool = False
    resource_preempted: bool = False
    stop_reason: str = ""
    current_url: str = ""
    coverage: float = 0.0
    wall_hits: int = 0
    consecutive_wall_hits: int = 0
    operation_count: int = 0
    recent_actions: deque = field(default_factory=lambda: deque(maxlen=RECENT_ACTIONS_WINDOW))
    page_visit_counts: dict = field(default_factory=dict)
    # 本会话已撞墙（登录/验证码/付费）的域名 → 链接选择时跳过，避免反复选中
    wall_hosts: dict = field(default_factory=dict)
    scroll_depth: int = 0
    keyword_index: int = 0
    last_search_url: str = ""
    learned_summary: list[str] = field(default_factory=list)
    last_traffic_bytes: int = 0       # 流量增量同步游标（TC-S-009）
    # TASK-052 分级决策统计：{"fast": n, "slow": n}（观测快速路径占比）
    path_stats: dict = field(default_factory=lambda: {"fast": 0, "slow": 0})

    def log_entry(self, action: str, reason: str, result: str) -> dict:
        """追加操作日志（时间戳/动作/理由/结果）。"""
        entry = {"ts": round(time.time(), 3), "action": action,
                 "reason": reason, "result": result}
        self.logs.append(entry)
        return entry

    def to_status_dict(self) -> dict:
        elapsed = (time.time() - self.started_at) / 60.0 if self.started_at else 0.0
        return {
            "session_id": self.session_id,
            "topic_id": self.topic_id,
            "goal": self.goal,
            "status": self.status,
            "pages_visited": self.pages_visited,
            "knowledge_extracted": self.knowledge_extracted,
            "coverage": round(self.coverage, 3),
            "current_url": self.current_url,
            "elapsed_minutes": round(elapsed, 2),
            "budget": self.budget.to_dict(),
            "sub_goals": [g.to_dict() for g in self.sub_goals],
            "operation_count": self.operation_count,
            "stop_reason": self.stop_reason,
            "created_at": self.created_at,
            "started_at": self.started_at,
        }


# ═══════════════════════════════════════════════════════════════════
#  SQLite 持久化：learning_settings / learning_topics /
#                learning_sessions / learning_logs（自建表）
# ═══════════════════════════════════════════════════════════════════

_LEARNING_DDL = """
CREATE TABLE IF NOT EXISTS learning_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT DEFAULT '{}',
    updated_at  REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS learning_topics (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    keywords        TEXT DEFAULT '[]',
    status          TEXT DEFAULT 'active',
    progress        REAL DEFAULT 0,
    knowledge_count INTEGER DEFAULT 0,
    source          TEXT DEFAULT 'manual',
    depth           TEXT DEFAULT 'standard',
    seed_urls       TEXT DEFAULT '[]',
    max_pages       INTEGER DEFAULT 20,
    created_at      REAL NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS learning_sessions (
    id                  TEXT PRIMARY KEY,
    topic_id            TEXT NOT NULL,
    goal                TEXT DEFAULT '',
    budget              TEXT DEFAULT '{}',
    status              TEXT DEFAULT 'pending',
    pages_visited       INTEGER DEFAULT 0,
    knowledge_extracted INTEGER DEFAULT 0,
    coverage            REAL DEFAULT 0,
    stop_reason         TEXT DEFAULT '',
    trigger_type        TEXT DEFAULT 'manual',
    started_at          REAL DEFAULT 0,
    ended_at            REAL DEFAULT 0,
    created_at          REAL NOT NULL DEFAULT 0,
    updated_at          REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_learning_sessions_topic
    ON learning_sessions(topic_id);
CREATE TABLE IF NOT EXISTS learning_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    ts          REAL NOT NULL DEFAULT 0,
    action      TEXT DEFAULT '',
    reason      TEXT DEFAULT '',
    result      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_learning_logs_session
    ON learning_logs(session_id);
"""

_tables_ready = False
_tables_lock = threading.Lock()


def ensure_learning_tables() -> bool:
    """创建学习域自建表（幂等）。"""
    global _tables_ready
    if _tables_ready:
        return True
    with _tables_lock:
        if _tables_ready:
            return True
        try:
            from ..data.database import get_db
            get_db().executescript(_LEARNING_DDL)
            # 批 3（LEARN-004/005）：已存在库补 depth/seed_urls/max_pages 列
            db = get_db()
            existing = {r["name"] for r in db.query(
                "PRAGMA table_info(learning_topics)")}
            for col, ddl in (("depth", "TEXT DEFAULT 'standard'"),
                             ("seed_urls", "TEXT DEFAULT '[]'"),
                             ("max_pages", "INTEGER DEFAULT 20")):
                if col not in existing:
                    db.sql(f"ALTER TABLE learning_topics ADD COLUMN {col} {ddl}")
            _tables_ready = True
            log.info("学习域数据表初始化完成（4 张表）")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("学习域数据表初始化失败: %s", exc)
            return False


def _db():
    from ..data.database import get_db_safe
    return get_db_safe()


def get_learning_settings() -> dict:
    """读取学习设置（合并默认值）。"""
    settings = dict(DEFAULT_LEARNING_SETTINGS)
    db = _db()
    if db is not None:
        try:
            ensure_learning_tables()
            from ..data.database import parse_json
            rows = db.query("SELECT key, value FROM learning_settings")
            for row in rows:
                settings[row["key"]] = parse_json(row.get("value"),
                                                  settings.get(row["key"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("读取学习设置失败（使用默认值）: %s", exc)
    return settings


def update_learning_settings(patch: dict) -> dict:
    """更新学习设置（白名单键），返回合并后的完整设置。"""
    db = _db()
    now = time.time()
    if db is not None:
        try:
            ensure_learning_tables()
            for key, value in patch.items():
                if key not in DEFAULT_LEARNING_SETTINGS:
                    continue
                db.sql(
                    "INSERT INTO learning_settings (key, value, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), now))
        except Exception as exc:  # noqa: BLE001
            log.warning("写入学习设置失败: %s", exc)
    merged = get_learning_settings()
    # 联动：黑名单/广告过滤变更即时生效
    try:
        svc = get_browser_service()
        if "domain_blacklist" in patch:
            svc.reload_blacklist()
        if "ad_filter_enabled" in patch:
            svc.set_ad_filter(bool(patch["ad_filter_enabled"]))
    except Exception:  # noqa: BLE001
        pass
    return merged


def _traffic_key_today() -> str:
    return time.strftime("traffic_%Y%m%d")


def record_traffic(num_bytes: int) -> int:
    """累计今日流量消耗，返回今日总量（TC-S-009）。"""
    if num_bytes <= 0:
        return get_traffic_today()
    db = _db()
    if db is None:
        return 0
    try:
        ensure_learning_tables()
        key = _traffic_key_today()
        row = db.query_one("SELECT value FROM learning_settings WHERE key=?",
                           (key))
        from ..data.database import parse_json
        current = int(parse_json(row["value"], 0) if row else 0)
        current += num_bytes
        db.sql(
            "INSERT INTO learning_settings (key, value, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, str(current), time.time()))
        return current
    except Exception:  # noqa: BLE001
        return 0


def get_traffic_today() -> int:
    """读取今日已消耗流量（字节）。"""
    db = _db()
    if db is None:
        return 0
    try:
        ensure_learning_tables()
        from ..data.database import parse_json
        row = db.query_one("SELECT value FROM learning_settings WHERE key=?",
                           (_traffic_key_today(),))
        return int(parse_json(row["value"], 0) if row else 0)
    except Exception:  # noqa: BLE001
        return 0


def persist_session_log(session_id: str, entry: dict) -> None:
    """操作日志落库（learning_logs 表，供报告与前端查看）。"""
    db = _db()
    if db is None:
        return
    try:
        ensure_learning_tables()
        db.insert("learning_logs", {
            "session_id": session_id,
            "ts": float(entry.get("ts", time.time())),
            "action": str(entry.get("action", ""))[:200],
            "reason": str(entry.get("reason", ""))[:500],
            "result": str(entry.get("result", ""))[:500],
        })
    except Exception as exc:  # noqa: BLE001
        log.debug("日志落库失败（忽略）: %s", exc)


# ═══════════════════════════════════════════════════════════════════
#  决策解析（A~G）
# ═══════════════════════════════════════════════════════════════════

_ACTION_PATTERN = re.compile(r"(?:^|[\s　>*\-])([A-G])[\.、\):：]", re.M)
_REASON_PATTERN = re.compile(r"理由[:：]?\s*(.+)", re.S)


def parse_decision(text: str) -> dict:
    """解析提供者返回的决策文本 → {choice, reason, keyword, links, direction}。"""
    result = {"choice": "", "reason": "", "keyword": "",
              "links": [], "direction": ""}
    if not text:
        return result
    m = _ACTION_PATTERN.search(text)
    if m:
        result["choice"] = m.group(1)
    else:
        # 宽松匹配：文本中第一个孤立 A~G 字母
        m2 = re.search(r"\b([A-G])\b", text)
        if m2:
            result["choice"] = m2.group(1)
    rm = _REASON_PATTERN.search(text)
    if rm:
        result["reason"] = rm.group(1).strip().splitlines()[0][:200]
    else:
        first_line = text.strip().splitlines()[0] if text.strip() else ""
        result["reason"] = first_line[:200]
    if result["choice"] == "D":
        km = re.search(r"关键词\s*[:：]?\s*[\"'“]?([^\n\"'”]+)", text)
        if not km:
            km = re.search(r"搜索\s*[:：]\s*[\"'“]?([^\n\"'”]+)", text)
        if km:
            kw = re.sub(r"^新?关键词\s*[:：]?\s*", "", km.group(1).strip())
            result["keyword"] = kw[:100]
    if result["choice"] == "G":
        result["direction"] = result["reason"][:100]
    if result["choice"] == "B":
        result["links"] = re.findall(r"https?://[^\s\)\]，。'\"]+", text)[:3]
    return result


def rule_based_decision(session: LearningSession,
                        understanding: dict) -> dict:
    """规则回退策略：搜索→点第一个结果→滚动→提取→翻页。

    保证决策提供者未注入时循环仍可运转。
    """
    # 尚未开始 → 搜索第一个子目标关键词
    if session.pages_visited == 0 and not session.current_url:
        kw = _next_keyword(session)
        return {"choice": "D", "keyword": kw,
                "reason": f"规则策略：开始学习，搜索关键词「{kw}」"}
    # 在搜索结果页 → 点击第一个相关链接
    if understanding.get("is_search_result") and understanding.get("links"):
        return {"choice": "B",
                "links": [understanding["links"][0].get("href", "")],
                "reason": "规则策略：点击搜索结果第一条"}
    # 当前页未读完 → 继续滚动
    if not understanding.get("page_fully_read") and session.scroll_depth < 3:
        return {"choice": "A", "reason": "规则策略：继续滚动阅读当前页面"}
    # 读完一页 → 返回搜索结果翻下一篇
    if understanding.get("is_content_page") and session.pages_visited % 3 != 0:
        return {"choice": "C", "reason": "规则策略：本篇已读完，返回搜索结果"}
    # 换关键词翻页
    kw = _next_keyword(session)
    return {"choice": "D", "keyword": kw,
            "reason": f"规则策略：换关键词继续探索「{kw}」"}


def _next_keyword(session: LearningSession) -> str:
    """按子目标顺序取下一个搜索关键词（TC-S-012：仅通用关键词）。"""
    keywords: list[str] = []
    for g in session.sub_goals:
        keywords.extend(g.keywords)
    if not keywords:
        keywords = [session.goal]
    kw = keywords[session.keyword_index % len(keywords)]
    session.keyword_index += 1
    return kw


# 搜索引擎/门户站内域名：搜索页内链接（翻页、相关搜索、导航栏）一律跳过，
# 否则 agent 会永远在搜索结果页打转（实测 20 页预算全部耗在 bing 翻页）。
_SEARCH_HOSTS = ("bing.com", "baidu.com", "sogou.com", "so.com",
                 "google.com", "microsoft.com", "go.microsoft.com")


def _unwrap_search_redirect(href: str) -> str:
    """搜索引擎跳转壳 → 真实 URL。

    bing 结果链接形如 https://www.bing.com/ck/a?...&u=a1<base64url>，
    u 参数去掉 "a1" 前缀后 base64url 解码即真实目标；无法解码时返回 ""
    （调用方跳过该链接）。非跳转链接原样返回。
    """
    if not href:
        return ""
    if "bing.com/ck/" not in href:
        return href
    try:
        u = (parse_qs(urlparse(href).query).get("u") or [""])[0]
        if u.startswith("a1") and len(u) > 4:
            payload = u[2:]
            payload += "=" * (-len(payload) % 4)
            return base64.urlsafe_b64decode(payload).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _is_content_link(href: str) -> bool:
    """判定链接是否为站外内容页（解码跳转壳后排除搜索引擎站内链接）。"""
    real = _unwrap_search_redirect(href)
    if not real or not bs.is_allowed_url(real):
        return False
    host = bs.extract_domain(real)
    if not host:  # 相对路径（站内翻页/导航）一律跳过
        return False
    return not any(h in host for h in _SEARCH_HOSTS)


# ═══════════════════════════════════════════════════════════════════
#  BrowserAgentService（单例）
# ═══════════════════════════════════════════════════════════════════

class BrowserAgentService:
    """AI 浏览 Agent：自主浏览学习完整循环。"""

    def __init__(self) -> None:
        self._sessions: dict[str, LearningSession] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    # ── 目标规划（TASK-032/042）─────────────────────────────────────

    def plan_goal(self, topic: str) -> list[SubGoal]:
        """拆解学习目标为子目标；提供者可用时走 LLM，否则关键词模板回退。"""
        topic = (topic or "").strip()
        if not topic:
            return []
        if _decision_provider is not None:
            prompt = (
                f"请将学习主题「{topic}」拆解为 3~5 个子目标。"
                "每行一个，格式：子目标标题 | 搜索关键词1, 搜索关键词2。"
                "关键词必须是通用词汇，不包含任何个人/项目专名。")
            try:
                t0 = time.time()
                text = _decision_provider(prompt)
                goals = self._parse_plan_response(topic, text)
                if goals:
                    log.info("LLM 目标拆解完成: %d 个子目标（%.1fs）",
                             len(goals), time.time() - t0)
                    return goals
            except Exception as exc:  # noqa: BLE001
                log.warning("LLM 目标拆解失败，回退模板: %s", exc)
        # 关键词模板回退
        return [
            SubGoal(id="sg1", title=f"{topic} 基础概念与定义",
                    keywords=[topic, f"{topic} 是什么", f"{topic} 入门教程"]),
            SubGoal(id="sg2", title=f"{topic} 核心方法与技巧",
                    keywords=[f"{topic} 技巧", f"{topic} 方法"]),
            SubGoal(id="sg3", title=f"{topic} 案例与实战分析",
                    keywords=[f"{topic} 案例分析", f"{topic} 实战经验"]),
            SubGoal(id="sg4", title=f"{topic} 常见问题与进阶",
                    keywords=[f"{topic} 常见问题", f"{topic} 进阶指南"]),
        ]

    @staticmethod
    def _parse_plan_response(topic: str, text: str) -> list[SubGoal]:
        goals: list[SubGoal] = []
        for i, line in enumerate((text or "").splitlines()):
            line = line.strip().lstrip("0123456789.-、) ")
            if not line:
                continue
            if "|" in line:
                title, kw = line.split("|", 1)
                keywords = [k.strip() for k in re.split(r"[,，、]", kw)
                            if k.strip()]
            else:
                title, keywords = line, [line]
            title = title.strip()[:80]
            if title:
                goals.append(SubGoal(id=f"sg{i + 1}", title=title,
                                     keywords=keywords[:4] or [topic]))
            if len(goals) >= 5:
                break
        return goals

    # ── 感知 / 理解 ──────────────────────────────────────────────────

    def perceive_page(self, visual: bool = False) -> dict:
        """感知页面：DOM 优先（<100ms），视觉模式可选（截图+理解提供者）。"""
        browser = get_browser_service()
        t0 = time.time()
        data: dict[str, Any] = {"url": "", "title": "", "text": "",
                                "links": [], "mode": "dom",
                                "screenshot": None}
        try:
            status = browser.get_status()
            data["url"] = status.get("current_url", "")
        except BrowserError:
            pass
        try:
            data["text"] = browser.get_text()
            data["links"] = browser.get_links()
        except BrowserError as exc:
            log.warning("页面感知失败: %s", exc.message)
        # TASK-052：随感知同步采集 DOM 结构特征（单次 JS 往返），
        # 供 classify_page 纯函数化使用（分类不再产生额外浏览器操作，
        # 否则每次分类都触发 ≥1s 节流，快速路径无法达标 <500ms）。
        try:
            feats = browser.execute_js(BrowserAgentService._CLASSIFY_JS)
            if isinstance(feats, dict):
                data["dom_features"] = feats
        except Exception:  # noqa: BLE001 - 特征缺失时分类走启发式回退
            pass
        try:
            tabs = browser.list_tabs()
            for t in tabs:
                if t.get("active"):
                    data["title"] = t.get("title", "")
                    data["url"] = t.get("url", data["url"])
        except BrowserError:
            pass
        if visual:
            try:
                data["screenshot"] = browser.screenshot()
                data["mode"] = "visual"
            except BrowserError:
                pass
        log.debug("感知完成（%s 模式，%.0fms）", data["mode"],
                  (time.time() - t0) * 1000)
        return data

    def understand_page(self, page_data: dict) -> dict:
        """理解页面：提供者可用时走模型，否则启发式摘要。"""
        text = page_data.get("text", "") or ""
        links = page_data.get("links", []) or []
        url = page_data.get("url", "") or ""
        summary = ""
        if _page_understanding_provider is not None and text:
            prompt = (f"请用 3 句话概括以下网页内容，并说明其与学习主题的"
                      f"相关性：\n{text[:3000]}")
            try:
                summary = str(_page_understanding_provider(prompt))[:500]
            except Exception as exc:  # noqa: BLE001
                log.warning("页面理解提供者调用失败（回退启发式）: %s", exc)
        if not summary:
            summary = re.sub(r"\s+", " ", text).strip()[:300]
        engine_hosts = ("bing.com/search", "baidu.com/s", "sogou.com/web",
                        "so.com/s", "google.com/search")
        is_search = any(h in url for h in engine_hosts)
        return {
            "summary": summary,
            "is_search_result": is_search,
            "is_content_page": bool(text) and not is_search,
            "page_fully_read": len(text) < 1500,   # 短页一次读完
            "links": links,
            "text_len": len(text),
        }

    # ── 页面复杂度分类（TASK-052 v2.3.1）────────────────────────────

    # 复杂页特征域名（社交媒体/Web App）：命中直接判复杂
    _COMPLEX_HOSTS = (
        "weibo.com", "x.com", "twitter.com", "facebook.com", "instagram.com",
        "douyin.com", "xiaohongshu.com", "reddit.com", "discord.com",
        "taobao.com", "jd.com", "tmall.com",
    )
    # 复杂页特征路径（后台管理/控制台）
    _COMPLEX_PATHS = ("/admin", "/dashboard", "/console", "/manage",
                      "/workspace", "/settings")

    # DOM 结构特征采集脚本（单次 execute_js 往返，<100ms）
    _CLASSIFY_JS = """() => {
  const d = document;
  const q = (s) => d.querySelectorAll(s).length;
  const htmlLen = d.documentElement ? d.documentElement.outerHTML.length : 0;
  const textLen = (d.body && d.body.innerText) ? d.body.innerText.length : 0;
  return {
    forms: q('form'),
    inputs: q('input,select,textarea'),
    buttons: q('button,[role="button"],input[type="button"],input[type="submit"]'),
    onclick: q('[onclick]'),
    iframes: q('iframe'),
    links: q('a[href]'),
    html_len: htmlLen,
    text_len: textLen
  };
}"""

    def classify_page(self, page_data: dict) -> dict:
        """轻量级页面分类器（TASK-052）：规则评分 → simple/complex。

        特征（规格要求）：
          - DOM 结构：<form> 数量、<input> 数量、<button> 数量
          - 内容密度：文本与 HTML 标签的比例（text_len / html_len）
          - 样式/交互特征：onclick 处理器数量、iframe 数量
          - 站点特征：社交媒体/Web App 域名、/admin /dashboard 等路径
        返回 {"level","score","features","reason"}。
        纯函数：只消费 page_data["dom_features"]（感知阶段已采集），
        不做任何浏览器调用（避免触发操作节流拖慢快速路径）。
        特征缺失时按当前文本/链接启发式回退（保持现有运行行为）。
        """
        url = (page_data.get("url") or "").lower()
        host = bs.extract_domain(url) or ""
        # 站点特征：已知社交/电商/App 域名与后台路径 → 直接复杂
        if any(h in host for h in self._COMPLEX_HOSTS):
            return {"level": "complex", "score": 99.0, "features": {},
                    "reason": f"复杂站点域名: {host}"}
        if any(p in url for p in self._COMPLEX_PATHS):
            return {"level": "complex", "score": 99.0, "features": {},
                    "reason": "后台管理路径特征"}

        feats: dict[str, Any] = {}
        raw = page_data.get("dom_features")
        if isinstance(raw, dict):
            feats = {k: int(v) for k, v in raw.items()
                     if isinstance(v, (int, float))}

        if not feats:
            # 回退：文本充足且链接稀疏 → 简单（与现运行行为一致）
            text_len = len(page_data.get("text") or "")
            link_count = len(page_data.get("links") or [])
            simple = text_len >= 500 and link_count <= max(20, text_len // 100)
            return {"level": "simple" if simple else "complex",
                    "score": 0.0 if simple else COMPLEX_SCORE_THRESHOLD,
                    "features": {"text_len": text_len, "links": link_count},
                    "reason": "no_dom_features_fallback"}

        score = 0.0
        forms = feats.get("forms", 0)
        inputs = feats.get("inputs", 0)
        buttons = feats.get("buttons", 0)
        onclick = feats.get("onclick", 0)
        iframes = feats.get("iframes", 0)
        links = feats.get("links", 0)
        html_len = max(feats.get("html_len", 0), 1)
        text_len = feats.get("text_len", 0)
        # DOM 结构
        score += 2.0 if forms >= 2 else (0.5 if forms == 1 else 0.0)
        score += 2.0 if inputs >= 8 else (1.0 if inputs >= 3 else 0.0)
        score += 1.5 if buttons >= 15 else (0.75 if buttons >= 8 else 0.0)
        # 交互/样式特征
        score += 1.5 if onclick >= 10 else (0.75 if onclick >= 3 else 0.0)
        score += 1.0 if iframes >= 3 else 0.0
        # 内容密度：文本/HTML 比极低且标记量大 → App 化页面
        density = text_len / html_len
        if density < 0.02 and html_len > 200_000:
            score += 1.5
        # 导航密度：每千字链接数 >40 → 导航型/App 型
        if links / max(text_len / 1000.0, 1.0) > 40:
            score += 1.0
        # 强简单信号：正文充足且无表单
        if text_len >= 800 and forms == 0:
            score -= 1.0
        level = "complex" if score >= COMPLEX_SCORE_THRESHOLD else "simple"
        return {"level": level, "score": round(score, 2),
                "features": feats,
                "reason": f"规则评分 {score:.1f}（阈值 {COMPLEX_SCORE_THRESHOLD}）"}

    # ── 决策（<5s，提供者 + 规则回退 + 循环强制换向）──────────────────

    def decide_action(self, session: LearningSession,
                      understanding: dict) -> dict:
        """决策下一步操作（A~G）。提供者超时/失败时回退规则策略。"""
        t0 = time.time()
        action: dict = {}
        if _decision_provider is not None:
            prompt = self._build_decision_prompt(session, understanding)
            try:
                result_holder: dict[str, str] = {}

                def _call() -> None:
                    try:
                        result_holder["text"] = str(_decision_provider(prompt))
                    except Exception as exc:  # noqa: BLE001
                        result_holder["error"] = str(exc)

                th = threading.Thread(target=_call, daemon=True)
                th.start()
                th.join(DECISION_TIMEOUT_S)
                if th.is_alive():
                    log.warning("决策提供者超时（>%.0fs），回退规则策略",
                                DECISION_TIMEOUT_S)
                elif "text" in result_holder:
                    action = parse_decision(result_holder["text"])
                else:
                    log.warning("决策提供者异常（%s），回退规则策略",
                                result_holder.get("error", "unknown"))
            except Exception as exc:  # noqa: BLE001
                log.warning("决策调用失败（回退规则策略）: %s", exc)
        if not action.get("choice"):
            action = rule_based_decision(session, understanding)
        return self._post_decision(session, action, t0,
                                   timeout=DECISION_TIMEOUT_S)

    # ── 分级决策（TASK-052 v2.3.1）───────────────────────────────────

    def decide_action_graded(self, session: LearningSession,
                             understanding: dict,
                             page_data: dict) -> dict:
        """分级决策入口：classify_page → 简单页快速路径 / 复杂页慢速路径。

        - 简单页（博客/新闻/文档）：跳过截图与视觉理解，仅用 DOM 文本走
          快速决策提供者（Qwen3-4B）或规则回退，目标 <500ms。
        - 复杂页（Web App/后台/社媒）：维持 DOM+视觉 双模慢速决策流程。
        """
        cls = self.classify_page(page_data)
        understanding["page_class"] = cls["level"]
        if cls["level"] == "simple":
            session.path_stats["fast"] += 1
            action = self._decide_fast(session, understanding)
            action["decision_path"] = "fast"
        else:
            session.path_stats["slow"] += 1
            log.debug("复杂页面（%s），走 DOM+视觉 慢速决策", cls["reason"])
            # 慢速路径：补充视觉感知（截图），供视觉理解/决策提供者使用
            if page_data.get("screenshot") is None:
                try:
                    page_data["screenshot"] = \
                        get_browser_service().screenshot()
                    page_data["mode"] = "visual"
                except Exception as exc:  # noqa: BLE001 - 截图失败不阻断
                    log.debug("复杂页截图失败（继续 DOM 决策）: %s", exc)
            action = self.decide_action(session, understanding)
            action["decision_path"] = "slow"
        action["page_class"] = cls["level"]
        return action

    def _decide_fast(self, session: LearningSession,
                     understanding: dict) -> dict:
        """快速路径决策（TASK-052）：轻量提供者（Qwen3-4B）+ 规则回退。

        目标 <500ms：硬超时 2s 兜底，超时/失败立即回退规则策略（<1ms）。
        """
        t0 = time.time()
        action: dict = {}
        if _fast_decision_provider is not None:
            prompt = self._build_decision_prompt(session, understanding)
            try:
                result_holder: dict[str, str] = {}

                def _call() -> None:
                    try:
                        result_holder["text"] = str(
                            _fast_decision_provider(prompt))
                    except Exception as exc:  # noqa: BLE001
                        result_holder["error"] = str(exc)

                th = threading.Thread(target=_call, daemon=True)
                th.start()
                th.join(FAST_DECISION_TIMEOUT_S)
                if th.is_alive():
                    log.warning("快速决策提供者超时（>%.1fs），回退规则策略",
                                FAST_DECISION_TIMEOUT_S)
                elif "text" in result_holder:
                    action = parse_decision(result_holder["text"])
                else:
                    log.warning("快速决策提供者异常（%s），回退规则策略",
                                result_holder.get("error", "unknown"))
            except Exception as exc:  # noqa: BLE001
                log.warning("快速决策调用失败（回退规则策略）: %s", exc)
        if not action.get("choice"):
            action = rule_based_decision(session, understanding)
        out = self._post_decision(session, action, t0,
                                  timeout=FAST_DECISION_TIMEOUT_S)
        elapsed = time.time() - t0
        if elapsed > FAST_DECISION_TARGET_S:
            log.warning("简单页决策耗时 %.0fms 超过目标 500ms",
                        elapsed * 1000)
        return out

    def _post_decision(self, session: LearningSession, action: dict,
                       t0: float, timeout: float = DECISION_TIMEOUT_S
                       ) -> dict:
        """决策后处理：循环检测强制换向 + 延迟标注（两条路径共用）。"""
        # 循环检测：连续 3 次相同操作 → 强制换方向（TC-A-002）
        loop = self.detect_loop(session)
        if loop == "same_action_x3":
            forced_kw = _next_keyword(session)
            action = {"choice": "D", "keyword": forced_kw,
                      "reason": f"检测到循环，切换方向：搜索新关键词「{forced_kw}」"}
            session.log_entry("loop_detected", "连续3次相同操作",
                              "强制换方向")
        elapsed = time.time() - t0
        if elapsed > timeout:
            log.warning("决策耗时 %.1fs（目标 <%.0fs）", elapsed, timeout)
        action["latency_s"] = round(elapsed, 2)
        return action

    def _build_decision_prompt(self, session: LearningSession,
                               understanding: dict) -> str:
        elapsed = ((time.time() - session.started_at) / 60.0
                   if session.started_at else 0.0)
        links = understanding.get("links", [])[:10]
        links_str = "\n".join(
            f"  - {l.get('text', '')[:40]}: {l.get('href', '')[:80]}"
            for l in links)
        return DECISION_PROMPT.format(
            goal=session.goal,
            learned_summary="；".join(session.learned_summary[-5:]) or "（暂无）",
            learned_count=session.knowledge_extracted,
            page_title=understanding.get("summary", "")[:80],
            page_content_summary=understanding.get("summary", "")[:300],
            page_links=links_str or "（无）",
            link_count=len(understanding.get("links", [])),
            pages_visited=session.pages_visited,
            max_pages=session.budget.max_pages,
            elapsed=f"{elapsed:.1f}分钟",
            budget=f"{session.budget.max_time_minutes}分钟",
            page_fully_read=("是" if understanding.get("page_fully_read")
                             else "否"),
        )

    # ── 循环检测（TC-A-002）──────────────────────────────────────────

    def record_action(self, session: LearningSession, action_key: str) -> None:
        """记录操作（滑动窗口最近 10 次）并累计操作数。"""
        session.recent_actions.append(action_key)
        session.operation_count += 1

    def detect_loop(self, session: LearningSession) -> Optional[str]:
        """循环检测：
        - 最近 10 次中连续 3 次相同操作 → "same_action_x3"
        - 操作数超过 200 次上限 → "op_limit"
        """
        if session.operation_count >= MAX_OPERATIONS:
            return "op_limit"
        acts = list(session.recent_actions)
        if (len(acts) >= SAME_ACTION_THRESHOLD
                and acts[-1] == acts[-2] == acts[-3]):
            return "same_action_x3"
        return None

    def note_page_visit(self, session: LearningSession, url: str) -> bool:
        """记录页面访问；同一页面访问 >3 次返回 True（应跳过）。"""
        if not url:
            return False
        count = session.page_visit_counts.get(url, 0) + 1
        session.page_visit_counts[url] = count
        return count > PAGE_VISIT_THRESHOLD

    # ── 执行 ──────────────────────────────────────────────────────────

    def execute_action(self, session: LearningSession,
                       action: dict) -> dict:
        """执行决策动作，返回 {ok, detail}。所有异常转为日志友好的结果。"""
        browser = get_browser_service()
        choice = action.get("choice", "")
        reason = action.get("reason", "")
        result = {"ok": False, "detail": ""}
        try:
            browser.ensure_ai_control()
            if choice == "A":      # 继续滚动
                browser.scroll("down", 700)
                session.scroll_depth += 1
                result = {"ok": True, "detail": "已向下滚动"}
            elif choice == "B":    # 点击链接
                result = self._execute_click_link(session, action)
            elif choice == "C":    # 返回上一页
                info = browser.go_back()
                session.scroll_depth = 0
                result = {"ok": True,
                          "detail": f"返回上一页: {info.get('url', '')[:80]}"}
            elif choice == "D":    # 搜索新关键词
                keyword = (action.get("keyword") or "").strip()
                if not keyword:
                    keyword = _next_keyword(session)
                result = self._execute_search(session, keyword)
            elif choice == "E":    # 打开新标签页
                tab_id = browser.new_tab()
                result = {"ok": True, "detail": f"新标签页: {tab_id}"}
            elif choice == "F":    # 结束学习
                session.stop_requested = True
                session.stop_reason = "goal_achieved"
                result = {"ok": True, "detail": "Agent 判定目标已达成"}
            elif choice == "G":    # 调整方向
                new_goal = (action.get("direction") or "").strip()
                if new_goal:
                    session.goal = new_goal
                kw = _next_keyword(session)
                result = self._execute_search(session, kw)
                result["detail"] = f"调整方向后搜索「{kw}」"
            else:
                result = {"ok": False, "detail": f"无效决策: {choice!r}"}
        except (InvalidUrlError, BlacklistedDomainError) as exc:
            result = {"ok": False, "detail": f"安全拦截: {exc.message}"}
        except BrowserUnavailableError as exc:
            result = {"ok": False, "detail": f"浏览器不可用: {exc.message}"}
            session.stop_requested = True
            session.stop_reason = "browser_unavailable"
        except BrowserError as exc:
            result = {"ok": False, "detail": f"浏览器错误: {exc.message}"}
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "detail": f"执行异常: {exc}"}
        entry = session.log_entry(f"action_{choice or '?'}", reason,
                                  result["detail"])
        persist_session_log(session.session_id, entry)
        _broadcast({"type": "learn_action", "session_id": session.session_id,
                    "action": choice, "reason": reason,
                    "result": result["detail"],
                    "decision_path": action.get("decision_path", ""),
                    "page_class": action.get("page_class", ""),
                    "latency_s": action.get("latency_s", 0),
                    "ts": time.time()})
        return result

    def _execute_search(self, session: LearningSession,
                        keyword: str) -> dict:
        """构造搜索 URL 直接导航（不填写搜索表单）。"""
        settings = get_learning_settings()
        engine = str(settings.get("search_engine", DEFAULT_SEARCH_ENGINE))
        url = build_search_url(keyword, engine)
        browser = get_browser_service()
        info = browser.navigate(url)
        session.last_search_url = url
        session.current_url = info.get("url", url)
        session.scroll_depth = 0
        return {"ok": True, "detail": f"搜索「{keyword}」({engine})"}

    def _execute_click_link(self, session: LearningSession,
                            action: dict) -> dict:
        """点击链接：优先决策给出的 URL，其次页面第一个内容链接。

        统一经 _is_content_link 过滤（跳转壳解码 + 搜索引擎站内链接剔除），
        防止在搜索结果页内翻页死循环。
        """
        browser = get_browser_service()
        # 决策给出的链接在前，页面全部链接作后备（决策链接被过滤光时仍可命中）
        candidates = list(action.get("links") or [])
        candidates += [l.get("href", "") for l in browser.get_links()]
        # 选择策略：跳过撞墙域名；优先未访问页面，退而允许 <阈值 次的已访问页
        target = ""
        fallback = ""
        for href in candidates:
            if not _is_content_link(href):
                continue
            real = _unwrap_search_redirect(href)
            if bs.extract_domain(real) in session.wall_hosts:
                continue
            visits = session.page_visit_counts.get(real, 0)
            if visits == 0:
                target = real
                break
            if visits < PAGE_VISIT_THRESHOLD and not fallback:
                fallback = real
        target = target or fallback
        if not target:
            return {"ok": False, "detail": "页面无可用内容链接"}
        # 同页访问 >3 次 → 跳过（TC-A-002）
        if self.note_page_visit(session, target):
            return {"ok": False,
                    "detail": f"该页面已访问超过{PAGE_VISIT_THRESHOLD}次，跳过"}
        info = browser.navigate(target)
        session.pages_visited += 1
        session.current_url = info.get("url", target)
        session.scroll_depth = 0
        return {"ok": True,
                "detail": f"打开页面({session.pages_visited}): "
                          f"{session.current_url[:80]}"}

    # ── 知识提取（契约：knowledge_service.process_page）────────────────

    def extract_knowledge(self, session: LearningSession,
                          page_data: dict) -> list:
        """从页面提取知识并入库（另一 agent 的 knowledge_service）。"""
        text = page_data.get("text", "") or ""
        url = page_data.get("url", "") or session.current_url
        if len(text.strip()) < 100:
            return []
        ks = _resolve_knowledge_service()
        if ks is None:
            log.debug("knowledge_service 不可用，跳过知识提取")
            return []
        try:
            items = ks.process_page(text, session.goal, source_url=url)
            items = list(items or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("知识提取失败（不中断学习）: %s", exc)
            return []
        session.knowledge_extracted += len(items)
        for it in items[:3]:
            summary = ""
            if isinstance(it, dict):
                summary = str(it.get("title") or it.get("concept")
                              or it.get("question") or "")[:60]
            if summary:
                session.learned_summary.append(summary)
        return items

    # ── 评估反馈 ──────────────────────────────────────────────────────

    def evaluate_progress(self, session: LearningSession,
                          new_knowledge: list) -> dict:
        """评估学习进度：目标覆盖度 / 知识质量 / 预算消耗 → 继续/换方向/结束。"""
        # 覆盖度：子目标关键词在当前页面命中即视为该子目标有进展
        if session.sub_goals:
            done = sum(1 for g in session.sub_goals if g.done)
            session.coverage = done / len(session.sub_goals)
        elapsed_min = ((time.time() - session.started_at) / 60.0
                       if session.started_at else 0.0)
        decision = "continue"
        if session.coverage >= COVERAGE_DONE:
            decision = "stop"
        elif not new_knowledge and session.pages_visited > 3:
            decision = "change_direction"
        evaluation = {
            "coverage": round(session.coverage, 3),
            "knowledge_total": session.knowledge_extracted,
            "new_knowledge": len(new_knowledge),
            "budget_time_used": round(elapsed_min, 2),
            "budget_pages_used": session.pages_visited,
            "decision": decision,
        }
        if decision == "change_direction":
            session.log_entry("evaluate", "连续多页无新知识", "建议换方向")
        return evaluation

    # ── 终止条件（TASK-032）───────────────────────────────────────────

    def should_stop(self, session: LearningSession) -> Optional[str]:
        """返回终止原因；None 表示继续。"""
        if session.stop_requested:
            return session.stop_reason or "user_stop"
        if session.resource_preempted:
            return "preempted"
        elapsed_min = ((time.time() - session.started_at) / 60.0
                       if session.started_at else 0.0)
        if elapsed_min >= SESSION_HARD_CAP_MINUTES:
            return "hard_time_cap"           # 单会话 >60 分钟
        if session.coverage > COVERAGE_DONE:
            return "goal_achieved"           # 覆盖度 >90%
        if session.pages_visited >= session.budget.max_pages:
            return "budget_pages"            # 页数预算用尽
        if elapsed_min >= session.budget.max_time_minutes:
            return "budget_time"             # 时间预算用尽
        if session.consecutive_wall_hits >= WALL_CONSECUTIVE_LIMIT:
            return "all_login_walls"         # 全部来源需登录（TC-A-004）
        if self.detect_loop(session) == "op_limit":
            return "op_limit"                # 操作数上限（循环防护）
        return None

    # ── 检查点（TASK-040）─────────────────────────────────────────────

    def should_checkpoint(self, session: LearningSession) -> bool:
        return (time.time() - session.last_checkpoint_at
                >= CHECKPOINT_INTERVAL_S)

    def save_checkpoint(self, session: LearningSession) -> Path:
        """保存检查点到 data/checkpoints/{session_id}.json（原子写）。"""
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session.session_id,
            "topic_id": session.topic_id,
            "goal": session.goal,
            "sub_goals": [g.to_dict() for g in session.sub_goals],
            "budget": session.budget.to_dict(),
            "status": session.status,
            "pages_visited": session.pages_visited,
            "knowledge_extracted": session.knowledge_extracted,
            "coverage": session.coverage,
            "current_url": session.current_url,
            "operation_count": session.operation_count,
            "page_visit_counts": dict(session.page_visit_counts),
            "keyword_index": session.keyword_index,
            "learned_summary": list(session.learned_summary)[-20:],
            "stop_reason": session.stop_reason,
            "saved_at": time.time(),
        }
        path = CHECKPOINT_DIR / f"{session.session_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)
        session.last_checkpoint_at = time.time()
        session.checkpoint = payload
        log.info("检查点已保存: %s（页数=%d 知识=%d 覆盖=%.0f%%）",
                 path.name, session.pages_visited,
                 session.knowledge_extracted, session.coverage * 100)
        return path

    def restore_checkpoint(self, session_id: str) -> Optional[LearningSession]:
        """从检查点恢复会话（<5 秒，断电恢复）。"""
        path = CHECKPOINT_DIR / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("检查点读取失败: %s", exc)
            return None
        session = LearningSession(
            session_id=str(payload.get("session_id", session_id)),
            topic_id=str(payload.get("topic_id", "")),
            goal=str(payload.get("goal", "")),
            sub_goals=[SubGoal.from_dict(g)
                       for g in payload.get("sub_goals", []) or []],
            budget=LearningBudget.from_dict(payload.get("budget")),
            status="restored",
            pages_visited=int(payload.get("pages_visited", 0) or 0),
            knowledge_extracted=int(
                payload.get("knowledge_extracted", 0) or 0),
            checkpoint=payload,
        )
        session.coverage = float(payload.get("coverage", 0.0) or 0.0)
        session.current_url = str(payload.get("current_url", ""))
        session.operation_count = int(payload.get("operation_count", 0) or 0)
        session.page_visit_counts = dict(
            payload.get("page_visit_counts", {}) or {})
        session.keyword_index = int(payload.get("keyword_index", 0) or 0)
        session.learned_summary = list(
            payload.get("learned_summary", []) or [])
        session.stop_reason = str(payload.get("stop_reason", ""))
        session.log_entry("restore", "从检查点恢复",
                          f"页数={session.pages_visited} "
                          f"知识={session.knowledge_extracted}")
        return session

    # ── 安全墙检测与离开（TC-S-001/007/008, TC-A-004）──────────────────

    def check_walls_and_leave(self, session: LearningSession) -> Optional[str]:
        """检测登录墙/验证码/付费墙；命中则立即离开并返回墙类型。"""
        browser = get_browser_service()
        wall = ""
        wall_log = ""
        try:
            if browser.detect_captcha():
                wall, wall_log = "captcha", "检测到验证码，离开"
            elif browser.detect_login_wall():
                wall, wall_log = "login_wall", "检测到登录墙，离开"
            elif browser.detect_paywall():
                wall, wall_log = "paywall", "检测到付费墙，离开"
        except BrowserError:
            return None
        if not wall:
            session.consecutive_wall_hits = 0
            return None
        session.wall_hits += 1
        session.consecutive_wall_hits += 1
        # 记住撞墙域名：本会话内链接选择直接跳过该站
        wall_host = bs.extract_domain(session.current_url or "")
        if wall_host:
            session.wall_hosts[wall_host] = wall
        entry = session.log_entry("wall_detected", wall_log,
                                  f"立即离开（第{session.wall_hits}次）")
        persist_session_log(session.session_id, entry)
        log.warning("%s: %s", wall_log, session.current_url[:100])
        # 立即离开：返回上一页；失败则回搜索页/开新搜索
        try:
            browser.go_back()
        except BrowserError:
            pass
        return wall

    # ── 主循环（TASK-032）──────────────────────────────────────────────

    def run_learning_session(self, session: LearningSession) -> None:
        """主循环：感知→理解→决策→执行→提取→评估（每 5 分钟检查点）。

        TASK-054：浏览器实例从进程池获取（预热后 acquire <1s），
        结束归还时由池执行会话隔离清理（Cookie/缓存清除+预建干净快照）；
        池获取失败时回退原直取路径（优雅降级）。
        """
        from .browser_pool import get_browser_pool
        pool = get_browser_pool()
        pooled = False
        browser = None
        try:
            browser = pool.acquire(timeout=30.0)
            pooled = True
        except BrowserError as exc:
            log.warning("进程池获取浏览器失败（回退直取路径）: %s",
                        exc.message)
            browser = get_browser_service()
        session.status = "running"
        session.started_at = time.time()
        session.last_checkpoint_at = time.time()
        session.log_entry("session_start", f"开始学习主题「{session.goal}」",
                          f"预算: {session.budget.max_time_minutes}分钟/"
                          f"{session.budget.max_pages}页")
        _broadcast({"type": "learn_session_start",
                    "session_id": session.session_id, "goal": session.goal,
                    "ts": time.time()})
        if not browser.is_running:
            if not browser.init():
                session.status = "interrupted"
                session.stop_reason = "browser_unavailable"
                session.log_entry("session_abort", "浏览器初始化失败",
                                  browser.unavailable_reason or "unknown")
                if pooled:
                    pool.release(browser)
                self._finalize_session(session)
                return
        try:
            browser.begin_session()
        except BrowserError as exc:
            session.status = "interrupted"
            session.stop_reason = "browser_unavailable"
            session.log_entry("session_abort", "无法创建浏览上下文",
                              exc.message)
            if pooled:
                pool.release(browser)
            self._finalize_session(session)
            return
        try:
            while True:
                stop = self.should_stop(session)
                if stop:
                    session.stop_reason = stop
                    break
                # 暂停/用户接管：挂起等待
                if session.pause_requested or browser.is_user_takeover():
                    session.status = "paused"
                    time.sleep(1.0)
                    continue
                session.status = "running"
                # 用户抢占（创作活跃）：由调度器置位
                if session.resource_preempted:
                    session.stop_reason = "preempted"
                    break
                try:
                    # 0. 流量预算（TC-S-009）：先同步增量再判定上限
                    self._sync_traffic(session)
                    settings = get_learning_settings()
                    limit_mb = float(settings.get(
                        "daily_traffic_limit_mb", 50) or 50)
                    if get_traffic_today() >= limit_mb * 1048576:
                        session.log_entry(
                            "traffic_limit", "今日流量已达上限",
                            f"超过 {limit_mb:.0f}MB，学习自动停止")
                        session.stop_reason = "traffic_limit"
                        break
                    # 1. 感知
                    page_data = self.perceive_page()
                    session.current_url = page_data.get(
                        "url", session.current_url)
                    # 2. 墙检测（命中即离开，continue 重新决策）
                    wall = self.check_walls_and_leave(session)
                    if wall:
                        continue
                    # 3. 理解
                    understanding = self.understand_page(page_data)
                    # 4. 分级决策（TASK-052：classify_page → 快速/慢速路径）
                    action = self.decide_action_graded(
                        session, understanding, page_data)
                    # 5. 执行
                    self.record_action(
                        session, f"{action.get('choice', '?')}"
                                 f":{action.get('keyword', '')[:20]}")
                    self.execute_action(session, action)
                    # 6. 知识提取（内容页才提取）
                    new_knowledge: list = []
                    if understanding.get("is_content_page"):
                        new_knowledge = self.extract_knowledge(
                            session, page_data)
                        self._mark_subgoals(session, page_data)
                    # 7. 评估
                    self.evaluate_progress(session, new_knowledge)
                    # 8. 检查点（每 5 分钟）
                    if self.should_checkpoint(session):
                        self.save_checkpoint(session)
                    _broadcast({"type": "learn_progress",
                                "session_id": session.session_id,
                                "pages_visited": session.pages_visited,
                                "knowledge_extracted":
                                    session.knowledge_extracted,
                                "coverage": session.coverage,
                                "ts": time.time()})
                except BrowserUnavailableError as exc:
                    session.log_entry("browser_lost", "浏览器不可用",
                                      exc.message)
                    session.stop_reason = "browser_unavailable"
                    break
                except Exception as exc:  # noqa: BLE001 - 单步失败不中断会话
                    log.exception("学习循环单步异常: %s", exc)
                    session.log_entry("step_error", type(exc).__name__,
                                      str(exc)[:200])
                    time.sleep(1.0)
        finally:
            try:
                self.save_checkpoint(session)
            except Exception:  # noqa: BLE001
                pass
            if pooled:
                # 归还进程池：隔离清理（Cookie/缓存清除）+ 预建干净快照
                pool.release(browser)
            else:
                try:
                    browser.end_session()
                except Exception:  # noqa: BLE001
                    pass
            self._finalize_session(session)

    @staticmethod
    def _sync_traffic(session: LearningSession) -> None:
        """把浏览器拦截层统计的流量增量同步到每日流量计数（TC-S-009）。"""
        try:
            total = int(get_browser_service()
                        .get_traffic_stats().get("total_bytes", 0))
        except Exception:  # noqa: BLE001
            return
        delta = total - session.last_traffic_bytes
        if delta > 0:
            session.last_traffic_bytes = total
            record_traffic(delta)

    @staticmethod
    def _mark_subgoals(session: LearningSession, page_data: dict) -> None:
        """子目标关键词在页面命中 → 标记完成（覆盖度统计用）。"""
        text = (page_data.get("text", "") or "").lower()
        if not text:
            return
        for g in session.sub_goals:
            if g.done:
                continue
            hits = sum(1 for kw in g.keywords if kw and kw.lower() in text)
            if hits >= max(1, len(g.keywords) // 2):
                g.done = True
                session.log_entry("subgoal_done", g.title,
                                  "关键词命中，标记完成")

    def _finalize_session(self, session: LearningSession) -> None:
        """会话收尾：状态落库 + 完成通知。"""
        reason = session.stop_reason or "completed"
        if session.status not in ("completed", "interrupted"):
            session.status = ("completed" if reason in (
                "goal_achieved", "budget_pages", "budget_time",
                "hard_time_cap", "traffic_limit", "all_login_walls",
                "op_limit", "user_stop")
                else "interrupted")
        session.log_entry("session_end", f"会话结束: {reason}",
                          f"页数={session.pages_visited} "
                          f"知识={session.knowledge_extracted} "
                          f"覆盖={session.coverage:.0%}")
        _broadcast({"type": "learn_session_end",
                    "session_id": session.session_id, "reason": reason,
                    "report": self.build_report(session), "ts": time.time()})
        log.info("学习会话结束: %s reason=%s pages=%d knowledge=%d",
                 session.session_id[:8], reason, session.pages_visited,
                 session.knowledge_extracted)

    def build_report(self, session: LearningSession) -> dict:
        """生成学习完成报告（TASK-044 数据基础）。"""
        elapsed_min = ((time.time() - session.started_at) / 60.0
                       if session.started_at else 0.0)
        reason_text = {
            "goal_achieved": "目标已达成",
            "budget_pages": "页数预算用尽",
            "budget_time": "时间预算用尽",
            "hard_time_cap": "达到单次会话 60 分钟上限",
            "user_stop": "用户手动停止",
            "preempted": "资源被抢占（用户开始创作）",
            "all_login_walls": "所有来源需要登录",
            "op_limit": "检测到异常循环，已停止",
            "traffic_limit": "今日流量已达上限",
            "browser_unavailable": "浏览器不可用",
        }.get(session.stop_reason, session.stop_reason or "正常结束")
        return {
            "session_id": session.session_id,
            "topic_id": session.topic_id,
            "goal": session.goal,
            "status": session.status,
            "stop_reason": session.stop_reason,
            "stop_reason_text": reason_text,
            "pages_visited": session.pages_visited,
            "knowledge_extracted": session.knowledge_extracted,
            "coverage": round(session.coverage, 3),
            "elapsed_minutes": round(elapsed_min, 2),
            "sub_goals": [g.to_dict() for g in session.sub_goals],
            "wall_hits": session.wall_hits,
            "operation_count": session.operation_count,
            "logs": list(session.logs)[-100:],
        }

    # ── 会话管理（API 层调用）──────────────────────────────────────────

    def create_session(self, topic_id: str, goal: str,
                       budget: Optional[dict] = None,
                       session_id: Optional[str] = None) -> LearningSession:
        """创建会话对象（不启动线程）。"""
        session = LearningSession(
            session_id=session_id or uuid.uuid4().hex,
            topic_id=topic_id,
            goal=goal,
            sub_goals=self.plan_goal(goal),
            budget=LearningBudget.from_dict(budget),
        )
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def start_session(self, session: LearningSession) -> None:
        """在后台守护线程中运行会话主循环（不阻塞调用方）。"""
        _ensure_llm_extractor()
        with self._lock:
            old = self._threads.get(session.session_id)
            if old is not None and old.is_alive():
                raise LearningError(ERR_SESSION_RUNNING,
                                    "该学习会话已在运行中")
            thread = threading.Thread(
                target=self.run_learning_session, args=(session,),
                name=f"LearnSession-{session.session_id[:8]}",
                daemon=True)
            self._threads[session.session_id] = thread
            thread.start()

    def pause_session(self, session_id: str) -> LearningSession:
        session = self._require_session(session_id)
        if session.status not in ("running", "restored"):
            raise LearningError(ERR_SESSION_STATE,
                                f"当前状态 {session.status} 不允许暂停")
        session.pause_requested = True
        session.status = "paused"
        session.log_entry("pause", "用户暂停", "已暂停")
        return session

    def resume_session(self, session_id: str) -> LearningSession:
        session = self._require_session(session_id)
        if session.status != "paused":
            raise LearningError(ERR_SESSION_STATE,
                                f"当前状态 {session.status} 不允许恢复")
        session.pause_requested = False
        session.status = "running"
        session.log_entry("resume", "用户恢复", "继续学习")
        return session

    def stop_session(self, session_id: str) -> LearningSession:
        session = self._require_session(session_id)
        session.stop_requested = True
        session.stop_reason = "user_stop"
        session.pause_requested = False
        session.log_entry("stop", "用户停止", "等待循环退出")
        return session

    def preempt_session(self, session_id: str) -> None:
        """资源抢占（创作活跃 → 学习停止，TASK-014 联动）。"""
        session = self._sessions.get(session_id)
        if session is not None:
            session.resource_preempted = True
            session.pause_requested = False

    def get_session(self, session_id: str) -> Optional[LearningSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def _require_session(self, session_id: str) -> LearningSession:
        session = self.get_session(session_id)
        if session is None:
            raise LearningError(ERR_SESSION_NOT_FOUND,
                                "学习会话不存在",
                                detail={"session_id": session_id})
        return session

    def list_sessions(self) -> list[LearningSession]:
        with self._lock:
            return list(self._sessions.values())

    def active_session(self) -> Optional[LearningSession]:
        with self._lock:
            for s in self._sessions.values():
                if s.status in ("running", "paused"):
                    return s
        return None


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_agent: Optional[BrowserAgentService] = None
_agent_lock = threading.Lock()


def get_browser_agent_service() -> BrowserAgentService:
    """获取 AI 浏览 Agent 服务单例。"""
    global _agent
    if _agent is None:
        with _agent_lock:
            if _agent is None:
                _agent = BrowserAgentService()
    return _agent
