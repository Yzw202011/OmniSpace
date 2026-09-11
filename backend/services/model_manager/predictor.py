"""OmniSpace AI v2.3 功能切换 ML 预测模块（TASK-011 / 规格 §3.4 ML 预测预加载）。

预测目标：给定当前功能上下文，预测用户下一个最可能切换到的功能及概率。
概率 > 0.7 时给出预加载建议（预加载对应模型到 GPU）。

诚实标注（运行环境现状）：xgboost 未包含在 requirements.txt 中，当前出货
环境未安装 XGBoost，也不存在训练产物 data/ml/feature_model.json——
因此 _xgb 恒为 None，实际恒走下方马尔可夫链回退路径（engine 字段如实
上报 "markov+time"）。XGBoost 加载代码保留作为迁移路径：将来把 xgboost
加入依赖并离线训练 booster 落盘后，本模块自动接管，无需改代码。

实现策略（xgboost 缺失时的等价回退）：
  - 首选：XGBoost 分类器（data/ml/feature_model.json 存在且 xgboost 可导入时）。
  - 回退：一阶马尔可夫链（转移计数矩阵）+ 时间特征（小时桶先验）加权融合，
    推理为纯内存查表 + 少量浮点运算，稳定 < 50ms。

事件流持久化：SQLite 表 model_usage_events（自建，DB 不可用时降级内存环形缓冲）。
"""
from __future__ import annotations

import importlib
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from ...config import DATA_DIR

log = logging.getLogger("omnispace.model_manager.predictor")


def _try_import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_xgb = _try_import("xgboost")

# ── 功能集合（与 feature_lock 对齐 + 后台学习类）─────────────────────────
KNOWN_FEATURES = (
    "dialog", "paint", "video_gen", "manga", "training",
    "browser_learning", "behavior_learning",
)

# 功能 -> 预加载目标模型类别（供预加载建议映射）
FEATURE_MODEL_CATEGORY = {
    "dialog": "dialog",
    "paint": "vision",
    "video_gen": "video",
    "manga": "video",
    "training": "dialog",      # LoRA 训练基座使用对话模型
}

_PRELOAD_THRESHOLD = 0.7     # 规格 §3.4：概率 > 0.7 → 触发预加载
_MAX_MEMORY_EVENTS = 5000    # 内存降级环形缓冲上限

# §4.3 自适应：冷门功能判定（长时间未使用 → 相关学习主题降权）
COLD_FEATURE_DAYS = 14       # 使用统计窗口（天）
COLD_FEATURE_MAX_USES = 3    # 窗口内使用次数 < 3 → 冷门


class FeaturePredictor:
    """功能切换预测器——马尔可夫链 + 时间特征（XGBoost 等价回退）。

    线程安全；推理路径无 IO（事件计数常驻内存，启动时从 SQLite 预热）。
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._lock = threading.Lock()
        # 转移计数: {(from, to): count}；from="" 表示会话起始
        self._transitions: dict[tuple[str, str], float] = {}
        self._from_totals: dict[str, float] = {}
        # 时间特征: {(from, to, hour): count}
        self._hour_transitions: dict[tuple[str, str, int], float] = {}
        self._hour_from_totals: dict[tuple[str, int], float] = {}
        self._event_count = 0
        self._last_feature: str = ""
        self._memory_events: deque = deque(maxlen=_MAX_MEMORY_EVENTS)

        self._db_path = db_path or str(DATA_DIR / "model_usage.db")
        self._sqlite_ok = False
        self._init_sqlite()
        self._warm_up()

        # XGBoost 模型（可选）：仅当训练产物存在时加载，否则马尔可夫回退
        self._booster: Any = None
        self.engine = "markov+time"
        self._try_load_xgboost()

    # ── 存储层 ──────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_sqlite(self) -> None:
        """自建 model_usage_events 表；失败降级内存缓冲。"""
        try:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS model_usage_events (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts           REAL NOT NULL,
                        from_feature TEXT NOT NULL DEFAULT '',
                        to_feature   TEXT NOT NULL,
                        hour         INTEGER NOT NULL DEFAULT 0,
                        weekday      INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mue_from "
                    "ON model_usage_events(from_feature)"
                )
            self._sqlite_ok = True
        except Exception as exc:  # noqa: BLE001
            log.warning("model_usage_events 建表失败，降级内存缓冲: %s", exc)
            self._sqlite_ok = False

    def _warm_up(self) -> None:
        """启动时从 SQLite 加载历史事件到内存计数（推理零 IO）。"""
        if not self._sqlite_ok:
            return
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT from_feature, to_feature, hour FROM model_usage_events"
                ).fetchall()
                last = conn.execute(
                    "SELECT to_feature FROM model_usage_events ORDER BY id DESC LIMIT 1"
                ).fetchone()
            with self._lock:
                for frm, to, hour in rows:
                    self._accumulate(frm, to, int(hour))
                if last:
                    self._last_feature = last[0]
            log.info("ML 预测器预热完成: %d 条历史事件", len(rows))
        except Exception as exc:  # noqa: BLE001
            log.warning("预测器预热失败（使用空计数启动）: %s", exc)

    def _try_load_xgboost(self) -> None:
        """加载已训练的 XGBoost 模型（可选）。

        迁移路径：离线训练脚本用历史事件训练 booster 后保存到
        data/ml/feature_model.json，本函数自动接管推理；未训练/未安装
        xgboost 时保持马尔可夫+时间特征回退（等价语义，见模块 docstring）。
        """
        model_path = DATA_DIR / "ml" / "feature_model.json"
        if _xgb is None or not os.path.isfile(model_path):
            return
        try:
            booster = _xgb.Booster()
            booster.load_model(str(model_path))
            self._booster = booster
            self.engine = "xgboost"
            log.info("XGBoost 功能预测模型已加载: %s", model_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("XGBoost 模型加载失败，回退马尔可夫: %s", exc)

    # ── 计数累计 ────────────────────────────────────────────────────

    def _accumulate(self, frm: str, to: str, hour: int) -> None:
        key = (frm, to)
        self._transitions[key] = self._transitions.get(key, 0.0) + 1.0
        self._from_totals[frm] = self._from_totals.get(frm, 0.0) + 1.0
        hkey = (frm, to, hour)
        self._hour_transitions[hkey] = self._hour_transitions.get(hkey, 0.0) + 1.0
        hfkey = (frm, hour)
        self._hour_from_totals[hfkey] = self._hour_from_totals.get(hfkey, 0.0) + 1.0
        self._event_count += 1

    # ── 对外接口 ────────────────────────────────────────────────────

    def record_event(self, from_feature: str, to_feature: str,
                     ts: float | None = None) -> None:
        """记录一次功能切换事件（持久化 + 更新内存计数）。

        Args:
            from_feature: 来源功能（会话首次进入传 ""）
            to_feature:   目标功能
            ts:           事件时间戳（默认当前时间）
        """
        ts = ts or time.time()
        lt = time.localtime(ts)
        hour, weekday = lt.tm_hour, lt.tm_wday
        frm = (from_feature or "").strip()
        to = (to_feature or "").strip()
        if not to:
            return

        with self._lock:
            self._accumulate(frm, to, hour)
            self._last_feature = to

        if self._sqlite_ok:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO model_usage_events"
                        " (ts, from_feature, to_feature, hour, weekday)"
                        " VALUES (?,?,?,?,?)",
                        (ts, frm, to, hour, weekday),
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("事件落库失败（内存计数已更新）: %s", exc)
                self._memory_events.append((ts, frm, to, hour, weekday))
        else:
            self._memory_events.append((ts, frm, to, hour, weekday))

    @property
    def last_feature(self) -> str:
        return self._last_feature

    @property
    def event_count(self) -> int:
        return self._event_count

    def predict_next(self, current_feature: str | None = None,
                     hour: int | None = None) -> dict:
        """预测下一个功能（推理 < 50ms）。

        融合公式: score = 0.7 * P_markov(next|cur) + 0.3 * P_hour(next|cur,hour)
        两项均为加 1 拉普拉斯平滑的经验概率。

        Args:
            current_feature: 当前功能（缺省用最近一次事件的目标功能）
            hour:            小时桶（默认当前系统小时）

        Returns:
            {
              "current_feature": str,
              "next_feature": str,          # 数据不足时为 ""
              "probability": float,
              "preload": bool,              # probability > 0.7
              "preload_model_category": str,
              "candidates": [{feature, probability}...],
              "engine": "markov+time" | "xgboost",
              "events": int,
              "elapsed_ms": float,
            }
        """
        t0 = time.perf_counter()
        cur = (current_feature or self._last_feature or "").strip()
        hour = hour if hour is not None else time.localtime().tm_hour

        candidates: dict[str, float] = {}
        if self._booster is not None:
            candidates = self._predict_xgboost(cur, hour)
        if not candidates:
            candidates = self._predict_markov(cur, hour)

        ranked = sorted(candidates.items(), key=lambda kv: kv[1], reverse=True)
        top_feature, top_prob = (ranked[0] if ranked else ("", 0.0))
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        return {
            "current_feature": cur,
            "next_feature": top_feature,
            "probability": round(top_prob, 4),
            "preload": bool(top_feature) and top_prob > _PRELOAD_THRESHOLD,
            "preload_model_category": FEATURE_MODEL_CATEGORY.get(top_feature, ""),
            "candidates": [
                {"feature": f, "probability": round(p, 4)} for f, p in ranked[:5]
            ],
            "engine": self.engine,
            "events": self._event_count,
            "elapsed_ms": round(elapsed_ms, 3),
        }

    # ── 推理实现 ────────────────────────────────────────────────────

    def _predict_markov(self, cur: str, hour: int) -> dict[str, float]:
        """一阶马尔可夫 + 时间先验加权融合。"""
        with self._lock:
            transitions = dict(self._transitions)
            from_totals = dict(self._from_totals)
            hour_trans = dict(self._hour_transitions)
            hour_totals = dict(self._hour_from_totals)

        n_features = len(KNOWN_FEATURES)
        from_total = from_totals.get(cur, 0.0)
        hour_total = hour_totals.get((cur, hour), 0.0)
        scores: dict[str, float] = {}

        for feat in KNOWN_FEATURES:
            # P_markov = (count(cur->feat) + 1) / (total(cur->*) + N)  拉普拉斯平滑
            markov = (transitions.get((cur, feat), 0.0) + 1.0) / (from_total + n_features)
            time_prior = (hour_trans.get((cur, feat, hour), 0.0) + 1.0) / (
                hour_total + n_features
            )
            score = 0.7 * markov + 0.3 * time_prior
            if score > 0.0:
                scores[feat] = score

        # 数据完全不足时返回空（不给出误导性建议）
        if from_total <= 0.0:
            return {}
        return scores

    def _predict_xgboost(self, cur: str, hour: int) -> dict[str, float]:
        """XGBoost 推理（模型存在时）。特征: [cur one-hot, hour, weekday]。"""
        try:
            feat_vec = [1.0 if f == cur else 0.0 for f in KNOWN_FEATURES]
            feat_vec += [float(hour), float(time.localtime().tm_wday)]
            dmat = _xgb.DMatrix([feat_vec])
            probs = self._booster.predict(dmat)[0]
            return {
                f: float(p)
                for f, p in zip(KNOWN_FEATURES, probs, strict=True)
                if p > 0.0
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("XGBoost 推理失败，回退马尔可夫: %s", exc)
            self._booster = None
            self.engine = "markov+time"
            return {}

    # ── 统计 ────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """返回预测器统计（供 /models/status 暴露）。"""
        return {
            "engine": self.engine,
            "events": self._event_count,
            "last_feature": self._last_feature,
            "sqlite": self._sqlite_ok,
            "tracked_transitions": len(self._transitions),
        }

    # ── §4.3 自适应：冷门功能检测 ──────────────────────────────────

    def get_cold_features(self, days: int = COLD_FEATURE_DAYS) -> list[str]:
        """统计近 days 天各功能使用次数，低于阈值（<COLD_FEATURE_MAX_USES
        次）的功能视为冷门，返回功能名列表。

        仅评估历史上出现过的功能（to_feature 有记录）：窗口内次数
        低于阈值即"长时间未使用"。无任何使用统计时返回 []（调用方不降权）。
        查询异常返回 []，不影响预测主链。
        """
        cutoff = time.time() - days * 86400.0
        recent: dict[str, int] = {}
        historical: set[str] = set()
        if self._sqlite_ok:
            try:
                with self._connect() as conn:
                    for feat, cnt in conn.execute(
                            "SELECT to_feature, COUNT(*) FROM "
                            "model_usage_events WHERE ts >= ? "
                            "GROUP BY to_feature", (cutoff,)):
                        recent[str(feat)] = int(cnt)
                    for (feat,) in conn.execute(
                            "SELECT DISTINCT to_feature "
                            "FROM model_usage_events"):
                        historical.add(str(feat))
            except Exception as exc:  # noqa: BLE001
                log.warning("冷门功能统计查询失败: %s", exc)
                return []
        else:
            for ts, _frm, to, _hour, _weekday in self._memory_events:
                historical.add(to)
                if ts >= cutoff:
                    recent[to] = recent.get(to, 0) + 1
        if not historical:
            return []           # 无使用统计 → 不判定冷门
        return sorted(f for f in historical
                      if recent.get(f, 0) < COLD_FEATURE_MAX_USES)
# 本项目仅供学习使用，商业授权请+Q 3559331368
