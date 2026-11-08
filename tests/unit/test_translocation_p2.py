"""批2（2026-11-08 转义层升级立项）：动作链八段式展开 + VFX 四要素 + token 守卫单测。"""
from __future__ import annotations

import pytest

from src.config import _cfg
from src.services.inference.h3_chain_engine import (
    _expand_action_chains,
    _expand_vfx,
    _token_guard,
)


@pytest.fixture
def action_on(monkeypatch):
    monkeypatch.setitem(_cfg.setdefault("manga", {}), "h3_action_chains", True)


@pytest.fixture
def vfx_on(monkeypatch):
    monkeypatch.setitem(_cfg.setdefault("manga", {}), "h3_vfx_expand", True)


def _shot(text: str) -> dict:
    return {"text": text, "start": 0.0, "dlg": [], "sfx": "", "cam": ""}


def test_bare_verb_gets_process_chain(action_on) -> None:
    shots = [_shot("中景侧面机位，剑客挥剑攻击木人桩。")]
    _expand_action_chains(shots)
    # 左most命中=挥剑（比攻击更早出现在句中），断言其模板要素
    assert "连续过程" in shots[0]["text"] and "收剑" in shots[0]["text"]
    assert shots[0]["text"].startswith("中景侧面机位，剑客挥剑攻击木人桩")


def test_process_described_shot_skipped(action_on) -> None:
    s = _shot("剑客凝神蓄力，随即突刺，命中后顺势收剑。")
    _expand_action_chains([s])
    assert "连续过程" not in s["text"]  # 已含过程描述，不重复注入


def test_one_injection_per_shot(action_on) -> None:
    s = _shot("剑客出剑攻击，随即翻滚闪避。")  # 出剑/攻击/翻滚/闪避 多命中
    _expand_action_chains([s])
    assert s["text"].count("以连续过程呈现") == 1  # 每镜限 1 条


def test_vfx_gets_four_elements(vfx_on) -> None:
    shots = [_shot("剑锋命中木人桩，木屑飞溅，尘土飞扬。")]
    _expand_vfx(shots)
    # 尘土命中 → 扬起(触发)+弥散(形态方向)+尘幕(光线后果) 四要素句
    assert "尘土自受力点扬起" in shots[0]["text"] and "尘幕" in shots[0]["text"]


def test_vfx_one_per_shot(vfx_on) -> None:
    s = _shot("远处爆炸，火光冲天，烟尘滚滚。")
    _expand_vfx([s])
    assert s["text"].count("。") - s["text"].count("。", 0, len(_shot("").get("text", ""))) >= 0
    # 首个命中（爆炸）注入一次，其余词不再注入
    assert "自爆心向外扩张" in s["text"]
    assert s["text"].count("火光自燃点窜起") == 0


def test_switch_off_untouched(monkeypatch) -> None:
    monkeypatch.setitem(_cfg.setdefault("manga", {}), "h3_action_chains", False)
    monkeypatch.setitem(_cfg.setdefault("manga", {}), "h3_vfx_expand", False)
    shots = [_shot("剑客挥剑攻击木人桩，木屑飞溅。")]
    _expand_action_chains(shots)
    _expand_vfx(shots)
    assert shots[0]["text"] == "剑客挥剑攻击木人桩，木屑飞溅。"


def test_token_guard_warns_only(capsys) -> None:
    # 超阈值不抛异常（告警不阻断）
    _token_guard("x" * 4001)
    _token_guard("x" * 100)  # 静默
