"""知识处理管线单元测试（对应测试文档 TC-U-001 ~ TC-U-008）。

直接调用 KnowledgeProcessingService 的真实实现（过滤/分段/提取/质量/SimHash）。
为保证用例确定性与隔离性，服务实例使用内存元数据存储 + 全新内存向量库，
测试对象（算法与管线逻辑）为生产代码本身，无任何 mock 行为替换。
"""
from __future__ import annotations

import time

import pytest

from backend.data.vector_db import _InMemoryVectorStore
from backend.services.knowledge_service import (
    CleanContent,
    Knowledge,
    KnowledgeProcessingService,
    Segment,
    simhash64,
    simhash_similarity,
)


@pytest.fixture()
def service() -> KnowledgeProcessingService:
    """隔离的服务实例：内存向量库 + 内存元数据（真实算法）。"""
    svc = KnowledgeProcessingService(vector_db=_InMemoryVectorStore())
    svc._db = None           # 强制内存元数据通道（实现自带降级路径）
    svc._mem_meta = {}
    return svc


# ── TC-U-001：内容过滤 - 去除广告 ─────────────────────────────

TC001_HTML = """
<html>
  <nav>导航栏内容</nav>
  <div class="ad-banner">广告内容</div>
  <article>
    <h1>正文标题</h1>
    <p>正文第一段内容，包含有价值的知识。</p>
    <p>正文第二段内容。</p>
  </article>
  <div class="cookie-notice">我们使用Cookie</div>
  <footer>页脚内容</footer>
  <script>var x = 1;</script>
</html>
"""


def test_tc_u_001_filter_content(service):
    t0 = time.perf_counter()
    out = service.filter_content(TC001_HTML)
    dt = (time.perf_counter() - t0) * 1000
    assert "正文标题" in out.text
    assert "正文第一段内容" in out.text
    assert "正文第二段内容" in out.text
    assert "导航栏内容" not in out.text
    assert "广告内容" not in out.text
    assert "我们使用Cookie" not in out.text
    assert "页脚内容" not in out.text
    assert "var x = 1" not in out.text
    assert dt < 50, f"处理时间 {dt:.1f}ms 超过 50ms"


# ── TC-U-002：内容过滤 - 空页面 ───────────────────────────────

def test_tc_u_002_filter_empty(service):
    t0 = time.perf_counter()
    out = service.filter_content("")
    dt = (time.perf_counter() - t0) * 1000
    assert isinstance(out, CleanContent)
    assert out.text == "" and out.blocks == []
    assert dt < 10


# ── TC-U-003：内容分段 - 按标题层级 ───────────────────────────

def test_tc_u_003_segment_by_heading(service):
    content = CleanContent(title="", text="", blocks=[
        {"kind": "heading", "level": 1, "text": "第一章"},
        {"kind": "para", "level": 0, "text": "内容A" + "字" * 200},
        {"kind": "heading", "level": 2, "text": "第一节"},
        {"kind": "para", "level": 0, "text": "内容B" + "字" * 300},
        {"kind": "heading", "level": 2, "text": "第二节"},
        {"kind": "para", "level": 0, "text": "内容C" + "字" * 800},
        {"kind": "heading", "level": 1, "text": "第二章"},
        {"kind": "para", "level": 0, "text": "内容D" + "字" * 100},
    ])
    segs = service.segment_content(content)
    headings = [s.heading for s in segs]
    assert headings[0] == "第一章"
    assert "第一节" in headings and "第二节" in headings and "第二章" in headings
    assert any("内容A" in s.text for s in segs)
    assert any("内容B" in s.text for s in segs)
    assert any("内容C" in s.text for s in segs)
    assert any("内容D" in s.text for s in segs)
    # 800 字段落被切分（~500字/段 → ≥2 段）
    c_segs = [s for s in segs if "内容C" in s.text or
              (s.heading == "第二节" and s.text)]
    long_parts = [s for s in c_segs if len(s.text) > 0]
    assert len([s for s in segs if s.heading == "第二节"]) >= 2
    for s in long_parts:
        assert len(s.text) <= 500 + 10  # 切分阈值容差
    # 每段保留标题信息
    assert all(s.heading for s in segs)


# ── TC-U-004/005/006：SimHash 去重 ────────────────────────────

OLD_K = "三幕式结构是编剧的基本框架，包含建置、对抗、解决三个阶段。"


def _seed(service, content=OLD_K):
    k = Knowledge(content=content, topic="编剧")
    service.vectorize_and_store(k)
    return k


def test_tc_u_004_simhash_identical_skip(service):
    _seed(service)
    k2 = Knowledge(content=OLD_K, topic="编剧")
    assert service.deduplicate(k2) == "skip"


def test_tc_u_005_simhash_similar_merge(service):
    _seed(service)
    k2 = Knowledge(
        content="三幕式结构是编剧常用的框架，包含建置、对抗、解决三个主要阶段。",
        topic="编剧")
    fp_old = simhash64(OLD_K)
    sim = simhash_similarity(fp_old, simhash64(k2.content))
    decision = service.deduplicate(k2)
    assert 0.7 <= sim <= 1.0
    assert decision in ("merge", "skip")
    if decision == "merge":
        assert "merge_target_id" in k2.extra


def test_tc_u_006_simhash_new(service):
    _seed(service)
    k2 = Knowledge(content="Python的异步编程使用async/await关键字实现协程。",
                   topic="编程")
    sim = simhash_similarity(simhash64(OLD_K), simhash64(k2.content))
    assert service.deduplicate(k2) == "new"
    assert sim < 0.9


# ── TC-U-007：知识提取 - 规则模式（LLM 提取见实机集成测试）─────

def test_tc_u_007_extract_knowledge_rules(service):
    text = ("短剧的前3秒被称为'黄金钩子'。常见的钩子写法包括：悬念式：直接展示冲突结果；"
            "反差式：打破观众预期；共鸣式：触及普遍情感；视觉冲击：强画面开场；提问式：抛出核心问题。")
    seg = Segment(heading="钩子写法", level=2, text=text, index=0)
    t0 = time.perf_counter()
    items = service.extract_knowledge(seg, topic="短剧编剧")
    dt = time.perf_counter() - t0
    assert len(items) >= 2
    assert any(i.type == "concept" for i in items)      # 定义句 → 概念
    assert all(i.quality_score == 0.0 for i in items)   # 评分由后续步骤填充
    assert dt < 5


# ── TC-U-008：质量评估 - 低质内容过滤 ─────────────────────────

def test_tc_u_008_quality_filter(service):
    k = Knowledge(content="今天天气不错，大家可以出去玩。", topic="短剧编剧技巧")
    score = service.evaluate_quality(k, "短剧编剧技巧")
    assert score.relevance < 0.5
    assert score.passed is False
