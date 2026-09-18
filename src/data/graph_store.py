"""OmniSpace AI v2.5.0 知识图谱存储（TASK-055）。

职责：保存知识处理管线抽取的实体关系三元组（实体1, 关系, 实体2），
为 GET /v1/learn/knowledge/graph 提供节点/边查询。

存储设计（轻量级图存储，SQLite 两张表，不引入 networkx 依赖）：
  - kg_entities：实体节点（name 唯一，ref_count 累计出现次数，kind 粗分类）
  - kg_edges  ：关系边（src/dst 指向实体 name，kid 溯源知识条目，
              weight 累计权重，(src,dst,relation,kid) 唯一去重）

降级策略（与 fts_store 一致）：数据库不可用时降级为内存字典存储，
进程重启后清空，不抛异常。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import Database

log = logging.getLogger("omnispace.graph")

# ── DDL ──────────────────────────────────────────────────────
_GRAPH_DDL = """
CREATE TABLE IF NOT EXISTS kg_entities (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL DEFAULT 'entity',
    ref_count  INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kg_entities_name ON kg_entities(name);

CREATE TABLE IF NOT EXISTS kg_edges (
    id         TEXT PRIMARY KEY,
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    relation   TEXT NOT NULL DEFAULT '相关',
    kid        TEXT NOT NULL DEFAULT '',
    weight     REAL NOT NULL DEFAULT 1.0,
    created_at REAL NOT NULL DEFAULT 0,
    UNIQUE(src, dst, relation, kid)
);
CREATE INDEX IF NOT EXISTS idx_kg_edges_kid ON kg_edges(kid);
CREATE INDEX IF NOT EXISTS idx_kg_edges_src ON kg_edges(src);
CREATE INDEX IF NOT EXISTS idx_kg_edges_dst ON kg_edges(dst);
"""

# 查询上限（防止全图爆炸）
MAX_GRAPH_NODES = 200
MAX_GRAPH_EDGES = 400
MAX_ENTITY_NAME = 60


def _norm_entity(name: str) -> str:
    """实体名归一化：去空白/截断/去首尾标点。"""
    # strip 按字符集剥离首尾标点，非子串匹配，属预期用法
    n = (name or "").strip().strip(  # noqa: B005
        "，。；：、""''（）()【】[]<>《》 \t\r\n")
    n = " ".join(n.split())
    return n[:MAX_ENTITY_NAME]


class GraphStore:
    """知识图谱存储（线程安全单例，见 get_graph_store()）。"""

    def __init__(self, db: Database | None = None) -> None:
        self._lock = threading.Lock()
        if db is None:
            from .database import get_db_safe
            db = get_db_safe()
        self._db = db
        # 内存降级镜像
        self._mem_entities: dict[str, dict] = {}   # name -> row
        self._mem_edges: dict[str, dict] = {}      # edge key -> row
        if self._db is None:
            log.warning("数据库不可用，知识图谱降级为内存存储")
            return
        try:
            self._db.executescript(_GRAPH_DDL)
            log.info("kg_entities/kg_edges 图谱表就绪")
        except Exception as exc:  # noqa: BLE001
            log.warning("图谱建表失败，降级内存存储: %s", exc, exc_info=True)
            self._db = None

    # ── 写入 ─────────────────────────────────────────────────

    def add_triples(self, kid: str, triples: list[tuple[str, str, str]]) -> int:
        """写入一条知识抽取出的三元组列表，返回实际入库边数。

        triples 元素: (实体1, 关系, 实体2)。实体自动 upsert（ref_count 累加），
        边按 (src,dst,relation,kid) 去重（重复命中仅累加 weight）。
        """
        if not kid or not triples:
            return 0
        now = time.time()
        added = 0
        with self._lock:
            for e1, rel, e2 in triples:
                s, d = _norm_entity(e1), _norm_entity(e2)
                r = (rel or "相关").strip()[:20] or "相关"
                if not s or not d or s == d:
                    continue
                try:
                    self._upsert_entity(s, now)
                    self._upsert_entity(d, now)
                    self._upsert_edge(s, d, r, kid, now)
                    added += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug("三元组入库失败（跳过）: %s", exc)
        if added:
            log.debug("知识 %s 三元组入库 %d 条", kid[:8], added)
        return added

    def _upsert_entity(self, name: str, now: float) -> None:
        if self._db is not None:
            self._db.sql(
                "INSERT INTO kg_entities (id, name, kind, ref_count, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "ref_count=ref_count+1, updated_at=excluded.updated_at",
                (uuid.uuid4().hex, name, "entity", 1, now, now))
            return
        row = self._mem_entities.get(name)
        if row is None:
            self._mem_entities[name] = {
                "id": uuid.uuid4().hex, "name": name, "kind": "entity",
                "ref_count": 1, "created_at": now, "updated_at": now}
        else:
            row["ref_count"] = int(row.get("ref_count", 0)) + 1
            row["updated_at"] = now

    def _upsert_edge(self, src: str, dst: str, relation: str,
                     kid: str, now: float) -> None:
        if self._db is not None:
            self._db.sql(
                "INSERT INTO kg_edges (id, src, dst, relation, kid, weight, "
                "created_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(src, dst, relation, kid) DO UPDATE SET "
                "weight=weight+1.0",
                (uuid.uuid4().hex, src, dst, relation, kid, 1.0, now))
            return
        key = f"{src}{dst}{relation}{kid}"
        row = self._mem_edges.get(key)
        if row is None:
            self._mem_edges[key] = {
                "id": uuid.uuid4().hex, "src": src, "dst": dst,
                "relation": relation, "kid": kid, "weight": 1.0,
                "created_at": now}
        else:
            row["weight"] = float(row.get("weight", 1.0)) + 1.0

    # ── 删除（知识条目删除时级联）─────────────────────────────

    def delete_by_kid(self, kid: str) -> int:
        """删除某知识条目产生的所有边；随后清理不再被任何边引用的孤立实体。"""
        if not kid:
            return 0
        removed = 0
        with self._lock:
            if self._db is not None:
                try:
                    removed = self._db.delete("kg_edges", "kid=?", (kid,))
                    # 孤立实体清理（无出边且无入边）
                    self._db.sql(
                        "DELETE FROM kg_entities WHERE name NOT IN "
                        "(SELECT src FROM kg_edges UNION SELECT dst FROM kg_edges)",
                        ())
                except Exception as exc:  # noqa: BLE001
                    log.warning("图谱级联删除失败: %s", exc, exc_info=True)
            else:
                keys = [k for k, e in self._mem_edges.items()
                        if e.get("kid") == kid]
                for k in keys:
                    self._mem_edges.pop(k, None)
                removed = len(keys)
                used = set()
                for e in self._mem_edges.values():
                    used.add(e["src"])
                    used.add(e["dst"])
                for name in [n for n in self._mem_entities if n not in used]:
                    self._mem_entities.pop(name, None)
        return removed

    # ── 查询 ─────────────────────────────────────────────────

    def graph(self, kid: str | None = None,
              max_nodes: int = MAX_GRAPH_NODES) -> dict[str, Any]:
        """查询图谱数据（nodes/edges）。

        kid 非空：返回该知识条目直接关系 + 一跳邻居扩展（共享实体的其他边）。
        kid 为空：返回全局图谱（按 ref_count/weight 取 Top-N）。
        """
        max_nodes = max(1, min(int(max_nodes or MAX_GRAPH_NODES), 1000))
        max_edges = max_nodes * 2
        edges = self._load_edges(kid, max_edges)
        if kid:
            edges = self._expand_one_hop(edges, max_edges)
        edges = edges[:max_edges]
        nodes = self._nodes_for_edges(edges, max_nodes)
        return {
            "nodes": nodes,
            "edges": [{"id": e["id"], "source": e["src"], "target": e["dst"],
                       "relation": e["relation"], "weight": e["weight"],
                       "kid": e["kid"]} for e in edges],
            "kid": kid or "",
            "total_entities": self.count_entities(),
            "total_edges": self.count_edges(),
        }

    def _load_edges(self, kid: str | None, limit: int) -> list[dict]:
        if self._db is not None:
            try:
                if kid:
                    rows = self._db.query(
                        "SELECT id, src, dst, relation, kid, weight, created_at"
                        " FROM kg_edges WHERE kid=? "
                        "ORDER BY weight DESC, created_at DESC LIMIT ?",
                        (kid, limit))
                else:
                    rows = self._db.query(
                        "SELECT id, src, dst, relation, kid, weight, created_at"
                        " FROM kg_edges "
                        "ORDER BY weight DESC, created_at DESC LIMIT ?",
                        (limit,))
                return [dict(r) for r in rows]
            except Exception as exc:  # noqa: BLE001
                log.warning("图谱边查询失败: %s", exc, exc_info=True)
                return []
        edges = list(self._mem_edges.values())
        if kid:
            edges = [e for e in edges if e.get("kid") == kid]
        edges.sort(key=lambda e: (e.get("weight", 0), e.get("created_at", 0)),
                   reverse=True)
        return edges[:limit]

    def _expand_one_hop(self, edges: list[dict], limit: int) -> list[dict]:
        """一跳邻居扩展：把共享实体的其他边并入（kid 视图下展示上下文）。"""
        if not edges:
            return edges
        names = {e["src"] for e in edges} | {e["dst"] for e in edges}
        seen = {e["id"] for e in edges}
        extra: list[dict] = []
        if self._db is not None:
            try:
                marks = ",".join("?" * len(names))
                rows = self._db.query(
                    f"SELECT id, src, dst, relation, kid, weight, created_at"
                    f" FROM kg_edges WHERE src IN ({marks}) "
                    f"OR dst IN ({marks}) ORDER BY weight DESC LIMIT ?",
                    tuple(names) + tuple(names) + (limit,))
                extra = [dict(r) for r in rows]
            except Exception:  # noqa: BLE001
                extra = []
        else:
            extra = [e for e in self._mem_edges.values()
                     if e["src"] in names or e["dst"] in names]
            extra.sort(key=lambda e: e.get("weight", 0), reverse=True)
        for e in extra:
            if e["id"] not in seen and len(edges) < limit:
                edges.append(e)
                seen.add(e["id"])
        return edges

    def _nodes_for_edges(self, edges: list[dict], max_nodes: int) -> list[dict]:
        names: list[str] = []
        seen: set[str] = set()
        for e in edges:
            for n in (e["src"], e["dst"]):
                if n not in seen:
                    seen.add(n)
                    names.append(n)
        names = names[:max_nodes]
        info: dict[str, dict] = {}
        if self._db is not None and names:
            try:
                marks = ",".join("?" * len(names))
                rows = self._db.query(
                    f"SELECT id, name, kind, ref_count, created_at, updated_at"
                    f" FROM kg_entities WHERE name IN ({marks})",
                    tuple(names))
                info = {r["name"]: dict(r) for r in rows}
            except Exception:  # noqa: BLE001
                info = {}
        elif not self._db:
            info = {n: self._mem_entities[n]
                    for n in names if n in self._mem_entities}
        return [{"id": (info.get(n) or {}).get("id", n),
                 "name": n,
                 "kind": (info.get(n) or {}).get("kind", "entity"),
                 "ref_count": int((info.get(n) or {}).get("ref_count", 1))}
                for n in names]

    # ── 统计 ─────────────────────────────────────────────────

    def count_entities(self) -> int:
        if self._db is not None:
            try:
                rows = self._db.query("SELECT COUNT(*) AS c FROM kg_entities", ())
                return int(rows[0]["c"]) if rows else 0
            except Exception:  # noqa: BLE001
                return 0
        return len(self._mem_entities)

    def count_edges(self) -> int:
        if self._db is not None:
            try:
                rows = self._db.query("SELECT COUNT(*) AS c FROM kg_edges", ())
                return int(rows[0]["c"]) if rows else 0
            except Exception:  # noqa: BLE001
                return 0
        return len(self._mem_edges)

    def stats(self) -> dict:
        return {"entities": self.count_entities(),
                "edges": self.count_edges(),
                "backend": "sqlite" if self._db is not None else "memory"}


# ═══════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════

_gs_instance: GraphStore | None = None
_gs_lock = threading.Lock()


def get_graph_store() -> GraphStore:
    """获取图谱存储单例（线程安全）。"""
    global _gs_instance
    if _gs_instance is None:
        with _gs_lock:
            if _gs_instance is None:
                _gs_instance = GraphStore()
    return _gs_instance


def reset_graph_store() -> None:
    """重置单例（测试用）。"""
    global _gs_instance
    with _gs_lock:
        _gs_instance = None
# 本项目仅供学习使用，商业授权请+Q 3559331368
