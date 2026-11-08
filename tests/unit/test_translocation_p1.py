"""批1（2026-11-08 转义层升级立项）：远景行走连续性注入 + 空洞形容词 lint 单测。

证据依据：Singularity 规范书 §15.1/§16 对照（docs/plan-translocation-
upgrade-2026-11-08.md 批1）。开关 manga.h3_distant_walk 默认关（A/B
胜出后翻默认）；lint 纯告警零改写（manga.h3_prompt_lint 默认 True）。"""
from __future__ import annotations

import pytest

from src.config import _cfg
from src.services.inference.h3_chain_engine import (
    _inject_distant_walk,
    _lint_generic_adjectives,
)


@pytest.fixture
def walk_on(monkeypatch):
    monkeypatch.setitem(_cfg.setdefault("manga", {}), "h3_distant_walk", True)


def _shot(text: str) -> dict:
    return {"text": text, "start": 0.0, "dlg": [], "sfx": "", "cam": ""}


def test_far_walking_shot_gets_continuity_sentence(walk_on) -> None:
    shots = [_shot("远景：两人在地平线上缓缓走过荒原。")]
    _inject_distant_walk(shots)
    assert "持续行走" in shots[0]["text"]
    assert "步伐始终保持连续" in shots[0]["text"]
    # 原文保留（注入是追加不是改写）
    assert shots[0]["text"].startswith("远景：两人在地平线上缓缓走过荒原")


def test_closeup_or_nonwalk_shot_untouched(walk_on) -> None:
    near = _shot("近景：她转过身，面对镜头微笑。")
    still = _shot("远景：雪山静立，空无一人。")
    _inject_distant_walk([near, still])
    assert near["text"] == "近景：她转过身，面对镜头微笑。"
    assert still["text"] == "远景：雪山静立，空无一人。"


def test_already_continuous_not_reinjected(walk_on) -> None:
    s = _shot("远景：两人一直走向城镇尽头，步伐始终连续。")
    _inject_distant_walk([s])
    assert "持续行走" not in s["text"]  # 未注入第二份（防权重叠加）


def test_switch_off_no_injection(monkeypatch) -> None:
    monkeypatch.setitem(_cfg.setdefault("manga", {}), "h3_distant_walk", False)
    shots = [_shot("远景：两人在地平线上走过。")]
    _inject_distant_walk(shots)
    assert shots[0]["text"] == "远景：两人在地平线上走过。"


def test_lint_hits_generic_adjectives() -> None:
    joined = " ".join(_lint_generic_adjectives(
        "电影感的画面，dynamic camera 缓慢推进，高质量细节"))
    assert "电影感" in joined
    assert "dynamic camera" in joined
    assert "高质量" in joined


def test_lint_silent_on_concrete_text() -> None:
    assert _lint_generic_adjectives(
        "镜头以匀速侧向跟随人物，保持人物在画面左三分之一处。") == []
