"""V37 关键帧 seed 固定 + 一致性评分纯逻辑测试（2026-08-27 v36 事故方案D）。

B9（2026-09-13）：AST 沙箱改真 import——真模块依赖（re/json/random）
天然就位；_resolve_base_seed 的 db 由参数传入，_FakeDB 桩保持原样。

覆盖：
  - _parse_consistency_score：VLM 输出评分宽容解析（格式漂移/钳制/无匹配）
  - _resolve_base_seed：显式 > 沿用最近版本 > 随机；force_new_seed 跳过沿用
"""
from __future__ import annotations

import src.api.manga.keyframe as _kf_mod

KEYFRAME_PY = None  # 历史 Path 锚点退役（真 import 不再读源文件）


def _load_funcs(*names: str) -> dict:
    """真 import 取目标函数（形态与原沙箱函数表一致，调用点零改动）。"""
    ns: dict = {}
    for name in names:
        assert hasattr(_kf_mod, name), f"目标函数缺失: {name}"
        ns[name] = getattr(_kf_mod, name)
    return ns


class _FakeDB:
    """query_one 桩：按预设响应返回 keyframes 最近版本行。"""

    def __init__(self, prev_row: dict | None):
        self._prev = prev_row
        self.queries: list[str] = []

    def query_one(self, sql: str, params=()):
        self.queries.append(sql)
        return self._prev


# ── _parse_consistency_score ──────────────────────────────────────

def test_parse_score_standard():
    f = _load_funcs("_parse_consistency_score")["_parse_consistency_score"]
    assert f("评分：85\n理由：发型一致") == 85
    assert f("评分: 72\n理由：脸型略有漂移") == 72
    assert f("评分：100") == 100
    assert f("评分：0") == 0


def test_parse_score_fallback_and_clamp():
    f = _load_funcs("_parse_consistency_score")["_parse_consistency_score"]
    assert f("综合给 88 分") == 88           # 回退「NN 分」格式
    assert f("评分：150") == 100             # 钳制上限
    assert f("评分：999") == 100
    assert f("完全不一致，无法评分") is None  # 无数字 → None（不计入判定）
    assert f("") is None


# ── _resolve_base_seed ────────────────────────────────────────────

def test_resolve_seed_explicit_wins():
    f = _load_funcs("_resolve_base_seed")["_resolve_base_seed"]
    db = _FakeDB({"version": 3, "shot_seeds": "[111,112]"})
    assert f(db, "row1", 424242, False) == (424242, "explicit")
    # 显式 seed 即使 force_new_seed 也优先（显式语义最强）
    assert f(db, "row1", 7, True) == (7, "explicit")


def test_resolve_seed_reuse_stored():
    f = _load_funcs("_resolve_base_seed")["_resolve_base_seed"]
    db = _FakeDB({"version": 5, "shot_seeds": "[123456789, 123456790]"})
    seed, src = f(db, "row1", None, False)
    assert seed == 123456789 and src == "reused:v5"


def test_resolve_seed_force_new_skips_stored():
    f = _load_funcs("_resolve_base_seed")["_resolve_base_seed"]
    db = _FakeDB({"version": 5, "shot_seeds": "[123456789]"})
    seed, src = f(db, "row1", None, True)
    assert src == "random" and 0 <= seed <= 2**31 - 1
    assert seed != 123456789


def test_resolve_seed_random_when_no_history():
    f = _load_funcs("_resolve_base_seed")["_resolve_base_seed"]
    for prev in (None,
                 {"version": 1, "shot_seeds": "[]"},
                 {"version": 1, "shot_seeds": "not-json"},
                 {"version": 1, "shot_seeds": ""}):
        seed, src = f(_FakeDB(prev), "row1", None, False)
        assert src == "random" and 0 <= seed <= 2**31 - 1, prev


def test_resolve_seed_explicit_zero_is_valid():
    f = _load_funcs("_resolve_base_seed")["_resolve_base_seed"]
    assert f(_FakeDB(None), "row1", 0, False) == (0, "explicit")
# 本项目仅供学习使用，商业授权请+Q 3559331368
