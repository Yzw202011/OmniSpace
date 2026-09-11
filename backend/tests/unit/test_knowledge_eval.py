"""检索质量评测集回归（知识学习升级方案 批4，2026-09-11）。

金标集 knowledge_eval_golden.json：20 条 query 由真实库行为推导并逐条
人工目验（14 正例=命中条目须含标记词；6 负例=库外/离题 query 零命中）。
批 3 的注入阈值（MIN_VECTOR_SCORE）等改动跑本集防倒退。

真实库缺失环境自动跳过（同哨兵纪律）。语料有意变更时同步更新金标集。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.config import DB_PATH

_GOLDEN = Path(__file__).with_name("knowledge_eval_golden.json")

pytestmark = [
    pytest.mark.skipif(
        not Path(str(DB_PATH)).is_file(),
        reason="真实知识库不存在（非实机环境）"),
]


def _cases() -> list[dict]:
    return json.loads(_GOLDEN.read_text("utf-8"))["cases"]


def test_golden_set_shape() -> None:
    cases = _cases()
    assert len(cases) == 20
    assert sum(1 for c in cases if c["expect"] is None) == 6
    assert all(c.get("query") for c in cases)


def test_retrieval_golden_regression() -> None:
    from backend.services.injection_service import get_injection_service
    inj = get_injection_service()
    failures: list[str] = []
    for case in _cases():
        q, expect = case["query"], case["expect"]
        markers = [expect] if isinstance(expect, str) else list(expect or [])
        results = inj.retrieve(q, top_k=5)
        if not markers:
            if results:
                failures.append(
                    f"[负例应零命中] {q} → {len(results)} 条: "
                    f"{str(results[0].get('content'))[:40]}")
        else:
            # any-of 多标记：top-5 有小幅排名抖动，任一命中即达标
            if not any(m in str(r.get("content") or "")
                       for m in markers for r in results):
                first = str(results[0].get("content"))[:40] if results else "无"
                failures.append(f"[正例未命中标记{markers}] {q} → 首条: {first}")
    assert failures == [], f"评测集 {len(failures)} 例不达标:\n" + "\n".join(failures)


def test_creative_intent_downweight() -> None:
    """批3/D3 软规则：创作意图 → 类型排序（方法论优先）+ 条数收敛到 3。"""
    from backend.services.injection_service import (
        _CREATIVE_INTENT_RE,
        _CREATIVE_TYPE_ORDER,
        get_injection_service,
    )
    assert _CREATIVE_INTENT_RE.search("帮我写一部短剧剧本")
    inj = get_injection_service()
    _injected, refs = inj.enhance_chat("帮我写一部短剧剧本")
    if not refs:
        pytest.skip("当前语料对该创作 query 无命中（语料变更时复核金标集）")
    assert len(refs) <= 3
    orders = [_CREATIVE_TYPE_ORDER.get(str(r.get("type")), 3) for r in refs]
    assert orders == sorted(orders), f"类型排序未按方法论优先: {orders}"
