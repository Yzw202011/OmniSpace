"""知识规则提取器普通陈述句兜底回归（P2-1 2026-09-02）。

病根：extract_triples_rule 只认 9 个句式模式（X是Y/X包括Y/…），
「高温会加速化学反应」类无触发词的普通陈述句产出零三元组——事实
从图谱消失、检索不可达。修复 = 陈述兜底边（主语片段, 陈述, 全句），
仅中文句；主语经停用字/截断词/虚词三重过滤。

直接 import（services 层无重依赖，既有测试同款手法）。
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[2].parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2].parent / "pydeps"))

from backend.services.knowledge_service import (  # noqa: E402
    _subject_head,
    extract_triples_rule,
)


def test_plain_statement_gets_fallback_triple() -> None:
    t = extract_triples_rule("高温会加速化学反应。", topic="化学", title="常识")
    assert ("高温", "陈述", "高温会加速化学反应") in t, "普通陈述句必须入图谱"


def test_subject_head_cuts_adverbs_and_verbs() -> None:
    assert _subject_head("系统每天凌晨自动备份用户数据。") == "系统"
    assert _subject_head("光合作用释放氧气并吸收二氧化碳。") == "光合作用"
    assert _subject_head("高温会加速化学反应。") == "高温"


def test_fallback_sentence_survives_retrieval_shape() -> None:
    # 三个不同主语的陈述 + 一个句式句：句式句走原模式，陈述句各有兜底
    text = ("高温会加速化学反应。系统每天凌晨自动备份数据。"
            "光合作用释放氧气。Transformer 是一种神经网络架构。")
    t = extract_triples_rule(text, topic="t", title="综合")
    stmts = [(e1, e2) for e1, rel, e2 in t if rel == "陈述"]
    assert any(e1 == "高温" for e1, _ in stmts)
    assert any(e1 == "系统" for e1, _ in stmts)
    assert any(e1 == "光合作用" for e1, _ in stmts)
    # 句式句仍走原「定义」模式不被兜底劫持
    assert any(rel == "定义" and e1 == "Transformer" for e1, rel, e2 in t)


def test_english_not_fallback() -> None:
    t = extract_triples_rule(
        "Kubernetes orchestrates containers in production clusters.",
        topic="t", title="k8s")
    assert all(rel != "陈述" for _, rel, _ in t), "英文陈述不兜底（LLM 主链负责）"


def test_head_equals_sentence_rejected() -> None:
    # 主语截断后等于整句（无有效主语）→ 不产自指兜底边
    t = extract_triples_rule("十四个字以内全为名词短语", topic="t", title="x")
    assert all(not (e1 == e2) for e1, rel, e2 in t), "不得产生自指边"
