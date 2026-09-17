"""OmniSpace AI v2.1 向量数据库（规格 §3.3 知识学习 / 向量检索）。

优先使用 ChromaDB 进行语义检索；不可用时降级为基于关键词的内存存储。
提供 add / embed / search 方法供知识学习模块调用。

规格引用：
  - §3.3 知识学习：文档导入 → 向量化 → 检索增强生成（RAG）
  - §7 model_cache：向量模型缓存
  - §14 约束1：向量数据本地持久化
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from typing import Any

from ..config import DATA_DIR, MODELS_DIR

log = logging.getLogger("omnispace.vector")

# 向量持久化目录
VECTOR_DIR = DATA_DIR / "vector_store"

# ── 嵌入模型配置（TASK-033：BGE-large-zh 本地加载）─────────────
# 本地模型路径（1024 维，normalize_embeddings=True）
EMBED_MODEL_PATH = MODELS_DIR / "embed" / "bge-large-zh"
EMBED_MODEL_NAME = "bge-large-zh"
EMBED_DIM = 1024
# 哈希向量回退维度（与 BGE 维度保持一致，避免同一集合内维度混杂）
FALLBACK_DIM = 1024

# ── ChromaDB 可用性探测 ──────────────────────────────────────
_CHROMA_AVAILABLE = False
try:
    import chromadb  # type: ignore
    from chromadb.config import Settings  # type: ignore
    _CHROMA_AVAILABLE = True
except Exception:  # pragma: no cover - 降级路径
    _CHROMA_AVAILABLE = False
    log.info("ChromaDB 不可用，向量数据库降级为内存关键词检索")

# ── 嵌入模型可用性探测 ───────────────────────────────────────
_EMBED_AVAILABLE = False
try:
    from sentence_transformers import SentenceTransformer  # type: ignore
    _EMBED_AVAILABLE = True
except Exception:  # pragma: no cover - 降级路径
    log.debug("<module>: 降级忽略", exc_info=True)


# ═══════════════════════════════════════════════════════════════════
#  降级方案：基于关键词的内存向量存储
# ═══════════════════════════════════════════════════════════════════

class _InMemoryVectorStore:
    """ChromaDB 不可用时的降级存储：基于 TF-IDF 风格的词频余弦相似度。"""

    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """简易分词：英文按词、中文按字（bigram 补充）。"""
        text = text.lower()
        tokens = re.findall(r"[a-z]+|\u4e00-\u9fff", text)
        # 中文按双字组合
        cjk_chars = re.findall(r"[\u4e00-\u9fff]", text)
        bigrams = [cjk_chars[i] + cjk_chars[i + 1]
                    for i in range(len(cjk_chars) - 1)]
        return tokens + bigrams

    @staticmethod
    def _term_freq(tokens: list[str]) -> dict[str, float]:
        tf: dict[str, float] = {}
        total = len(tokens) or 1
        for t in tokens:
            tf[t] = tf.get(t, 0.0) + 1.0
        for k in tf:
            tf[k] /= total
        return tf

    @staticmethod
    def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
        keys = a.keys() & b.keys()
        if not keys:
            return 0.0
        dot = sum(a[k] * b[k] for k in keys)
        na = math.sqrt(sum(v * v for v in a.values())) or 1.0
        nb = math.sqrt(sum(v * v for v in b.values())) or 1.0
        return dot / (na * nb)

    def add(self, documents: list[str], ids: list[str] | None = None,
            metadatas: list[dict] | None = None) -> None:
        """与 VectorDB 统一接口对齐：add(documents, ids=None, metadatas=None)。"""
        with self._lock:
            if ids is None:
                ids = [hashlib.md5(d.encode()).hexdigest()[:16]
                       for d in documents]
            metadatas = metadatas or [{} for _ in documents]
            for i, doc_id in enumerate(ids):
                self._docs.append({
                    "id": doc_id,
                    "text": documents[i],
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                    "tf": self._term_freq(self._tokenize(documents[i])),
                })

    def query(self, query_text: str, n_results: int = 5) -> list[dict]:
        qtf = self._term_freq(self._tokenize(query_text))
        scored = []
        with self._lock:
            for doc in self._docs:
                score = self._cosine(qtf, doc["tf"])
                scored.append((score, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"id": doc["id"], "document": doc["text"],
             "metadata": doc["metadata"], "distance": 1.0 - score}
            for score, doc in scored[:n_results] if score > 0
        ]

    def delete(self, ids: list[str]) -> None:
        with self._lock:
            id_set = set(ids)
            self._docs = [d for d in self._docs if d["id"] not in id_set]

    def count(self) -> int:
        with self._lock:
            return len(self._docs)

    # ── 统一接口别名（作为 VectorDB 的直接替代品时可 duck-typing）──
    def search(self, query: str, n_results: int = 5,
               where: dict | None = None) -> list[dict]:
        """统一接口别名（忽略 where 过滤）。"""
        return self.query(query, n_results)

    def disk_usage_bytes(self) -> int:
        """内存存储无磁盘占用，返回 0。"""
        return 0


# ═══════════════════════════════════════════════════════════════════
#  向量数据库统一接口
# ═══════════════════════════════════════════════════════════════════

class VectorDB:
    """向量数据库统一接口，自动选择 ChromaDB 或内存降级方案。"""

    def __init__(self, collection_name: str = "omnispace_knowledge") -> None:
        self._collection_name = collection_name
        self._lock = threading.Lock()
        self._embed_model = None
        self._embed_backend = "hash"   # bge-large-zh | hash
        self._client = None
        self._collection = None
        self._fallback = _InMemoryVectorStore()
        self._use_chroma = _CHROMA_AVAILABLE
        self._init_store()

    def _init_store(self) -> None:
        """初始化存储后端。"""
        if not self._use_chroma:
            log.warning("向量数据库运行在降级模式（内存关键词检索）")
            return
        try:
            VECTOR_DIR.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(
                path=str(VECTOR_DIR),
                settings=Settings(anonymized_telemetry=False, allow_reset=True),
            )
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            log.info("ChromaDB 向量集合就绪: %s", self._collection_name)
        except Exception as exc:
            log.warning("ChromaDB 初始化失败，降级为内存存储: %s", exc)
            self._use_chroma = False
            self._client = None
            self._collection = None

    def _get_embed_model(self) -> Any:
        """懒加载嵌入模型（降级时返回 None）。

        TASK-033：加载本地 BGE-large-zh（models/embed/bge-large-zh，1024 维）。
        优先使用 CUDA（可用时），加载失败返回 None 由调用方走哈希回退。
        """
        if not _EMBED_AVAILABLE:
            return None
        if self._embed_model is None:
            if not EMBED_MODEL_PATH.exists():
                log.warning("嵌入模型目录不存在: %s，使用哈希向量回退",
                            EMBED_MODEL_PATH)
                return None
            device = "cpu"
            try:
                import torch  # type: ignore
                if torch.cuda.is_available():
                    device = "cuda"
            except Exception:  # noqa: BLE001 - torch 缺失时用 CPU
                log.debug("_get_embed_model: 降级忽略", exc_info=True)
            try:
                self._embed_model = SentenceTransformer(
                    str(EMBED_MODEL_PATH), device=device
                )
                self._embed_backend = EMBED_MODEL_NAME
                log.info("嵌入模型已加载: %s (device=%s, dim=%d)",
                         EMBED_MODEL_NAME, device, EMBED_DIM)
            except Exception as exc:
                log.warning("嵌入模型加载失败，使用哈希向量回退: %s", exc)
                return None
        return self._embed_model

    # ── 公开方法 ──────────────────────────────────────────────

    def warmup(self) -> None:
        """启动期预热：触发嵌入模型加载 + 一次前向推理（CUDA kernel 预编译）。

        避免首次 RAG 检索时叠加模型加载与 CUDA 初始化延迟（实测首次
        检索 1188~2858ms，远超 100ms 指标；预热后降至百毫秒内）。
        """
        self.embed("warmup")

    # ── GPU 池压缩支持（2026-08-23 显存锚定修复）─────────────────

    def park_embed_model(self) -> bool:
        """bge 嵌入模型临时停靠 CPU（供显存池压缩，见
        model_manager.deflate_cuda_pool）。

        大模型卸载后 empty_cache 只能释放无活跃块的 segment；bge
        1.3GB 活跃块散布在大 segment 中会把整个缓存池钉死（实测
        reserved 18.2GB 物理锚定）。临时停靠 → empty_cache 全段
        释放 → restore 回卡，即可归还。设备迁移失败返回 False。
        """
        m = self._embed_model
        if m is None:
            return True  # 未加载（哈希回退态），无需停靠
        try:
            if str(getattr(m, "device", "")).startswith("cuda"):
                m.to("cpu")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("bge 停靠 CPU 失败（池压缩跳过）: %s", exc)
            return False

    def restore_embed_model(self) -> None:
        """停靠后回卡（失败留在 CPU：RAG 退化为慢速但可用）。"""
        m = self._embed_model
        if m is None:
            return
        try:
            import torch  # type: ignore
            if torch.cuda.is_available():
                m.to("cuda")
        except Exception as exc:  # noqa: BLE001
            log.warning("bge 回卡失败（暂用 CPU 检索）: %s", exc)

    def embed(self, text: str) -> list[float]:
        """将文本转换为向量。

        优先使用 sentence-transformers；不可用时返回简单哈希向量。
        """
        model = self._get_embed_model()
        if model is not None:
            try:
                vec = model.encode(text, normalize_embeddings=True)
                return vec.tolist()
            except Exception as exc:
                log.warning("嵌入失败，使用哈希降级: %s", exc)
        # 降级：基于哈希的稀疏向量（固定维度 256）
        return self._hash_embed(text)

    @staticmethod
    def _hash_embed(text: str, dim: int = FALLBACK_DIM) -> list[float]:
        """基于哈希的简单向量（仅降级用，无语义能力）。

        维度默认与 BGE-large-zh（1024）一致，保证同一集合内向量维度统一。
        """
        vec = [0.0] * dim
        for token in re.findall(r"\w+", text.lower()):
            h = int(hashlib.md5(token.encode()).hexdigest(), 16)
            vec[h % dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入（性能优化：单次模型前向替代逐条 encode，降低调用开销）。

        使用 sentence-transformers 的批式 encode(列表)；若模型不接受列表
        （降级/包装层限制）则自动回退为逐条 embed，语义结果一致。
        """
        if not texts:
            return []
        model = self._get_embed_model()
        if model is not None:
            try:
                vecs = model.encode(texts, normalize_embeddings=True)
                return [v.tolist() for v in vecs]
            except Exception:
                log.debug("_embed_batch: 降级忽略", exc_info=True)
        return [self.embed(t) for t in texts]

    def add(self, documents: list[str], ids: list[str] | None = None,
            metadatas: list[dict] | None = None) -> list[str]:
        """批量添加文档到向量库，返回实际使用的 ID 列表。

        Args:
            documents: 文档文本列表
            ids: 可选 ID 列表，不提供则自动生成
            metadatas: 可选元数据列表

        Returns:
            实际写入的 ID 列表
        """
        if not documents:
            return []
        if ids is None:
            ids = [hashlib.md5(doc.encode()).hexdigest()[:16]
                   for doc in documents]
        metadatas = metadatas or [{} for _ in documents]

        if self._use_chroma and self._collection is not None:
            try:
                embeddings = self._embed_batch(documents)
                self._collection.upsert(
                    ids=ids,
                    documents=documents,
                    metadatas=metadatas,
                    embeddings=embeddings,
                )
                return ids
            except Exception as exc:
                log.warning("ChromaDB 写入失败，降级到内存: %s", exc)
                self._use_chroma = False

        # 降级路径
        self._fallback.add(documents, ids=ids, metadatas=metadatas)
        return ids

    def search(self, query: str, n_results: int = 5,
               where: dict | None = None) -> list[dict]:
        """语义检索，返回最相关的文档列表。

        Args:
            query: 查询文本
            n_results: 返回条数
            where: 可选元数据过滤条件

        Returns:
            [{"id":..., "document":..., "metadata":..., "distance":...}, ...]
        """
        if self._use_chroma and self._collection is not None:
            try:
                query_emb = self.embed(query)
                kwargs: dict[str, Any] = {
                    "query_embeddings": [query_emb],
                    "n_results": n_results,
                }
                if where:
                    kwargs["where"] = where
                result = self._collection.query(**kwargs)
                items = []
                ids_list = result.get("ids", [[]])
                docs_list = result.get("documents", [[]])
                metas_list = result.get("metadatas", [[]])
                dists_list = result.get("distances", [[]])
                for i in range(len(ids_list[0])):
                    items.append({
                        "id": ids_list[0][i],
                        "document": docs_list[0][i] if i < len(docs_list[0]) else "",
                        "metadata": metas_list[0][i] if i < len(metas_list[0]) else {},
                        "distance": dists_list[0][i] if i < len(dists_list[0]) else 0.0,
                    })
                return items
            except Exception as exc:
                log.warning("ChromaDB 查询失败，降级到内存: %s", exc)
                self._use_chroma = False

        # 降级路径
        return self._fallback.query(query, n_results)

    def delete(self, ids: list[str]) -> None:
        """按 ID 删除文档。"""
        if self._use_chroma and self._collection is not None:
            try:
                self._collection.delete(ids=ids)
                return
            except Exception as exc:
                log.warning("ChromaDB 删除失败: %s", exc)
        self._fallback.delete(ids)

    def count(self) -> int:
        """返回当前集合的文档总数。"""
        if self._use_chroma and self._collection is not None:
            try:
                return self._collection.count()
            except Exception:
                log.debug("count: 降级忽略", exc_info=True)
        return self._fallback.count()

    @property
    def backend(self) -> str:
        """返回当前后端名称（chroma / memory）。"""
        return "chroma" if self._use_chroma else "memory"

    @property
    def embed_backend(self) -> str:
        """返回当前嵌入后端名称（bge-large-zh / hash）。

        嵌入模型为懒加载：未触发过 embed() 时返回 "hash"（未加载状态）。
        """
        if self._embed_model is not None:
            return self._embed_backend
        return "hash"

    @property
    def embedding_dim(self) -> int:
        """当前向量维度（BGE 与哈希回退均为 1024）。"""
        return EMBED_DIM

    @staticmethod
    def disk_usage_bytes() -> int:
        """统计向量持久化目录（data/vector_store）的磁盘占用字节数。"""
        total = 0
        try:
            if VECTOR_DIR.exists():
                for p in VECTOR_DIR.rglob("*"):
                    if p.is_file():
                        try:
                            total += p.stat().st_size
                        except OSError:
                            continue
        except Exception:  # noqa: BLE001 - 统计失败不影响主流程
            log.debug("disk_usage_bytes: 降级忽略", exc_info=True)
        return total


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_vdb_instance: VectorDB | None = None
_vdb_lock = threading.Lock()


def get_vector_db() -> VectorDB:
    """获取全局向量数据库单例。"""
    global _vdb_instance
    if _vdb_instance is None:
        with _vdb_lock:
            if _vdb_instance is None:
                _vdb_instance = VectorDB()
    return _vdb_instance
# 本项目仅供学习使用，商业授权请+Q 3559331368
