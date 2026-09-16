"""describe_refine 二遍精修单测（refine-prompt 接入 · 2026-08-30）。

不依赖真实引擎/数据库：engine 用桩（chat 按脚本回放），db 用内存桩。
覆盖：开关默认关零行为、评分解析容忍围栏、低分触发重写、
重写稿结构非法放行原稿、评审异常放行。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import asyncio
import json

from src.api.manga.describe_refine import (
    _parse_score_json,
    read_refine_config,
    refine_description,
)


class _StubEngine:
    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def chat(self, messages, temperature=0.7, max_new_tokens=512):
        self.calls.append({"prompt": messages[-1]["content"],
                           "temperature": temperature})
        return self._replies.pop(0)


class _StubDB:
    def __init__(self, value=None):
        self.value = value

    def query_one(self, sql, params=()):
        if "manga.describe_refine" in sql or (params and params[0] == "manga.describe_refine"):
            return {"value": self.value} if self.value is not None else None
        return None


def _finalize_passthrough(body: str) -> str:
    return (body or "").strip()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if hasattr(asyncio, "get_event_loop") else asyncio.run(coro)


DESC = "A. 画风：网漫风\n氛围：雨夜街头\nB. 高密度世界观构建：主角黑发红衣\nC. [0.1s-2.5s] 画面：街头奔跑"


def test_read_refine_config_default_off():
    assert read_refine_config(None) == {"enabled": False, "threshold": 75}
    assert read_refine_config(_StubDB()) == {"enabled": False, "threshold": 75}


def test_read_refine_config_enabled():
    db = _StubDB(json.dumps({"enabled": True, "threshold": 80}))
    assert read_refine_config(db) == {"enabled": True, "threshold": 80}


def test_read_refine_config_bad_json_falls_back_off():
    db = _StubDB("{broken")
    assert read_refine_config(db)["enabled"] is False


def test_parse_score_json_tolerates_fence_and_noise():
    raw = '说明文字\n```json\n{"score": 62, "issues": ["出现 3D 渲染词"]}\n```\n尾注'
    assert _parse_score_json(raw) == (62, ["出现 3D 渲染词"])
    assert _parse_score_json("") is None
    assert _parse_score_json("no json at all") is None


def test_disabled_returns_none_without_engine_call():
    engine = _StubEngine(['{"score": 10, "issues": []}'])
    out = asyncio.run(refine_description(
        engine, DESC, [], _StubDB(), _finalize_passthrough))
    assert out is None
    assert engine.calls == []  # 开关关 → 零调用零行为变化


def test_high_score_passes_through_untriggered():
    db = _StubDB(json.dumps({"enabled": True}))
    engine = _StubEngine(['{"score": 88, "issues": []}'])
    out = asyncio.run(refine_description(
        engine, DESC, [], db, _finalize_passthrough))
    assert out is not None and out["triggered"] is False
    assert out["score"] == 88
    assert out["description"] == DESC
    assert len(engine.calls) == 1  # 合格稿不再跑重写/复评


def test_low_score_triggers_rewrite_and_rescore():
    db = _StubDB(json.dumps({"enabled": True, "threshold": 75}))
    rewritten = "氛围：雨夜\nB. 高密度世界观构建：主角黑发红衣\nC. [0.1s-2.5s] 画面：街头奔跑"
    engine = _StubEngine([
        '{"score": 55, "issues": ["C 段出现白色行李箱与资产黑色硬壳行李箱冲突"]}',
        rewritten,
        '{"score": 86, "issues": []}',
    ])
    out = asyncio.run(refine_description(
        engine, DESC, [{"kind": "prop", "name": "黑色硬壳行李箱"}],
        db, _finalize_passthrough))
    assert out is not None and out["triggered"] is True
    assert out["score"] == 55 and out["rescore"] == 86
    assert out["description"] == rewritten
    assert engine.calls[0]["temperature"] == 0.0   # 评审稳定
    assert engine.calls[1]["temperature"] == 0.4   # 重写收敛
    # 重写提示词带资产名与问题清单
    assert "黑色硬壳行李箱" in engine.calls[1]["prompt"]
    assert "0.75~1.5" in engine.calls[1]["prompt"]


def test_structurally_invalid_rewrite_falls_back_original():
    db = _StubDB(json.dumps({"enabled": True}))
    engine = _StubEngine([
        '{"score": 40, "issues": ["画风违规"]}',
        "",  # 重写返回空 → finalize 后为空 → 放行原稿
    ])
    out = asyncio.run(refine_description(
        engine, DESC, [], db, _finalize_passthrough))
    assert out is not None and out["triggered"] is False
    assert out["description"] == DESC  # 不静默吞，原稿放行


def test_scorer_exception_releases_original():
    db = _StubDB(json.dumps({"enabled": True}))

    class _BoomEngine:
        def chat(self, *a, **k):
            raise RuntimeError("vLLM down")

    out = asyncio.run(refine_description(
        _BoomEngine(), DESC, [], db, _finalize_passthrough))
    assert out is None  # 异常 → None → 外层零行为变化


def test_rescore_failure_does_not_block():
    db = _StubDB(json.dumps({"enabled": True}))
    rewritten = "氛围：雨夜\nB. 高密度世界观构建：主角\nC. [0.1s-2.5s] 画面：奔跑"

    class _HalfBoom(_StubEngine):
        def __init__(self):
            super().__init__([
                '{"score": 50, "issues": ["x"]}', rewritten])
            self.n = 0

        def chat(self, *a, **k):
            self.n += 1
            if self.n >= 3:
                raise RuntimeError("复评挂了")
            return super().chat(*a, **k)

    out = asyncio.run(refine_description(
        _HalfBoom(), DESC, [], db, _finalize_passthrough))
    assert out is not None and out["triggered"] is True
    assert out["rescore"] is None  # 复评失败仅记日志
    assert out["description"] == rewritten
