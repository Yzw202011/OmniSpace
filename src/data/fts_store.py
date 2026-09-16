"""OmniSpace AI v2.5.0 全文检索存储（TASK-051 混合检索 · 关键词路）。

实现：SQLite FTS5（trigram 分词器，中文按 3 字滑窗索引）。
- 入库/删除与 knowledge_service 的知识生命周期同步（同步写入/移除）。
- 查询：≥3 字词走 FTS5 MATCH（BM25 排序）；2 字中文词走 LIKE 兜底
  （trigram 索引无法匹配 <3 字的查询，这是 SQLite FTS5 的固有限制）。
- 输出 [(kid, score)] 按相关性降序 —— 供 RRF 融合消费（只吃名次）。

降级：FTS5 不可用（编译缺失）时整张表退化为普通表 + LIKE 检索，
保证关键词路始终可用；所有异常吞掉并记日志，绝不影响主链路。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import logging
import re
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database

log = logging.getLogger("omnispace.fts")

_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    kid UNINDEXED, content, title, topic, tokenize='trigram'
);
"""
# FTS5 不可用时的普通表回退
_PLAIN_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_fts (
    kid TEXT PRIMARY KEY, content TEXT DEFAULT '', title TEXT DEFAULT '', topic TEXT DEFAULT ''
);
"""

_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")


def _query_terms(query: str) -> tuple[list[str], list[str]]:
    """把查询拆为 (fts_terms, like_terms)。

    fts_terms：英文/数字词（≥2 字符）与 ≥3 字的 CJK 片段 → FTS5 MATCH。
    like_terms：2 字 CJK 词（trigram 索引无法匹配）→ LIKE 兜底。
    CJK 长片段同时生成 3 字滑窗子串，提高召回（部分命中即可）。
    """
    query = (query or "").lower().strip()
    fts_terms: list[str] = []
    like_terms: list[str] = []
    for w in _WORD_RE.findall(query):
        if len(w) >= 2:
            fts_terms.append(w)
    for run in _CJK_RUN_RE.findall(query):
        if len(run) >= 3:
            fts_terms.append(run)
            # 3 字滑窗，长句也能部分命中
            fts_terms.extend(run[i:i + 3] for i in range(len(run) - 2))
        elif len(run) == 2:
            like_terms.append(run)
        # 单字噪声大，不参与
    # 去重保序
    fts_terms = list(dict.fromkeys(fts_terms))[:12]
    like_terms = list(dict.fromkeys(like_terms))[:8]
    return fts_terms, like_terms


class FtsStore:
    """知识全文索引（线程安全单例，见 get_fts_store()）。"""

    def __init__(self, db: Database | None = None) -> None:
        self._lock = threading.Lock()
        self._fts5 = False
        if db is None:
            from .database import get_db_safe
            db = get_db_safe()
        self._db = db
        if self._db is None:
            log.warning("数据库不可用，FTS 索引降级为空实现")
            return
        try:
            self._db.executescript(_FTS_DDL)
            self._fts5 = True
            log.info("knowledge_fts 全文索引就绪（FTS5/trigram）")
        except Exception as exc:  # noqa: BLE001
            log.warning("FTS5 不可用（%s），回退普通表+LIKE", exc)
            try:
                self._db.executescript(_PLAIN_DDL)
            except Exception:  # noqa: BLE001
                self._db = None
        self._backfill_if_empty()

    # ── 写入 ─────────────────────────────────────────────────

    def add(self, kid: str, content: str, title: str = "", topic: str = "") -> None:
        """同步写入全文索引（幂等：先删后插）。"""
        if self._db is None or not kid:
            return
        try:
            with self._lock:
                self._db.sql("DELETE FROM knowledge_fts WHERE kid = ?", (kid,))
                self._db.sql(
                    "INSERT INTO knowledge_fts (kid, content, title, topic) VALUES (?,?,?,?)",
                    (kid, (content or "")[:8000], (title or "")[:500], (topic or "")[:200]))
        except Exception as exc:  # noqa: BLE001
            log.debug("FTS 写入失败（忽略）: %s", exc)

    def delete(self, kid: str) -> None:
        """从全文索引移除。"""
        if self._db is None or not kid:
            return
        try:
            with self._lock:
                self._db.sql("DELETE FROM knowledge_fts WHERE kid = ?", (kid,))
        except Exception as exc:  # noqa: BLE001
            log.debug("FTS 删除失败（忽略）: %s", exc)

    def count(self) -> int:
        if self._db is None:
            return 0
        try:
            row = self._db.query_one("SELECT count(*) AS n FROM knowledge_fts")
            return int(row["n"]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    # ── 检索 ─────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """关键词检索，返回 [(kid, score)] 按相关性降序（分数仅用于排序）。"""
        if self._db is None:
            return []
        fts_terms, like_terms = _query_terms(query)
        if not fts_terms and not like_terms:
            return []
        hits: dict[str, float] = {}
        # 路 1：FTS5 MATCH（BM25，rank 越小越相关）
        if fts_terms and self._fts5:
            match = " OR ".join(f'"{t}"' for t in fts_terms)
            try:
                rows = self._db.query(
                    "SELECT kid, rank FROM knowledge_fts WHERE knowledge_fts MATCH ? "
                    "ORDER BY rank LIMIT ?", (match, max(top_k * 3, 10)))
                for i, r in enumerate(rows):
                    # rank 为负值（越小越好）→ 转正分并保留次序信息
                    hits[r["kid"]] = max(hits.get(r["kid"], 0.0),
                                         1000.0 + float(r.get("rank") or 0.0) - i * 0.01)
            except Exception as exc:  # noqa: BLE001
                log.debug("FTS5 检索失败（忽略）: %s", exc)
        # 路 2：LIKE 兜底（2 字 CJK 词；FTS5 不可用时的主路）
        probe = fts_terms + like_terms if not self._fts5 else like_terms
        for term in probe:
            try:
                rows = self._db.query(
                    "SELECT kid FROM knowledge_fts WHERE content LIKE ? OR title LIKE ? "
                    "OR topic LIKE ? LIMIT ?",
                    tuple([f"%{term}%"] * 3 + [max(top_k * 3, 10)]))
                for j, r in enumerate(rows):
                    # LIKE 命中给较低基分，按命中词序微调
                    hits[r["kid"]] = hits.get(r["kid"], 0.0) + 100.0 - j * 0.01
            except Exception as exc:  # noqa: BLE001
                log.debug("FTS LIKE 检索失败（忽略）: %s", exc)
        ordered = sorted(hits.items(), key=lambda kv: kv[1], reverse=True)
        return ordered[:max(1, int(top_k or 5))]

    # ── 存量回填 ─────────────────────────────────────────────

    def _backfill_if_empty(self) -> None:
        """首次建表后，把 knowledge_meta 存量知识回填进全文索引。"""
        if self._db is None:
            return
        try:
            if self.count() > 0:
                return
            rows = self._db.query(
                "SELECT id, content, title, topic FROM knowledge_meta LIMIT 20000")
            if not rows:
                return
            with self._lock:
                for r in rows:
                    self._db.sql(
                        "INSERT OR REPLACE INTO knowledge_fts (kid, content, title, topic) "
                        "VALUES (?,?,?,?)",
                        (r["id"], (r.get("content") or "")[:8000],
                         (r.get("title") or "")[:500], (r.get("topic") or "")[:200]))
            log.info("FTS 存量回填完成: %d 条", len(rows))
        except Exception as exc:  # noqa: BLE001
            log.debug("FTS 回填跳过: %s", exc)


_fts_instance: FtsStore | None = None
_fts_lock = threading.Lock()


def get_fts_store() -> FtsStore:
    """获取全文索引单例（线程安全双重检查）。"""
    global _fts_instance
    if _fts_instance is None:
        with _fts_lock:
            if _fts_instance is None:
                _fts_instance = FtsStore()
    return _fts_instance
