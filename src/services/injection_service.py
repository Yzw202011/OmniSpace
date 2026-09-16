"""OmniSpace AI v2.5.0 RAG 混合检索注入对话（TASK-034 + TASK-051）。

混合检索（TASK-051）：
  路1 向量召回 — ChromaDB 语义相似度（bge-large-zh 嵌入）。
  路2 关键词召回 — SQLite FTS5 trigram 全文索引（精确术语/代码/专有名词）。
  融合 — RRF（Reciprocal Rank Fusion, k=60）：只吃两路名次，不需分数归一化。
  过滤 — 向量路保留 score<0.5 阈值；关键词命中视为精确证据，可豁免阈值
         （语义弱但术语精确命中的知识应被注入，这正是混合检索的意义）。

性能目标：检索延迟 <100ms；无相关知识时不注入、不影响正常对话。
降级策略：向量库内部自动降级（ChromaDB → 内存 TF-IDF）；
         FTS 索引内部自动降级（FTS5 → LIKE）；单路故障时另一路独立可用。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..data.vector_db import VectorDB

import logging
import re
import threading
import time

from ..data.fts_store import get_fts_store
from ..data.vector_db import get_vector_db

log = logging.getLogger("omnispace.injection")

# ── 常量 ─────────────────────────────────────────────────────
SCORE_THRESHOLD = 0.5     # 向量相关性分数阈值（过滤 score < 0.5）
DEFAULT_TOP_K = 5
MAX_INJECT_CHARS = 2000   # 注入文本总长度上限（防止 Prompt 膨胀）
RRF_K = 60                # RRF 融合常数（标准值，压制高名次的分数差异）
RECALL_MULT = 2           # 单路召回倍数（每路取 top_k*RECALL_MULT 参与融合）
# 注入质量闸（升级批3，2026-09-05）：向量分低于此地板的条目不注入
# （关键词路命中分恒=SCORE_THRESHOLD，属无向量分兜底通道，不适用本地板）
MIN_VECTOR_SCORE = 0.55
# 创作意图降权（D3 软规则）：命中则方法论/概念类优先、剧情 fact 靠后且
# 条数收敛到 3——「写剧本要的是方法不是剧透」
_CREATIVE_INTENT_RE = re.compile(
    r"(?:写|创作|编|生成)[^。？！\n]{0,12}"
    r"(?:剧本|文案|故事|大纲|小说|脚本|台词)")
_CREATIVE_TYPE_ORDER = {"methodology": 0, "concept": 1, "case": 2, "fact": 3}


class KnowledgeInjectionService:
    """RAG 检索注入服务（TASK-034）。单例，见 get_injection_service()。"""

    def __init__(self, vector_db: VectorDB | None = None,
                 score_threshold: float = SCORE_THRESHOLD) -> None:
        self._vdb = vector_db if vector_db is not None else get_vector_db()
        self._threshold = score_threshold
        self.last_latency_ms = 0.0   # 最近一次检索耗时（观测用）

    # ── RAG 混合检索（目标 <100ms）──────────────────────────

    def retrieve(self, query: str, top_k: int = DEFAULT_TOP_K) -> list[dict]:
        """双路召回（向量+关键词）→ RRF 融合 → 阈值过滤 → 按融合分降序。

        返回: [{"id","content","source","score","topic","type","created_at",
                "match","rrf"}]
          match ∈ vector / keyword / hybrid（命中路标记，观测用）。
        空查询 / 空库 / 检索异常均返回空列表，不抛异常。
        """
        self.last_latency_ms = 0.0
        if not query or not query.strip():
            return []
        q = query.strip()
        top_k = max(1, int(top_k or DEFAULT_TOP_K))
        recall = top_k * RECALL_MULT
        t0 = time.perf_counter()

        # 路1：向量召回（异常按空路处理，不阻断关键词路）
        vec_map: dict[str, tuple[dict, float]] = {}   # kid -> (raw, score)
        try:
            for r in self._vdb.search(q, n_results=recall) or []:
                kid = r.get("id", "")
                if not kid:
                    continue
                score = 1.0 - float(r.get("distance", 1.0) or 0.0)
                vec_map[kid] = (r, score)
        except Exception as exc:  # noqa: BLE001
            log.warning("向量召回失败，仅走关键词路: %s", exc)

        # 路2：关键词召回（FTS5/LIKE，异常按空路处理）
        kw_ids: set[str] = set()
        kw_ranked: list[str] = []
        try:
            for kid, _s in get_fts_store().search(q, top_k=recall):
                if kid and kid not in kw_ids:
                    kw_ids.add(kid)
                    kw_ranked.append(kid)
        except Exception as exc:  # noqa: BLE001
            log.warning("关键词召回失败，仅走向量路: %s", exc)

        # RRF 融合（只吃名次）
        fused: dict[str, float] = {}
        for rank, kid in enumerate(vec_map.keys(), 1):
            fused[kid] = fused.get(kid, 0.0) + 1.0 / (RRF_K + rank)
        for rank, kid in enumerate(kw_ranked, 1):
            fused[kid] = fused.get(kid, 0.0) + 1.0 / (RRF_K + rank)

        items: list[dict] = []
        for kid, rrf in sorted(fused.items(), key=lambda kv: kv[1], reverse=True):
            if len(items) >= top_k:
                break
            in_vec = kid in vec_map
            in_kw = kid in kw_ids
            if in_vec:
                raw, score = vec_map[kid]
                # 向量低分且无关键词背书 → 过滤（保持原阈值语义）
                if score < self._threshold and not in_kw:
                    continue
                meta = raw.get("metadata") or {}
                items.append({
                    "id": kid,
                    "content": raw.get("document", "") or "",
                    "source": meta.get("source_url") or meta.get("source") or "",
                    "score": round(score, 4),
                    "topic": meta.get("topic", ""),
                    "type": meta.get("type", ""),
                    "created_at": meta.get("created_at", 0),
                    "match": "hybrid" if in_kw else "vector",
                    "rrf": round(rrf, 6),
                })
            else:
                # 关键词路独有 → 从 knowledge_meta 补全元数据
                row = self._meta_of(kid)
                if not row:
                    continue
                items.append({
                    "id": kid,
                    "content": row.get("content", "") or "",
                    "source": row.get("source_url", "") or "",
                    # 关键词精确命中视为达阈值（无向量分可算）
                    "score": self._threshold,
                    "topic": row.get("topic", "") or "",
                    "type": row.get("type", "") or "",
                    "created_at": row.get("created_at", 0),
                    "match": "keyword",
                    "rrf": round(rrf, 6),
                })

        self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
        if self.last_latency_ms > 100.0:
            log.warning("RAG 检索延迟超标: %.1fms (>100ms)", self.last_latency_ms)
        # 注入质量闸（升级批3）：向量分低于地板的条目不注入；
        # 关键词路独有命中（恒=阈值分）是无向量分时的兜底通道，不受此限
        items = [it for it in items
                 if it.get("match") == "keyword"
                 or float(it.get("score", 0.0)) >= MIN_VECTOR_SCORE]
        # 反馈降权过滤（升级批5）：被点踩降权至负分的知识不再注入
        items = [it for it in items if not self._is_demoted(it.get("id", ""))]
        return items

    def _is_demoted(self, kid: str) -> bool:
        """质量分为负（用户点踩降权）→ 不再注入。查询失败按未降权处理。"""
        if not kid:
            return False
        try:
            from ..data.database import get_db_safe
            db = get_db_safe()
            if db is None:
                return False
            row = db.query_one(
                "SELECT quality_score FROM knowledge_meta WHERE id=?", (kid,))
            if row is None:
                return False
            return float(row.get("quality_score") or 0.0) < 0.0
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _meta_of(kid: str) -> dict | None:
        """按 kid 读取 knowledge_meta（关键词路独有命中的元数据补全）。"""
        try:
            from ..data.database import get_db_safe
            db = get_db_safe()
            if db is None:
                return None
            return db.query_one(
                "SELECT id, content, source_url, topic, type, created_at "
                "FROM knowledge_meta WHERE id = ?", (kid,))
        except Exception:  # noqa: BLE001
            return None

    # ── Prompt 注入 ──────────────────────────────────────────

    def inject_to_prompt(self, query: str, results: list[dict]) -> str:
        """将检索结果注入到 Prompt 中。空结果返回空串。

        格式:
        【相关知识】
        1. {knowledge.content}（来源: {source}）
        请基于以上知识回答用户问题。
        """
        if not results:
            return ""
        lines = ["【相关知识】"]
        total = len(lines[0])
        for i, r in enumerate(results, 1):
            content = (r.get("content") or "").strip()
            if not content:
                continue
            source = (r.get("source") or "").strip()
            line = f"{i}. {content}（来源: {source}）" if source \
                else f"{i}. {content}"
            if total + len(line) > MAX_INJECT_CHARS:
                break
            lines.append(line)
            total += len(line)
        if len(lines) == 1:
            return ""
        # 框架语（2026-09-05 事故根修）：原「请基于以上知识回答用户问题」
        # 把创作类任务（写剧本/文案/故事）硬掰成提取模式——模型从资料里
        # 摘抄只言片语交差（实测「写一部短剧剧本」只回 10 字剧名）。改为
        # 参考资料制：事实类优先依据，创作类当素材按用户要求产出完整内容。
        lines.append(
            "以上是知识库中可能相关的资料：回答事实类问题时请优先依据这些"
            "知识；若用户要求创作（如写剧本、文案、故事），请把它们仅当作"
            "背景素材，按用户的实际要求产出完整、成型的内容，不要只从资料"
            "中摘抄词语或句子；资料与用户要求无关时请直接忽略。")
        return "\n".join(lines)

    # ── 对话增强 ─────────────────────────────────────────────

    def enhance_chat(self, user_message: str,
                     history: list | None = None) -> tuple[str, list]:
        """完整 RAG 增强流程。

        返回 (注入文本或空串, knowledge_refs)。无相关知识时不注入。
        history 预留给对话服务传入上下文（当前检索以 user_message 为准）。
        """
        results = self.retrieve(user_message)
        if not results:
            return "", []
        # 创作意图降权（升级批3/D3 软规则）：方法论/概念优先、剧情
        # fact 靠后且条数收敛到 3——写剧本要的是方法不是剧透
        if _CREATIVE_INTENT_RE.search(user_message):
            results = sorted(
                results,
                key=lambda r: _CREATIVE_TYPE_ORDER.get(r.get("type", "fact"), 3))
            results = results[:3]
        injected = self.inject_to_prompt(user_message, results)
        if not injected:
            return "", []
        log.info("RAG 注入 %d 条（%s）: %s",
                 len(results),
                 "创作模式" if _CREATIVE_INTENT_RE.search(user_message) else "常规",
                 [r.get("id", "")[:8] for r in results])
        return injected, results


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_injection_instance: KnowledgeInjectionService | None = None
_injection_lock = threading.Lock()


def get_injection_service() -> KnowledgeInjectionService:
    """获取 RAG 注入服务单例（线程安全双重检查）。"""
    global _injection_instance
    if _injection_instance is None:
        with _injection_lock:
            if _injection_instance is None:
                _injection_instance = KnowledgeInjectionService()
    return _injection_instance
