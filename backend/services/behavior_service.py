"""OmniSpace AI v2.3 行为学习记录器（TASK-035）。

用户操作行为异步记录（离线可用）：
  - record_event：内部队列 + 后台线程批量落库，调用返回 <10ms，不阻塞主线程；
  - analyze_preferences：Prompt 风格聚类 / AI输出vs用户修改差异 / 高频参数 / 拒绝模式；
  - build_training_pairs：构造 {"instruction","input","output"} LoRA 训练数据
    （偏好对 + 风格模板 + 负样本）；
  - should_trigger_finetune：可用训练数据 ≥100 且当前无训练任务。

降级策略：数据库不可用时写入内存缓冲（日志标注）；jieba 缺失时用正则分词回退。

存储：SQLite 表 behavior_logs（自建表 CREATE TABLE IF NOT EXISTS，
字段 event_id/event_type/content/context/before/after/timestamp/feature）。
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
import uuid
from collections import Counter

from ..data.crypto import decrypt_text, encrypt_text
from ..data.database import get_db_safe

log = logging.getLogger("omnispace.behavior")

# ── 可选依赖探测 ─────────────────────────────────────────────
try:
    import jieba  # type: ignore
    _JIEBA_AVAILABLE = True
except Exception:  # pragma: no cover - 降级路径
    jieba = None  # type: ignore
    _JIEBA_AVAILABLE = False
    log.info("jieba 不可用，偏好分析降级为正则分词")

# ── 常量 ─────────────────────────────────────────────────────
FINETUNE_MIN_PAIRS = 100        # 触发微调的最少训练对（TASK-035）
_FLUSH_INTERVAL_S = 0.5         # 后台批量落库间隔
_FLUSH_BATCH = 200              # 单次落库最大批量
_TOP_WORDS = 20                 # 高频词保留个数

# 合法事件类型（TASK-035 BehaviorEvent）
EVENT_TYPES = (
    "prompt_input",      # 用户输入的 Prompt
    "prompt_modify",     # 用户修改 AI 生成的内容
    "style_select",      # 选择的 LoRA/参数
    "storyboard_edit",   # 分镜编辑
    "reject",            # 删除/重新生成
    "feature_use",       # 功能使用
    "project_context",   # 项目上下文
)

_BEHAVIOR_DDL = """
CREATE TABLE IF NOT EXISTS behavior_logs (
    event_id   TEXT PRIMARY KEY,
    event_type TEXT NOT NULL DEFAULT '',
    content    TEXT DEFAULT '',
    context    TEXT DEFAULT '',
    "before"   TEXT DEFAULT '',
    "after"    TEXT DEFAULT '',
    timestamp  REAL NOT NULL DEFAULT 0,
    feature    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_behavior_logs_type ON behavior_logs(event_type);
CREATE INDEX IF NOT EXISTS idx_behavior_logs_ts ON behavior_logs(timestamp);
"""

_WORD_RE = re.compile(r"[a-zA-Z0-9_]+|[一-鿿]{2,}")
_STOPWORDS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "和", "与", "或",
    "一个", "一些", "什么", "怎么", "如何", "可以", "请", "帮我", "一下",
    "the", "a", "an", "is", "are", "to", "of", "and", "or", "in", "on",
}


def _tokenize(text: str) -> list[str]:
    """分词：jieba 可用时用之，否则正则回退（中文 2+ 字串 / 英文数字词）。"""
    if not text:
        return []
    if _JIEBA_AVAILABLE:
        try:
            return [w.strip() for w in jieba.lcut(text)
                    if w.strip() and w.strip() not in _STOPWORDS
                    and len(w.strip()) > 1]
        except Exception:  # noqa: BLE001
            pass
    return [w for w in _WORD_RE.findall(text.lower())
            if w not in _STOPWORDS and len(w) > 1]


class BehaviorLearningService:
    """行为学习记录器（TASK-035）。单例，见 get_behavior_service()。"""

    def __init__(self) -> None:
        self._db = get_db_safe()
        self._mem_logs: list[dict] = []          # 数据库不可用时内存回退
        self._queue: queue.Queue[dict] = queue.Queue()
        self._pending = 0                        # 已入队未落库计数
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._pref_cache: dict | None = None  # 最近一次偏好分析结果
        self._pref_analyzed_at = 0.0
        self._recent_summary: list[str] = []     # 最近学习摘要（滚动 10 条）
        if self._db is not None:
            try:
                self._db.executescript(_BEHAVIOR_DDL)
                log.info("behavior_logs 表就绪")
            except Exception as exc:  # noqa: BLE001
                log.warning("behavior_logs 建表失败，降级内存存储: %s", exc)
                self._db = None
        else:
            log.warning("数据库不可用，行为日志降级为内存存储")
        self._worker = threading.Thread(
            target=self._flush_loop, name="behavior-flush", daemon=True)
        self._worker.start()

    # ── 事件记录（<10ms，异步）────────────────────────────────

    def record_event(self, event: dict) -> str:
        """记录一条行为事件，返回 event_id。异步落库，调用耗时 <10ms。

        event 字段: event_type/content/context/before/after/feature/timestamp
        （timestamp 缺省取当前时间；event_id 缺省自动生成）。
        """
        if not isinstance(event, dict):
            raise ValueError("event 必须是 dict")
        event_id = str(event.get("event_id") or uuid.uuid4().hex)
        row = {
            "event_id": event_id,
            "event_type": str(event.get("event_type") or "feature_use"),
            "content": str(event.get("content") or ""),
            "context": str(event.get("context") or ""),
            "before": str(event.get("before") or ""),
            "after": str(event.get("after") or ""),
            "timestamp": float(event.get("timestamp") or time.time()),
            "feature": str(event.get("feature") or ""),
        }
        self._queue.put(row)
        with self._pending_lock:
            self._pending += 1
        return event_id

    def _flush_loop(self) -> None:
        """后台线程：定时批量落库。"""
        while not self._stop.is_set():
            try:
                self._flush_once()
            except Exception as exc:  # noqa: BLE001 - 落库异常不退出线程
                log.warning("行为日志批量落库失败: %s", exc)
            self._stop.wait(_FLUSH_INTERVAL_S)

    def _flush_once(self) -> int:
        """取出一批队列事件写入存储，返回写入条数。"""
        batch: list[dict] = []
        while len(batch) < _FLUSH_BATCH:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0
        written = 0
        if self._db is not None:
            for row in batch:
                try:
                    # 要求#36：行为细节四列落库加密（内存回退保持明文）
                    self._db.sql(
                        'INSERT OR REPLACE INTO behavior_logs '
                        '(event_id, event_type, content, context, '
                        '"before", "after", timestamp, feature) '
                        'VALUES (?,?,?,?,?,?,?,?)',
                        (row["event_id"], row["event_type"],
                         encrypt_text(row["content"]),
                         encrypt_text(row["context"]),
                         encrypt_text(row["before"]),
                         encrypt_text(row["after"]),
                         row["timestamp"], row["feature"]))
                    written += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("行为日志写入失败: %s", exc)
                    self._mem_logs.append(row)
        else:
            self._mem_logs.extend(batch)
            written = len(batch)
        with self._pending_lock:
            self._pending = max(0, self._pending - len(batch))
        return written

    def flush(self, timeout: float = 10.0) -> bool:
        """阻塞等待队列清空（测试/关停用），返回是否在超时内完成。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._pending_lock:
                pending = self._pending
            if pending == 0 and self._queue.empty():
                return True
            time.sleep(0.02)
        return False

    # ── 数据读取 ─────────────────────────────────────────────

    def _load_events(self, limit: int = 10000,
                     event_type: str = "") -> list[dict]:
        """读取行为日志（优先数据库，含内存回退数据）。"""
        rows: list[dict] = []
        if self._db is not None:
            try:
                if event_type:
                    rows = self._db.query(
                        'SELECT event_id, event_type, content, context,'
                        ' "before", "after", timestamp, feature'
                        ' FROM behavior_logs WHERE event_type=?'
                        ' ORDER BY timestamp DESC LIMIT ?',
                        (event_type, limit))
                else:
                    rows = self._db.query(
                        'SELECT event_id, event_type, content, context,'
                        ' "before", "after", timestamp, feature'
                        ' FROM behavior_logs'
                        ' ORDER BY timestamp DESC LIMIT ?', (limit,))
            except Exception as exc:  # noqa: BLE001
                log.warning("行为日志读取失败: %s", exc)
        mem = self._mem_logs
        if event_type:
            mem = [r for r in mem if r.get("event_type") == event_type]
        if mem:
            rows.extend(mem)
        rows.sort(key=lambda r: r.get("timestamp", 0), reverse=True)
        rows = rows[:limit]
        # 要求#36：数据库读出的密文列在此统一解密（明文历史/内存数据透传）
        for r in rows:
            for col in ("content", "context", "before", "after"):
                val = r.get(col)
                if isinstance(val, str) and val.startswith("enc:v1:"):
                    r[col] = decrypt_text(val)
        return rows

    def count(self) -> int:
        """已记录操作总数（含未落库队列）。"""
        total = len(self._mem_logs)
        if self._db is not None:
            try:
                total += self._db.count("behavior_logs")
            except Exception:  # noqa: BLE001
                pass
        with self._pending_lock:
            total += self._pending
        return total

    # ── 偏好分析 ─────────────────────────────────────────────

    def analyze_preferences(self) -> dict:
        """聚类分析用户偏好（每日凌晨或空闲时调用）。

        返回: Prompt 风格聚类（高频词/参数统计）、AI输出vs用户修改差异分析、
              高频参数、拒绝模式。
        """
        events = self._load_events()
        by_type: dict[str, list[dict]] = {}
        for e in events:
            by_type.setdefault(e.get("event_type", ""), []).append(e)

        # 1) Prompt 风格聚类：高频词 + 长度/句式特征
        prompts = [e.get("content", "") for e in by_type.get("prompt_input", [])]
        word_freq: Counter = Counter()
        for p in prompts:
            word_freq.update(_tokenize(p))
        lengths = [len(p) for p in prompts if p]
        prompt_style = {
            "count": len(prompts),
            "top_words": word_freq.most_common(_TOP_WORDS),
            "avg_length": round(sum(lengths) / len(lengths), 1) if lengths else 0,
            "tokenizer": "jieba" if _JIEBA_AVAILABLE else "regex",
        }

        # 2) AI输出 vs 用户修改差异分析
        modifies = by_type.get("prompt_modify", [])
        diffs: list[dict] = []
        grow_total = 0
        for e in modifies:
            before, after = e.get("before", ""), e.get("after", "")
            if not before and not after:
                continue
            delta = len(after) - len(before)
            grow_total += delta
            diffs.append({
                "event_id": e.get("event_id"),
                "feature": e.get("feature", ""),
                "before_len": len(before), "after_len": len(after),
                "delta": delta,
                "modified_ratio": round(
                    self._edit_ratio(before, after), 4),
            })
        modify_analysis = {
            "count": len(modifies),
            "avg_length_delta": round(grow_total / len(diffs), 1) if diffs else 0,
            "tendency": ("倾向扩写" if grow_total > 0
                         else "倾向精简" if grow_total < 0 else "持平"),
            "samples": diffs[:20],
        }

        # 3) 高频参数 / 风格
        styles = by_type.get("style_select", [])
        param_freq: Counter = Counter()
        for e in styles:
            if e.get("content"):
                param_freq[e["content"]] += 1
        features = by_type.get("feature_use", [])
        feature_freq: Counter = Counter()
        for e in features:
            if e.get("feature"):
                feature_freq[e["feature"]] += 1
        hot_params = {
            "styles": param_freq.most_common(_TOP_WORDS),
            "features": feature_freq.most_common(_TOP_WORDS),
        }

        # 4) 拒绝模式：频繁删除/重新生成的输出特征
        rejects = by_type.get("reject", [])
        reject_features: Counter = Counter()
        reject_words: Counter = Counter()
        for e in rejects:
            if e.get("feature"):
                reject_features[e["feature"]] += 1
            reject_words.update(_tokenize(e.get("content", ""))[:50])
        reject_analysis = {
            "count": len(rejects),
            "by_feature": reject_features.most_common(_TOP_WORDS),
            "reject_rate": round(
                len(rejects) / max(1, len(prompts) + len(styles)), 4),
            "top_words": reject_words.most_common(10),
        }

        result = {
            "analyzed_at": time.time(),
            "total_events": len(events),
            "prompt_style": prompt_style,
            "modify_analysis": modify_analysis,
            "hot_params": hot_params,
            "reject_analysis": reject_analysis,
            "event_type_counts": {k: len(v) for k, v in by_type.items()},
        }
        self._pref_cache = result
        self._pref_analyzed_at = result["analyzed_at"]
        summary = (f"分析 {len(events)} 条事件：Prompt {len(prompts)} 条，"
                   f"修改 {len(modifies)} 条（{modify_analysis['tendency']}），"
                   f"拒绝 {len(rejects)} 条")
        self._push_summary(summary)
        return result

    @staticmethod
    def _edit_ratio(before: str, after: str) -> float:
        """简易编辑差异度：字符级 LCS 近似（用 3-gram 重合度估计，O(n)）。"""
        if not before and not after:
            return 0.0
        if not before or not after:
            return 1.0

        def _grams(s: str) -> set:
            return {s[i:i + 3] for i in range(max(len(s) - 2, 1))}

        a, b = _grams(before), _grams(after)
        union = a | b
        if not union:
            return 0.0
        return 1.0 - len(a & b) / len(union)

    # ── 训练数据构造 ─────────────────────────────────────────

    def build_training_pairs(self) -> list[dict]:
        """构造 LoRA 训练数据: [{"instruction","input","output",...}]。

        - 偏好对：prompt_modify 的 AI原始(before) → 用户修改(after)；
        - 风格模板：高频 prompt_input 模式；
        - 负样本：reject 事件被删除的输出。
        """
        pairs: list[dict] = []

        # 1) 偏好对：AI原始 → 用户修改
        for e in self._load_events(event_type="prompt_modify"):
            before, after = e.get("before", ""), e.get("after", "")
            if not before or not after or before == after:
                continue
            pairs.append({
                "instruction": "请按照用户偏好改写以下内容",
                "input": before,
                "output": after,
                "pair_type": "preference",
                "feature": e.get("feature", ""),
                "timestamp": e.get("timestamp", 0),
            })

        # 2) 风格模板：高频 Prompt 模式（出现 ≥2 次的视为稳定偏好）
        prompts = self._load_events(event_type="prompt_input")
        style_freq: Counter = Counter(
            e.get("content", "") for e in prompts if e.get("content"))
        for content, cnt in style_freq.most_common(50):
            if cnt < 2:
                continue
            pairs.append({
                "instruction": "请按照用户常用风格生成内容",
                "input": e_context_of(prompts, content),
                "output": content,
                "pair_type": "style_template",
                "weight": cnt,
            })

        # 3) 负样本：拒绝/删除的输出
        for e in self._load_events(event_type="reject"):
            content = e.get("content", "")
            if not content:
                continue
            pairs.append({
                "instruction": "请避免生成以下内容（用户已拒绝的样本）",
                "input": e.get("context", "") or e.get("feature", ""),
                "output": content,
                "pair_type": "negative",
                "timestamp": e.get("timestamp", 0),
            })
        return pairs

    # ── 微调触发 ─────────────────────────────────────────────

    def should_trigger_finetune(self) -> bool:
        """可用训练数据 ≥100 且当前无训练任务时返回 True。"""
        if self._has_active_training():
            return False
        return len(self.build_training_pairs()) >= FINETUNE_MIN_PAIRS

    def _has_active_training(self) -> bool:
        """当前是否存在排队/进行中的训练任务（查 train_tasks 表）。"""
        if self._db is None:
            return False
        try:
            return self._db.count(
                "train_tasks", "status IN ('queued','training')") > 0
        except Exception:  # noqa: BLE001
            return False

    # ── 统计 ─────────────────────────────────────────────────

    def stats(self) -> dict:
        """已记录操作数 / 偏好模型状态 / 下次微调时间 / 最近学习摘要。"""
        pairs_count = None
        try:
            pairs_count = len(self.build_training_pairs())
        except Exception:  # noqa: BLE001
            pass
        remaining = (max(0, FINETUNE_MIN_PAIRS - pairs_count)
                     if pairs_count is not None else None)
        # 下次微调时间估算：按最近 100 条事件的平均速率外推
        next_finetune: float | None = None
        events = self._load_events(limit=100)
        if remaining and len(events) >= 2:
            span = max(1.0, events[0].get("timestamp", 0)
                       - events[-1].get("timestamp", 0))
            rate = len(events) / span            # 条/秒
            if rate > 0:
                next_finetune = time.time() + remaining / rate
        return {
            "total_events": self.count(),
            "training_pairs": pairs_count,
            "preference_model": {
                "status": "ready" if self._pref_cache else "not_analyzed",
                "analyzed_at": self._pref_analyzed_at or None,
                "tokenizer": "jieba" if _JIEBA_AVAILABLE else "regex",
            },
            "finetune": {
                "min_pairs": FINETUNE_MIN_PAIRS,
                "remaining_pairs": remaining,
                "should_trigger": self.should_trigger_finetune(),
                "next_estimate": next_finetune,
            },
            "recent_summary": list(self._recent_summary),
            "db_available": self._db is not None,
        }

    def clear(self) -> int:
        """清空行为日志（用户手动清除），返回删除条数。"""
        # 清空队列
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        with self._pending_lock:
            self._pending = 0
        deleted = len(self._mem_logs)
        self._mem_logs.clear()
        if self._db is not None:
            try:
                deleted += self._db.count("behavior_logs")
                self._db.sql("DELETE FROM behavior_logs")
            except Exception as exc:  # noqa: BLE001
                log.warning("清空行为日志失败: %s", exc)
        self._pref_cache = None
        self._pref_analyzed_at = 0.0
        self._push_summary("用户手动清除了行为学习数据")
        return deleted + dropped

    def _push_summary(self, text: str) -> None:
        self._recent_summary.append(
            f"{time.strftime('%H:%M:%S')} {text}")
        del self._recent_summary[:-10]

    def shutdown(self) -> None:
        """停止后台线程并落库剩余事件（应用关停时调用）。"""
        self._stop.set()
        self._worker.join(timeout=3.0)
        try:
            self._flush_once()
        except Exception:  # noqa: BLE001
            pass


def e_context_of(events: list[dict], content: str) -> str:
    """取某条 prompt 内容首次出现时的上下文（build_training_pairs 辅助）。"""
    for e in events:
        if e.get("content") == content:
            return e.get("context", "") or e.get("feature", "")
    return ""


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_behavior_instance: BehaviorLearningService | None = None
_behavior_lock = threading.Lock()


def get_behavior_service() -> BehaviorLearningService:
    """获取行为学习服务单例（线程安全双重检查）。"""
    global _behavior_instance
    if _behavior_instance is None:
        with _behavior_lock:
            if _behavior_instance is None:
                _behavior_instance = BehaviorLearningService()
    return _behavior_instance
