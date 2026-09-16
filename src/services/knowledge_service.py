"""OmniSpace AI v2.3 知识处理管线服务（TASK-033 知识处理管线 / TASK-045 知识去重）。

完整管线: 过滤 → 分段 → 提取 → 质量评估 → SimHash 去重 → 向量化入库。

降级策略（规格 §4.1 容错降级）：
  - 缺 beautifulsoup4    → 正则过滤回退（日志标注）
  - 缺 chromadb          → vector_db 内部内存 TF-IDF 回退
  - 缺嵌入模型           → vector_db 内部哈希向量回退
  - 未注入 llm_extractor → 规则提取回退（离线可用）
  - 数据库不可用         → 内存元数据存储回退

SimHash（TASK-045）：自实现 64 位（字符级 3-gram + md5 权重），不依赖三方库。
  相似度 > 0.9 → skip（重复），0.7~0.9 → merge（保留信息更完整版本），< 0.7 → new。

知识生命周期：青年(<7天) / 成熟(<30天) / 沉淀(<90天) / 淘汰(≥90天或长期低质)。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..data.vector_db import VectorDB

from ..data.database import get_db_safe
from ..data.fts_store import get_fts_store
from ..data.graph_store import get_graph_store
from ..data.vector_db import get_vector_db
from . import knowledge_quality_gate

log = logging.getLogger("omnispace.knowledge")

# ── 可选依赖探测 ─────────────────────────────────────────────
try:
    from bs4 import BeautifulSoup  # type: ignore
    _BS4_AVAILABLE = True
except Exception:  # pragma: no cover - 降级路径
    BeautifulSoup = None  # type: ignore
    _BS4_AVAILABLE = False
    log.warning("beautifulsoup4 不可用，filter_content 降级为正则过滤")

# ── 常量 ─────────────────────────────────────────────────────
MAX_KNOWLEDGE_COUNT = 100_000      # 知识库容量上限（TASK-033）
CLEANUP_BATCH = 100                # 超出上限时每次清理的最旧条数
SEGMENT_MAX_CHARS = 500            # 长段切分阈值（TASK-033：~500字/段）
DENSITY_THRESHOLD = 0.3            # 信息密度阈值（>0.3 保留）
RELEVANCE_THRESHOLD = 0.25         # 相关性阈值（≥0.25 保留；主题字/词覆盖率均值）
SIM_SKIP = 0.9                     # SimHash 相似度 > 0.9 → 跳过
SIM_MERGE = 0.7                    # 0.7~0.9 → 合并
DEDUP_SCAN_LIMIT = 5000            # 去重扫描的最近元数据条数上限

# §4.3 自适应：知识库接近容量上限（10 万条）时收紧去重 skip 阈值
DEDUP_THRESHOLD_NORMAL = 0.85      # 常规去重 skip 阈值
DEDUP_THRESHOLD_NEAR_FULL = 0.8    # 接近满容量时的去重 skip 阈值
NEAR_FULL_COUNT = 90_000           # 接近满容量判定线（条）

# 生命周期阶段
LIFECYCLE_YOUNG = "青年"
LIFECYCLE_MATURE = "成熟"
LIFECYCLE_DEPOSITED = "沉淀"
LIFECYCLE_OBSOLETE = "淘汰"

# 内容过滤：需要剔除的标签与 class/id 特征
_DROP_TAGS = ("nav", "footer", "script", "style", "aside", "noscript",
              "iframe", "form", "button", "select", "textarea")
_DROP_CLASS_RE = re.compile(
    r"^(ad|ads|advert\w*|banner|cookie\w*|popup|pop\w*ad|sidebar|comment\w*|"
    r"nav\w*|footer\w*|header|menu\w*|breadcrumb\w*|share\w*|social\w*|"
    r"recommend\w*|related\w*|promo\w*|sponsor\w*)$", re.I)
_DROP_SUBSTR_RE = re.compile(r"advert|cookie|\.ad\b|\bad[-_]|[-_]ad\b", re.I)

_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
_YEAR_RE = re.compile(r"(19|20)\d{2}\s*年?")
_DEF_RE = re.compile(
    r"^(.{2,40}?)(?:是|是指|指的是|被称为|称为|定义为|是一种|是一类)(.{4,})$")
_QA_RE = re.compile(r"什么|为什么|为何|如何|怎么|怎样|哪些|吗|呢|[?？]")
_FACT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|年|月|日|亿|万|倍|个|条|次|人|元|公里|千克|kg|"
    r"GB|MB|TB|ms|毫秒|秒|分钟|小时)")
_METHOD_RE = re.compile(
    r"步骤|流程|首先|其次|再次|然后|最后|方法|第一步|第二步|第三阶段?|阶段")
_CASE_RE = re.compile(r"例如|比如|案例|实例|为例|举例来说|举个例子")

_CJK_RE = re.compile(r"[一-鿿]")
_WORD_RE = re.compile(r"[a-z0-9]+|[一-鿿]")

# ── 实体关系三元组抽取规则（TASK-055，规则回退路径）─────────────
# 句法模式：(实体1 捕获组, 关系词, 实体2 捕获组)。实体2 截取到句读为止。
_TRIPLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(.{2,30}?)(?:是指|指的是|被称为|称为|定义为|"
                r"是一种|是一类|是一个|是)(.{2,40}?)$"), "定义"),
    (re.compile(r"^(.{2,30}?)(?:包括|包含|涵盖)(.{2,40}?)$"), "包含"),
    (re.compile(r"^(.{2,30}?)属于(.{2,40}?)$"), "属于"),
    (re.compile(r"^(.{2,30}?)由(.{2,30}?)(?:组成|构成)$"), "组成"),
    (re.compile(r"^(.{2,30}?)(?:用于|用来|适用于)(.{2,40}?)$"), "用途"),
    (re.compile(r"^(.{2,30}?)(?:需要|依赖|依赖于)(.{2,40}?)$"), "依赖"),
    (re.compile(r"^(.{2,30}?)(?:支持|兼容)(.{2,40}?)$"), "支持"),
    (re.compile(r"^(.{2,30}?)(?:产生|导致|引起|造成)(.{2,40}?)$"), "影响"),
    (re.compile(r"^(.{2,30}?)(?:优于|强于|好于)(.{2,40}?)$"), "比较"),
]
# 实体串中不允许出现的虚词片段（粗过滤，降低误抽）
_ENTITY_BAD_RE = re.compile(r"[的了在是和与及或等吗呢吧啊嗯噢]|^(我们|你们|他们|它们|"
                            r"这个|那个|这些|那些|可以|没有|什么|怎么)")
MAX_TRIPLES_PER_ITEM = 10     # 单条知识入库的三元组上限
_HAS_CJK_RE = re.compile(r"[\u4e00-\u9fa5]")
# 陈述兜底的主语截断（P2-1）：单字停用（虚词+高频触发字）与双字
# 截断词（时间副词/常见动词），主语片段遇之首截，防「高温会加速
# 化学反应」「系统每天凌晨自动备份」整段进实体位
_SUBJECT_STOP_CHARS = ("的了在是和与及或等会能将被不，。；：、！？")
_SUBJECT_CUT_WORDS = frozenset((
    "每天", "每日", "经常", "通常", "常常", "有时", "已经", "正在",
    "释放", "吸收", "生成", "消耗", "转化", "驱动", "加速", "自动",
))


def _subject_head(s: str) -> str:
    """句首主语片段：遇停用字/截断词截断，上限 16 字。"""
    for i, ch in enumerate(s):
        if ch in _SUBJECT_STOP_CHARS or s[i:i + 2] in _SUBJECT_CUT_WORDS:
            return s[:i]
        if i >= 16:
            return s[:16]
    return s[:16]


def extract_triples_rule(text: str, topic: str = "",
                         title: str = "") -> list[tuple[str, str, str]]:
    """规则法实体关系三元组抽取（TASK-055 离线回退路径）。

    对每条句子依次尝试句法模式；实体经虚词过滤与长度归一；
    末尾追加 (标题/首实体, 相关主题, topic) 主题归属边。
    返回去重后的 [(实体1, 关系, 实体2)]，上限 MAX_TRIPLES_PER_ITEM。
    """
    triples: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def _push(e1: str, rel: str, e2: str) -> None:
        e1, e2 = e1.strip(), e2.strip()
        if not e1 or not e2 or e1 == e2:
            return
        if _ENTITY_BAD_RE.search(e1) or _ENTITY_BAD_RE.search(e2):
            return
        if len(e1) < 2 or len(e2) < 2:
            return
        key = (e1, rel, e2)
        if key not in seen:
            seen.add(key)
            triples.append(key)

    for sent in _SENT_SPLIT_RE.split(text or ""):
        s = sent.strip().strip("，。；：、 ")
        if not (4 <= len(s) <= 120):
            continue
        matched = False
        for pat, rel in _TRIPLE_PATTERNS:
            m = pat.match(s)
            if m:
                _push(m.group(1), rel, m.group(2))
                matched = True
                break
        if not matched:
            # P2-1 修复（2026-09-02）：普通陈述句兜底——无句式命中的
            # 句子此前直接丢弃（「高温会加速化学反应」类事实从图谱
            # 消失、检索不可达）。取句首主语片段为实体（截断规则见
            # _subject_head），全句为宾语，关系记「陈述」；宾语是完整
            # 句子必含虚词，绕过 _push 的虚词实体过滤（主语仍过滤）。
            # 仅中文句兜底（英文无停用字切分依据，交给 LLM 主链）。
            if _HAS_CJK_RE.search(s):
                head = _subject_head(s)
                obj = s[:60]
                if (2 <= len(head) <= 16 and head != obj
                        and not _ENTITY_BAD_RE.search(head)):
                    key = (head, "陈述", obj)
                    if key not in seen:
                        seen.add(key)
                        triples.append(key)
        if len(triples) >= MAX_TRIPLES_PER_ITEM:
            break

    # 主题归属边：知识标题（或首句主语）→ 主题
    topic = (topic or "").strip()
    anchor = (title or "").strip()
    if topic and anchor and anchor != topic:
        _push(anchor, "相关主题", topic)
    return triples[:MAX_TRIPLES_PER_ITEM]


# ═══════════════════════════════════════════════════════════════════
#  数据模型
# ═══════════════════════════════════════════════════════════════════

@dataclass
class CleanContent:
    """过滤后的干净内容。"""
    title: str = ""
    text: str = ""
    blocks: list[dict] = field(default_factory=list)
    # blocks 元素: {"kind": "heading"|"para"|"table"|"image",
    #              "level": int(仅 heading), "text": str}
    tables: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)   # 图片 alt 文本

    def to_dict(self) -> dict:
        return {"title": self.title, "text": self.text, "blocks": self.blocks,
                "tables": self.tables, "images": self.images}


@dataclass
class Segment:
    """内容分段（保留标题归属）。"""
    heading: str = ""
    level: int = 0
    text: str = ""
    index: int = 0
    part: int = 0          # 长段切分后的分片序号（0 表示未切分）

    def to_dict(self) -> dict:
        return {"heading": self.heading, "level": self.level,
                "text": self.text, "index": self.index, "part": self.part}


@dataclass
class Knowledge:
    """结构化知识条目。type ∈ concept/qa/fact/methodology/case"""
    id: str = ""
    type: str = "concept"
    content: str = ""
    topic: str = ""
    source_url: str = ""
    title: str = ""
    quality_score: float = 0.0
    simhash: str = ""
    lifecycle: str = LIFECYCLE_YOUNG
    created_at: float = 0.0
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex
        if not self.created_at:
            self.created_at = time.time()

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "content": self.content,
                "topic": self.topic, "source_url": self.source_url,
                "title": self.title, "quality_score": self.quality_score,
                "simhash": self.simhash, "lifecycle": self.lifecycle,
                "created_at": self.created_at, "extra": self.extra}


@dataclass
class QualityScore:
    """多维质量评分。"""
    density: float = 0.0        # 信息密度（>0.3 保留）
    relevance: float = 0.0      # 相关性（>0.5 保留）
    timeliness: float = 0.0     # 时效性
    total: float = 0.0
    passed: bool = False
    has_timestamp: bool = False

    def to_dict(self) -> dict:
        return {"density": self.density, "relevance": self.relevance,
                "timeliness": self.timeliness, "total": self.total,
                "passed": self.passed, "has_timestamp": self.has_timestamp}


# ═══════════════════════════════════════════════════════════════════
#  SimHash（TASK-045，自实现 64 位，不依赖三方库）
# ═══════════════════════════════════════════════════════════════════

def simhash64(text: str) -> int:
    """计算文本的 64 位 SimHash（字符级 3-gram，md5 权重累加）。"""
    text = re.sub(r"\s+", "", (text or "").lower())
    if not text:
        return 0
    if len(text) < 3:
        grams = [text]
    else:
        grams = [text[i:i + 3] for i in range(len(text) - 2)]
    weights: dict[str, int] = {}
    for g in grams:
        weights[g] = weights.get(g, 0) + 1
    acc = [0.0] * 64
    for g, w in weights.items():
        h = int.from_bytes(
            hashlib.md5(g.encode("utf-8")).digest()[:8], "big")
        for i in range(64):
            acc[i] += w if (h >> i) & 1 else -w
    fp = 0
    for i in range(64):
        if acc[i] > 0:
            fp |= (1 << i)
    return fp


def hamming64(a: int, b: int) -> int:
    """64 位汉明距离。"""
    return bin((a ^ b) & 0xFFFFFFFFFFFFFFFF).count("1")


def simhash_similarity(a: int | str, b: int | str) -> float:
    """SimHash 相似度 ∈ [0,1]：1 - 汉明距离/64。接受 int 或 16 进制字符串。"""
    if isinstance(a, str):
        a = int(a, 16) if a else 0
    if isinstance(b, str):
        b = int(b, 16) if b else 0
    return 1.0 - hamming64(a, b) / 64.0


def _tokens(text: str) -> list[str]:
    """简易分词：英文/数字按词，中文按字 + 双字 bigram。"""
    text = (text or "").lower()
    words = _WORD_RE.findall(text)
    cjk = _CJK_RE.findall(text)
    bigrams = [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
    return words + bigrams


# ═══════════════════════════════════════════════════════════════════
#  知识处理管线服务
# ═══════════════════════════════════════════════════════════════════

_KNOWLEDGE_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_meta (
    id           TEXT PRIMARY KEY,
    content      TEXT NOT NULL DEFAULT '',
    type         TEXT DEFAULT 'concept',
    title        TEXT DEFAULT '',
    topic        TEXT DEFAULT '',
    source_url   TEXT DEFAULT '',
    quality_score REAL DEFAULT 0,
    simhash      TEXT DEFAULT '',
    lifecycle    TEXT DEFAULT '青年',
    vector_id    TEXT DEFAULT '',
    access_count INTEGER NOT NULL DEFAULT 0,
    lang         TEXT DEFAULT '',
    created_at   REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_knowledge_meta_topic ON knowledge_meta(topic);
CREATE INDEX IF NOT EXISTS idx_knowledge_meta_created ON knowledge_meta(created_at);
"""


_CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")


def detect_lang(text: str) -> str:
    """LEARN-066：规则法语言判定（离线零依赖）。

    CJK（中日韩）字符占比 ≥20% → "zh"（覆盖日文/韩文样本的混合文本，
    本系统以中文知识为主）；含 CJK 但占比低 → "mixed"；否则 → "en"。
    """
    if not text:
        return ""
    sample = text[:2000]
    cjk = len(_CJK_RE.findall(sample))
    ratio = cjk / max(len(sample), 1)
    if ratio >= 0.20:
        return "zh"
    if cjk > 0:
        return "mixed"
    return "en"


class KnowledgeProcessingService:
    """知识处理管线（TASK-033/045）。单例，见 get_knowledge_service()。"""

    def __init__(self, llm_extractor: Callable[[str], str] | None = None,
                 vector_db: VectorDB | None = None) -> None:
        self._lock = threading.Lock()
        self._llm_extractor = llm_extractor
        self._vdb = vector_db if vector_db is not None else get_vector_db()
        self._db = get_db_safe()
        self._mem_meta: dict[str, dict] = {}   # 数据库不可用时的内存回退
        self._near_full = False                # §4.3 自适应：去重阈值收紧状态（首切记日志）
        if self._db is not None:
            try:
                self._db.executescript(_KNOWLEDGE_DDL)
                # LEARN-066：已存在库补 lang 列
                cols = {r["name"] for r in self._db.query(
                    "PRAGMA table_info(knowledge_meta)")}
                if "lang" not in cols:
                    self._db.sql(
                        "ALTER TABLE knowledge_meta ADD COLUMN lang"
                        " TEXT DEFAULT ''")
                log.info("knowledge_meta 表就绪")
            except Exception as exc:  # noqa: BLE001
                log.warning("knowledge_meta 建表失败，降级内存元数据: %s", exc)
                self._db = None
        else:
            log.warning("数据库不可用，知识元数据降级为内存存储")

    # ── 外部注入 ──────────────────────────────────────────────

    def set_llm_extractor(self, extractor: Callable[[str], str] | None) -> None:
        """运行时注入 LLM 提取器（callable(prompt)->str）。None 恢复规则模式。"""
        self._llm_extractor = extractor

    # ── Step 1: 内容过滤 ─────────────────────────────────────

    def filter_content(self, raw_html: str) -> CleanContent:
        """去除广告/导航/页脚/评论区/Cookie提示，保留标题/正文/表格/图片alt。

        空输入返回空 CleanContent，不抛异常。目标耗时 <50ms。
        """
        if not raw_html or not raw_html.strip():
            return CleanContent()
        if not _BS4_AVAILABLE:
            return self._filter_content_regex(raw_html)
        try:
            return self._filter_content_bs4(raw_html)
        except Exception as exc:  # noqa: BLE001 - 解析失败降级正则
            log.warning("bs4 解析失败，降级正则过滤: %s", exc)
            return self._filter_content_regex(raw_html)

    def _filter_content_bs4(self, raw_html: str) -> CleanContent:
        soup = BeautifulSoup(raw_html, "html.parser")
        # 1) 剔除标签
        for tag in soup.find_all(_DROP_TAGS):
            tag.decompose()
        # 2) 剔除 class/id 命中的节点
        for tag in list(soup.find_all(True)):
            classes = tag.get("class") or []
            ident = tag.get("id") or ""
            hit = any(_DROP_CLASS_RE.match(c) for c in classes)
            hit = hit or bool(ident and _DROP_CLASS_RE.match(ident))
            hit = hit or any(_DROP_SUBSTR_RE.search(c) for c in classes)
            if hit:
                tag.decompose()
        out = CleanContent()
        if soup.title and soup.title.string:
            out.title = soup.title.string.strip()
        root = soup.body or soup
        for el in root.find_all(
                ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li",
                 "table", "img", "article", "main"]):
            name = el.name
            if name in ("article", "main"):
                continue  # 容器标签，子元素会被单独遍历
            if name.startswith("h") and len(name) == 2 and name[1].isdigit():
                text = el.get_text(" ", strip=True)
                if text:
                    out.blocks.append({"kind": "heading",
                                       "level": int(name[1]), "text": text})
                    if not out.title and name == "h1":
                        out.title = text
            elif name in ("p", "li"):
                # 跳过嵌套在其他已处理 p/li 内的节点，避免重复
                if el.find_parent(["p", "li"]):
                    continue
                text = el.get_text(" ", strip=True)
                if text:
                    out.blocks.append({"kind": "para", "level": 0, "text": text})
            elif name == "table":
                if el.find_parent("table"):
                    continue
                text = el.get_text(" | ", strip=True)
                if text:
                    out.tables.append(text)
                    out.blocks.append({"kind": "table", "level": 0, "text": text})
            elif name == "img":
                alt = (el.get("alt") or "").strip()
                if alt:
                    out.images.append(alt)
                    out.blocks.append({"kind": "image", "level": 0,
                                       "text": f"[图片: {alt}]"})
        if not out.blocks:
            # 纯文本输入（无标签）：按行拆为段落块
            for line in soup.get_text("\n", strip=True).splitlines():
                line = line.strip()
                if line:
                    out.blocks.append({"kind": "para", "level": 0,
                                       "text": line})
        out.text = "\n".join(b["text"] for b in out.blocks)
        return out

    @staticmethod
    def _filter_content_regex(raw_html: str) -> CleanContent:
        """bs4 缺失时的正则过滤回退。"""
        html = raw_html
        html = re.sub(r"(?is)<(script|style|nav|footer|aside|noscript|iframe)"
                      r"\b[^>]*>.*?</\1>", " ", html)
        html = re.sub(r"(?is)<!--.*?-->", " ", html)
        out = CleanContent()
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        if m:
            out.title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        for m in re.finditer(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>", html):
            text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if text:
                out.blocks.append({"kind": "heading",
                                   "level": int(m.group(1)), "text": text})
                if not out.title and m.group(1) == "1":
                    out.title = text
        body = re.sub(r"(?is)<h[1-6][^>]*>.*?</h[1-6]>", " ", html)
        body = re.sub(r"(?is)<table\b[^>]*>(.*?)</table>",
                      lambda m: " " + re.sub(r"<[^>]+>", " | ", m.group(1)) + " ",
                      body)
        body = re.sub(r"(?is)<img\b[^>]*alt=[\"']([^\"']+)[\"'][^>]*>",
                      r" [图片: \1] ", body)
        body = re.sub(r"(?s)<[^>]+>", " ", body)
        body = re.sub(r"[ \t]+", " ", body)
        for line in body.splitlines():
            line = line.strip()
            if line:
                out.blocks.append({"kind": "para", "level": 0, "text": line})
        out.text = "\n".join(b["text"] for b in out.blocks)
        return out

    # ── Step 2: 内容分段 ─────────────────────────────────────

    def segment_content(self, content: CleanContent) -> list[Segment]:
        """按标题层级（h1~h3）分段，长段按 ~500 字切分，保留标题归属。"""
        if content is None or not content.blocks:
            return []
        segments: list[Segment] = []
        cur_heading = content.title or ""
        cur_level = 0
        buf: list[str] = []
        group = [-1]  # 段组计数器（闭包内可写）

        def _flush() -> None:
            text = "\n".join(buf).strip()
            buf.clear()
            if not text:
                return
            group[0] += 1
            for j, part_text in enumerate(self._split_long_text(text)):
                segments.append(Segment(heading=cur_heading, level=cur_level,
                                        text=part_text, index=group[0],
                                        part=j))

        for block in content.blocks:
            if block["kind"] == "heading" and block.get("level", 9) <= 3:
                _flush()
                cur_heading = block["text"]
                cur_level = block["level"]
            else:
                buf.append(block["text"])
        _flush()
        return segments

    @staticmethod
    def _split_long_text(text: str) -> list[str]:
        """长文本按 ~500 字在句子边界切分；无句读的超长句按阈值硬切。"""
        if len(text) <= SEGMENT_MAX_CHARS:
            return [text]
        parts: list[str] = []
        sentences = [s for s in _SENT_SPLIT_RE.split(text) if s and s.strip()]
        cur = ""
        for sent in sentences:
            # 单句即超阈值：先冲刷缓冲，再对该句按阈值硬切
            while len(sent) > SEGMENT_MAX_CHARS:
                if cur.strip():
                    parts.append(cur.strip())
                    cur = ""
                chunk = sent[:SEGMENT_MAX_CHARS].strip()
                if chunk:
                    parts.append(chunk)
                sent = sent[SEGMENT_MAX_CHARS:]
            if not sent.strip():
                continue
            if cur and len(cur) + len(sent) > SEGMENT_MAX_CHARS:
                parts.append(cur.strip())
                cur = sent
            else:
                cur += sent
        if cur.strip():
            parts.append(cur.strip())
        return parts or [text]

    # ── Step 3: 知识提取（双模式：LLM / 规则回退）──────────────

    def extract_knowledge(self, segment: Segment,
                          topic: str | None = None) -> list[Knowledge]:
        """从分段提取结构化知识（概念/QA对/事实/方法论/案例 五类）。

        若已注入 llm_extractor（callable(prompt)->str）则走 LLM 提取并解析 JSON；
        任何失败自动回退到规则提取，保证离线可用。
        """
        if segment is None or not segment.text.strip():
            return []
        topic = topic or ""
        if callable(self._llm_extractor):
            try:
                items = self._extract_with_llm(segment, topic)
                if items:
                    return items
            except Exception as exc:  # noqa: BLE001
                # 引擎未加载属预期内回退（离线/显存紧张），降级 debug 防刷屏；
                # 真正的 LLM 异常仍按 warning 记录。
                if "not ready" in str(exc):
                    log.debug("LLM 未就绪，回退规则提取: %s", exc)
                else:
                    log.warning("LLM 提取失败，回退规则提取: %s", exc)
        return self._extract_with_rules(segment, topic)

    def _extract_with_llm(self, segment: Segment,
                          topic: str) -> list[Knowledge]:
        prompt = (
            "请从以下文本中提取结构化知识，输出 JSON 数组，每个元素包含 "
            "type(concept/qa/fact/methodology/case) 与 content 字段。\n"
            f"主题: {topic or '通用'}\n文本:\n{segment.text}\nJSON:"
        )
        raw = self._llm_extractor(prompt)  # type: ignore[misc]
        m = re.search(r"\[.*\]", raw or "", re.S)
        if not m:
            raise ValueError("LLM 输出不含 JSON 数组")
        data = json.loads(m.group(0))
        items: list[Knowledge] = []
        for it in data:
            if not isinstance(it, dict):
                continue
            content = str(it.get("content", "")).strip()
            if len(content) < 4:
                continue
            ktype = str(it.get("type", "concept"))
            if ktype not in ("concept", "qa", "fact", "methodology", "case"):
                ktype = "concept"
            items.append(Knowledge(
                type=ktype, content=content, topic=topic,
                title=segment.heading,
                extra={"heading": segment.heading, "extractor": "llm",
                       **{k: v for k, v in it.items()
                          if k not in ("type", "content")}}))
        return items

    def _extract_with_rules(self, segment: Segment,
                            topic: str) -> list[Knowledge]:
        """规则提取回退：标题句、定义句（是/称为）、列表枚举、问答句型。"""
        text = segment.text
        sentences = [s.strip() for s in _SENT_SPLIT_RE.split(text)
                     if s and s.strip()]
        items: list[Knowledge] = []
        seen: set[str] = set()

        def _add(ktype: str, content: str, **extra: Any) -> None:
            content = content.strip()
            key = content[:64]
            if len(content) < 8 or key in seen:
                return
            seen.add(key)
            items.append(Knowledge(
                type=ktype, content=content, topic=topic,
                title=segment.heading,
                extra={"heading": segment.heading, "extractor": "rule",
                       **extra}))

        # 标题句 → 概念（标题 + 首句释义）
        if segment.heading and sentences:
            _add("concept", f"{segment.heading}：{sentences[0]}",
                 name=segment.heading, definition=sentences[0])

        for i, sent in enumerate(sentences):
            # 问答句型 → QA 对（问句 + 后续句作答）
            if _QA_RE.search(sent):
                answer = ""
                if i + 1 < len(sentences) and not _QA_RE.search(sentences[i + 1]):
                    answer = sentences[i + 1]
                content = f"问：{sent}\n答：{answer}" if answer else sent
                _add("qa", content, question=sent, answer=answer)
                continue
            # 定义句 → 概念
            m = _DEF_RE.match(sent)
            if m:
                _add("concept", sent, name=m.group(1).strip(),
                     definition=m.group(2).strip())
                continue
            # 案例
            if _CASE_RE.search(sent):
                _add("case", sent)
                continue
            # 方法论（含步骤/流程/序列词或枚举列表）
            if _METHOD_RE.search(sent) or re.search(
                    r"(?:^|[、，,])\s*(?:\d+[.、)]|[一二三四五六七八九十]+[、.])",
                    sent):
                _add("methodology", sent)
                continue
            # 事实（含数据/日期/统计）
            if _FACT_RE.search(sent):
                _add("fact", sent)
                continue
        return items

    # ── Step 4: 质量评估 ─────────────────────────────────────

    def evaluate_quality(self, knowledge: Knowledge,
                         topic: str | None = None) -> QualityScore:
        """多维质量评分：信息密度 / 相关性 / 时效性，输出 passed 判定。"""
        content = knowledge.content or ""
        length = len(content)
        # 信息密度：长度启发 + 词元多样性
        if length < 10:
            length_score = 0.1
        elif length < 30:
            length_score = 0.4
        else:
            length_score = min(0.5 + length / 600.0, 1.0)
        toks = _tokens(content)
        diversity = (len(set(toks)) / len(toks)) if toks else 0.0
        density = min(0.6 * length_score + 0.4 * diversity, 1.0)

        # 相关性：主题字/词在内容中的覆盖率（无主题时视为通过）。
        # 注意 _tokens 会生成跨词边界的伪 bigram（如"短剧编剧"→"剧编"），
        # 若把主题全部 token 作分母会稀释得分（实测优质定义句仅 0.3），
        # 故拆为"单字命中率"与"bigram/词命中率"两组分别计算取均值。
        topic = topic if topic is not None else (knowledge.topic or "")
        if topic.strip():
            c_toks = set(_tokens(content)) | set(_tokens(knowledge.title or ""))
            t_chars = set(_CJK_RE.findall(topic.lower()))
            t_grams = set(_tokens(topic)) - t_chars
            parts: list[float] = []
            if t_chars:
                parts.append(len(t_chars & c_toks) / len(t_chars))
            if t_grams:
                parts.append(len(t_grams & c_toks) / len(t_grams))
            relevance = sum(parts) / len(parts) if parts else 1.0
        else:
            relevance = 1.0

        # 时效性：检测年份标记
        years = [int(m.group(0).strip().rstrip("年"))
                 for m in _YEAR_RE.finditer(content)]
        now_year = time.localtime().tm_year
        has_ts = bool(years)
        if not years:
            timeliness = 0.8          # 无时间信息：中性
        elif max(years) >= now_year - 1:
            timeliness = 1.0
        elif max(years) >= now_year - 5:
            timeliness = 0.7
        else:
            timeliness = 0.4

        total = 0.4 * density + 0.4 * relevance + 0.2 * timeliness
        passed = density >= DENSITY_THRESHOLD and relevance >= RELEVANCE_THRESHOLD
        return QualityScore(density=round(density, 4),
                            relevance=round(relevance, 4),
                            timeliness=round(timeliness, 4),
                            total=round(total, 4), passed=passed,
                            has_timestamp=has_ts)

    # ── Step 5: SimHash 去重（TASK-045）───────────────────────

    def deduplicate(self, knowledge: Knowledge) -> str:
        """SimHash 去重判定: "skip" / "merge" / "new"。

        >skip 阈值 skip；SIM_MERGE~skip 阈值 merge（保留信息更完整版本，
        目标 id 记入 knowledge.extra["merge_target_id"]）；<SIM_MERGE new。
        §4.3 自适应：skip 阈值随知识总量动态调整（0.85，接近 10 万条时 0.8）。
        """
        fp = simhash64(knowledge.content or "")
        knowledge.simhash = format(fp, "016x")
        skip_threshold = self._dedup_skip_threshold()   # §4.3 自适应
        best_sim = 0.0
        best_id = ""
        best_len = 0
        for row in self._iter_simhashes():
            other = row.get("simhash") or ""
            if not other or other == knowledge.simhash and row["id"] == knowledge.id:
                continue
            sim = simhash_similarity(fp, other)
            if sim > best_sim:
                best_sim = sim
                best_id = row["id"]
                best_len = len(row.get("content") or "")
        knowledge.extra["dedup_max_similarity"] = round(best_sim, 4)
        if best_sim > skip_threshold:
            knowledge.extra["merge_target_id"] = best_id
            return "skip"
        if best_sim >= SIM_MERGE:
            knowledge.extra["merge_target_id"] = best_id
            # 信息完整度：内容更长者胜出
            knowledge.extra["merge_keep_new"] = len(knowledge.content) > best_len
            return "merge"
        return "new"

    def _dedup_skip_threshold(self) -> float:
        """§4.3 自适应：知识总量 ≥ NEAR_FULL_COUNT（9 万）时去重 skip 阈值
        由 0.85 收紧为 0.8（接近 10 万容量上限时加大去重力度）。
        首次切换到收紧模式记一行 info；回落至常规模式后再次切换会重新记录。
        """
        total = self.count()
        near_full = total >= NEAR_FULL_COUNT
        if near_full and not self._near_full:
            log.info("§4.3 自适应：知识总量 %d ≥ %d，去重阈值 %.2f → %.2f",
                     total, NEAR_FULL_COUNT, DEDUP_THRESHOLD_NORMAL,
                     DEDUP_THRESHOLD_NEAR_FULL)
        self._near_full = near_full
        return DEDUP_THRESHOLD_NEAR_FULL if near_full else DEDUP_THRESHOLD_NORMAL

    def _iter_simhashes(self) -> list[dict]:
        """取最近 DEDUP_SCAN_LIMIT 条元数据（id/content/simhash）用于比对。"""
        if self._db is not None:
            try:
                return self._db.query(
                    "SELECT id, content, simhash FROM knowledge_meta "
                    "ORDER BY created_at DESC LIMIT ?", (DEDUP_SCAN_LIMIT,))
            except Exception as exc:  # noqa: BLE001
                log.warning("读取 simhash 失败: %s", exc)
                return []
        return [{"id": r["id"], "content": r.get("content", ""),
                 "simhash": r.get("simhash", "")}
                for r in list(self._mem_meta.values())[:DEDUP_SCAN_LIMIT]]

    # ── Step 6: 向量化入库 ───────────────────────────────────

    def vectorize_and_store(self, knowledge: Knowledge,
                            replace_id: str | None = None) -> str:
        """嵌入 → 向量库存储，元数据落 knowledge_meta。返回知识 id。

        元数据含 source_url/topic/quality_score/type/created_at/simhash/lifecycle。
        容量上限 100000 条，超出时触发最旧清理（每次 100 条）。
        """
        kid = replace_id or knowledge.id
        knowledge.id = kid
        knowledge.lifecycle = self.compute_lifecycle(
            knowledge.created_at, 0, knowledge.quality_score)
        if not knowledge.simhash:
            knowledge.simhash = format(simhash64(knowledge.content), "016x")

        with self._lock:
            self._enforce_capacity()
            metadata = {
                "source_url": knowledge.source_url,
                "topic": knowledge.topic,
                "quality_score": knowledge.quality_score,
                "type": knowledge.type,
                "created_at": knowledge.created_at,
                "simhash": knowledge.simhash,
                "lifecycle": knowledge.lifecycle,
                "title": knowledge.title,
            }
            self._vdb.add([knowledge.content], ids=[kid],
                          metadatas=[metadata])
            self._meta_upsert(kid, knowledge)
            # TASK-051: 同步全文索引（混合检索关键词路）
            try:
                get_fts_store().add(kid, knowledge.content,
                                    knowledge.title, knowledge.topic)
            except Exception:  # noqa: BLE001
                pass
            # TASK-055: 抽取实体关系三元组 → 图谱存储（离线规则路径）
            try:
                triples = extract_triples_rule(knowledge.content,
                                               topic=knowledge.topic,
                                               title=knowledge.title)
                if triples:
                    get_graph_store().add_triples(kid, triples)
            except Exception:  # noqa: BLE001
                pass
        return kid

    def _meta_upsert(self, kid: str, knowledge: Knowledge) -> None:
        lang = detect_lang(knowledge.content)
        row = {"id": kid, "content": knowledge.content, "type": knowledge.type,
               "title": knowledge.title, "topic": knowledge.topic,
               "source_url": knowledge.source_url,
               "quality_score": knowledge.quality_score,
               "simhash": knowledge.simhash, "lifecycle": knowledge.lifecycle,
               "vector_id": kid, "lang": lang,
               "created_at": knowledge.created_at}
        if self._db is not None:
            try:
                self._db.sql(
                    "INSERT INTO knowledge_meta (id, content, type, title, "
                    "topic, source_url, quality_score, simhash, lifecycle, "
                    "vector_id, lang, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET content=excluded.content,"
                    "type=excluded.type, title=excluded.title,"
                    "topic=excluded.topic, source_url=excluded.source_url,"
                    "quality_score=excluded.quality_score,"
                    "simhash=excluded.simhash, lifecycle=excluded.lifecycle,"
                    "vector_id=excluded.vector_id, lang=excluded.lang",
                    (row["id"], row["content"], row["type"], row["title"],
                     row["topic"], row["source_url"], row["quality_score"],
                     row["simhash"], row["lifecycle"], row["vector_id"],
                     row["lang"], row["created_at"]))
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("knowledge_meta 写入失败，降级内存: %s", exc)
        old = self._mem_meta.get(kid, {})
        row["access_count"] = old.get("access_count", 0)
        self._mem_meta[kid] = row

    def _enforce_capacity(self) -> None:
        """容量上限 100000 条：超出时清理最旧 CLEANUP_BATCH 条。"""
        total = self.count()
        if total < MAX_KNOWLEDGE_COUNT:
            return
        victims: list[str] = []
        if self._db is not None:
            try:
                rows = self._db.query(
                    "SELECT id FROM knowledge_meta ORDER BY created_at ASC "
                    "LIMIT ?", (CLEANUP_BATCH,))
                victims = [r["id"] for r in rows]
            except Exception as exc:  # noqa: BLE001
                log.warning("容量清理查询失败: %s", exc)
        else:
            ordered = sorted(self._mem_meta.values(),
                             key=lambda r: r.get("created_at", 0))
            victims = [r["id"] for r in ordered[:CLEANUP_BATCH]]
        for kid in victims:
            try:
                self._vdb.delete([kid])
            except Exception:  # noqa: BLE001
                pass
            try:
                get_fts_store().delete(kid)
            except Exception:  # noqa: BLE001
                pass
            try:
                get_graph_store().delete_by_kid(kid)
            except Exception:  # noqa: BLE001
                pass
            if self._db is not None:
                try:
                    self._db.delete("knowledge_meta", "id=?", (kid,))
                except Exception:  # noqa: BLE001
                    pass
            self._mem_meta.pop(kid, None)
        if victims:
            log.info("知识库容量上限触发，已清理最旧 %d 条", len(victims))

    # ── 完整管线 ─────────────────────────────────────────────

    def process_page(self, page_content: str, topic: str,
                     source_url: str | None = None) -> list[Knowledge]:
        """完整管线: 过滤→分段→提取→评估→去重→入库，返回新入库的知识。"""
        filtered = self.filter_content(page_content)
        segments = self.segment_content(filtered)
        all_knowledge: list[Knowledge] = []
        for seg in segments:
            for k in self.extract_knowledge(seg, topic):
                k.topic = topic
                k.source_url = source_url or ""
                score = self.evaluate_quality(k, topic)
                k.quality_score = score.total
                if not score.passed:
                    continue
                # 入库质检闸（升级批2）：导航残渣/无答案QA/超短拒收
                gate_ok, gate_reason = knowledge_quality_gate.check(
                    k.content or "", k.type or "fact")
                if not gate_ok:
                    log.info("知识入库质检拒收(%s): %s",
                             gate_reason, (k.content or "")[:40])
                    continue
                decision = self.deduplicate(k)
                if decision == "new":
                    self.vectorize_and_store(k)
                    all_knowledge.append(k)
                elif decision == "merge":
                    self._apply_merge(k)
                # skip: 重复，直接丢弃
        return all_knowledge

    def _apply_merge(self, knowledge: Knowledge) -> None:
        """merge 决策执行：保留信息更完整版本。"""
        target = knowledge.extra.get("merge_target_id")
        if not target:
            return
        if knowledge.extra.get("merge_keep_new"):
            # 新版本更完整：替换目标向量与元数据（保留原 id 与创建时间）
            old = self.get_knowledge(target)
            if old:
                knowledge.created_at = old.get("created_at", knowledge.created_at)
            self.vectorize_and_store(knowledge, replace_id=target)
            log.info("知识合并：%s → 替换 %s", knowledge.id, target)
        else:
            # 旧版本更完整：仅累计访问次数
            self._bump_access(target)

    def _bump_access(self, kid: str) -> None:
        if self._db is not None:
            try:
                self._db.sql("UPDATE knowledge_meta SET access_count="
                             "access_count+1 WHERE id=?", (kid,))
                return
            except Exception:  # noqa: BLE001
                pass
        if kid in self._mem_meta:
            self._mem_meta[kid]["access_count"] = \
                self._mem_meta[kid].get("access_count", 0) + 1

    # ── 生命周期 ─────────────────────────────────────────────

    @staticmethod
    def compute_lifecycle(created_at: float, access_count: int = 0,
                          quality_score: float = 0.0) -> str:
        """青年(<7天) / 成熟(<30天) / 沉淀(<90天) / 淘汰(≥90天或老且低质)。"""
        age_days = max(0.0, (time.time() - (created_at or time.time()))) / 86400.0
        if age_days >= 90 or (age_days >= 30 and quality_score
                              and quality_score < DENSITY_THRESHOLD
                              and access_count == 0):
            return LIFECYCLE_OBSOLETE
        if age_days >= 30:
            return LIFECYCLE_DEPOSITED
        if age_days >= 7:
            return LIFECYCLE_MATURE
        return LIFECYCLE_YOUNG

    def refresh_lifecycle(self, kid: str) -> str:
        """按当前时间重算某条知识的生命周期并写回。"""
        row = self.get_knowledge(kid)
        if not row:
            return ""
        stage = self.compute_lifecycle(row.get("created_at", 0),
                                       row.get("access_count", 0),
                                       row.get("quality_score", 0.0))
        if self._db is not None:
            try:
                self._db.update("knowledge_meta", {"lifecycle": stage},
                                "id=?", (kid,))
            except Exception:  # noqa: BLE001
                pass
        if kid in self._mem_meta:
            self._mem_meta[kid]["lifecycle"] = stage
        return stage

    # ── 查询接口（供 API 层使用）──────────────────────────────

    def count(self) -> int:
        """知识总条数。"""
        if self._db is not None:
            try:
                return self._db.count("knowledge_meta")
            except Exception:  # noqa: BLE001
                pass
        return len(self._mem_meta)

    def stats(self) -> dict:
        """条数 / 磁盘大小 / 主题分布 / 后端状态。"""
        topics: dict[str, int] = {}
        types: dict[str, int] = {}
        if self._db is not None:
            try:
                for r in self._db.query(
                        "SELECT topic, COUNT(*) AS c FROM knowledge_meta "
                        "GROUP BY topic"):
                    topics[r["topic"] or "未分类"] = r["c"]
                for r in self._db.query(
                        "SELECT type, COUNT(*) AS c FROM knowledge_meta "
                        "GROUP BY type"):
                    types[r["type"] or "unknown"] = r["c"]
            except Exception as exc:  # noqa: BLE001
                log.warning("统计查询失败: %s", exc)
        else:
            for r in self._mem_meta.values():
                topics[r.get("topic") or "未分类"] = \
                    topics.get(r.get("topic") or "未分类", 0) + 1
                types[r.get("type") or "unknown"] = \
                    types.get(r.get("type") or "unknown", 0) + 1
        disk = 0
        try:
            disk = self._vdb.disk_usage_bytes()
        except Exception:  # noqa: BLE001
            pass
        return {"total": self.count(),
                "capacity": MAX_KNOWLEDGE_COUNT,
                "disk_bytes": disk,
                "topics": topics,
                "types": types,
                "vector_backend": getattr(self._vdb, "backend", "unknown"),
                "embed_backend": getattr(self._vdb, "embed_backend", "unknown"),
                "db_available": self._db is not None}

    def list_knowledge(self, page: int = 1, page_size: int = 20,
                       topic: str = "", keyword: str = "",
                       type: str = "", min_score: float = 0.0) -> dict:
        """分页 + topic 过滤 + keyword 搜索（匹配 content/title）。

        LEARN-040：type 精确过滤（concept/fact/procedure/...）、
        min_score 质量分下限过滤。"""
        page = max(1, int(page or 1))
        page_size = min(max(1, int(page_size or 20)), 200)
        where: list[str] = []
        params: list[Any] = []
        if topic:
            where.append("topic = ?")
            params.append(topic)
        if keyword:
            where.append("(content LIKE ? OR title LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like])
        if type:
            where.append("type = ?")
            params.append(type)
        if min_score and min_score > 0:
            where.append("quality_score >= ?")
            params.append(float(min_score))
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        if self._db is not None:
            try:
                total = self._db.count(
                    "knowledge_meta",
                    " AND ".join(where) if where else "", tuple(params))
                rows = self._db.query(
                    "SELECT id, content, type, title, topic, source_url,"
                    " quality_score, simhash, lifecycle, vector_id,"
                    f" access_count, lang, created_at FROM knowledge_meta{where_sql} "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    tuple(params) + (page_size, (page - 1) * page_size))
                return {"items": rows, "total": total,
                        "page": page, "page_size": page_size}
            except Exception as exc:  # noqa: BLE001
                log.warning("列表查询失败，降级内存: %s", exc)
        items = list(self._mem_meta.values())
        if topic:
            items = [r for r in items if r.get("topic") == topic]
        if keyword:
            items = [r for r in items
                     if keyword in r.get("content", "")
                     or keyword in r.get("title", "")]
        if type:
            items = [r for r in items if r.get("type") == type]
        if min_score and min_score > 0:
            items = [r for r in items
                     if float(r.get("quality_score", 0) or 0) >= min_score]
        items.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        total = len(items)
        start = (page - 1) * page_size
        return {"items": items[start:start + page_size], "total": total,
                "page": page, "page_size": page_size}

    def update_knowledge(self, kid: str, *, content: str | None = None,
                         topic: str | None = None,
                         type: str | None = None) -> bool:
        """LEARN-041：编辑知识条目（content/topic/type），同步刷新 lang
        与 FTS 索引；content 变更时重算 simhash。返回是否找到记录。"""
        fields: dict[str, Any] = {}
        if content is not None:
            fields["content"] = content
            fields["simhash"] = format(simhash64(content), "016x")
            fields["lang"] = detect_lang(content)
        if topic is not None:
            fields["topic"] = topic
        if type is not None:
            fields["type"] = type
        if not fields:
            return True  # 无变更视为成功（幂等）
        if self._db is not None:
            try:
                affected = self._db.update("knowledge_meta", fields,
                                           "id=?", (kid,))
                if affected > 0:
                    try:  # FTS 同步（content/topic 变化影响检索）
                        row = self.get_knowledge(kid) or {}
                        get_fts_store().add(
                            kid, row.get("content", ""),
                            row.get("title", ""), row.get("topic", ""))
                    except Exception:  # noqa: BLE001
                        pass
                    return True
                return False
            except Exception as exc:  # noqa: BLE001
                log.warning("知识更新落库失败: %s", exc)
        if kid in self._mem_meta:
            self._mem_meta[kid].update(fields)
            return True
        return False

    def get_knowledge(self, kid: str) -> dict | None:
        """按 id 取单条知识元数据。"""
        if self._db is not None:
            try:
                row = self._db.query_one(
                    "SELECT id, content, type, title, topic, source_url,"
                    " quality_score, simhash, lifecycle, vector_id,"
                    " access_count, created_at FROM knowledge_meta WHERE id=?",
                    (kid,))
                if row:
                    return row
            except Exception as exc:  # noqa: BLE001
                log.warning("查询知识失败: %s", exc)
        return self._mem_meta.get(kid)

    def adjust_quality_score(self, kid: str, delta: float,
                             floor: float = -1.0,
                             ceiling: float = 1.0) -> float | None:
        """反馈闭环（升级批5）：按用户评价调整知识质量分。

        点踩 → 负 delta（默认 -0.8，常规条目一次即压到注入地板之下）；
        点赞 → 正 delta 小幅回升（可复活被误踩条目）。
        注入检索跳过质量分为负的条目。条目不存在返回 None。
        """
        if self._db is None:
            return None
        try:
            row = self._db.query_one(
                "SELECT quality_score FROM knowledge_meta WHERE id=?", (kid,))
        except Exception as exc:  # noqa: BLE001
            log.warning("知识质量分查询失败: %s", exc)
            return None
        if row is None:
            return None
        try:
            current = float(row.get("quality_score") or 0.0)
        except (TypeError, ValueError):
            current = 0.0
        new_score = max(floor, min(ceiling, current + delta))
        try:
            self._db.update("knowledge_meta",
                            {"quality_score": new_score}, "id=?", (kid,))
        except Exception as exc:  # noqa: BLE001
            log.warning("知识质量分更新失败: %s", exc)
            return None
        return new_score

    def delete_knowledge(self, kid: str) -> bool:
        """级联删除：向量 + 元数据 + 全文索引 + 图谱边。返回是否删除成功。"""
        existed = self.get_knowledge(kid) is not None
        try:
            self._vdb.delete([kid])
        except Exception as exc:  # noqa: BLE001
            log.warning("向量删除失败: %s", exc)
        try:
            get_fts_store().delete(kid)
        except Exception:  # noqa: BLE001
            pass
        try:
            get_graph_store().delete_by_kid(kid)
        except Exception:  # noqa: BLE001
            pass
        if self._db is not None:
            try:
                affected = self._db.delete("knowledge_meta", "id=?", (kid,))
                existed = existed or affected > 0
            except Exception as exc:  # noqa: BLE001
                log.warning("元数据删除失败: %s", exc)
        existed = existed or (kid in self._mem_meta)
        self._mem_meta.pop(kid, None)
        return existed

    # ── 知识图谱（TASK-055）───────────────────────────────────

    def knowledge_graph(self, kid: str = "", max_nodes: int = 200) -> dict:
        """查询知识图谱节点/边（kid 为空时返回全局 Top-N 图谱）。"""
        graph = get_graph_store().graph(kid=kid or None, max_nodes=max_nodes)
        return graph

    # ── LoRA 训练数据格式（TASK-033 Step6 附属要求）────────────

    def to_training_sample(self, knowledge: Knowledge) -> dict:
        """将知识条目转为 LoRA 训练样本格式。"""
        scope = f"「{knowledge.topic}」相关的" if knowledge.topic else ""
        return {"instruction": f"请解释{scope}以下知识点",
                "input": knowledge.title or knowledge.type,
                "output": knowledge.content}


# ═══════════════════════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════════════════════

_ks_instance: KnowledgeProcessingService | None = None
_ks_lock = threading.Lock()


def get_knowledge_service() -> KnowledgeProcessingService:
    """获取知识处理管线服务单例（线程安全双重检查）。"""
    global _ks_instance
    if _ks_instance is None:
        with _ks_lock:
            if _ks_instance is None:
                _ks_instance = KnowledgeProcessingService()
    return _ks_instance
