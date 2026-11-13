"""批3（2026-11-08 转义层升级立项）：运镜五要素展开单测（规范 §9 + R18 联动）。"""
from __future__ import annotations

import pytest

from src.config import _cfg
from src.services.inference.h3_chain_engine import (
    _expand_camera,
    _lint_contract_rules,
)


@pytest.fixture
def cam_on(monkeypatch):
    monkeypatch.setitem(_cfg.setdefault("manga", {}), "h3_camera_expand", True)


def _shot(cam: str) -> dict:
    return {"text": "画面内容。", "start": 0.0, "dlg": [], "sfx": "", "cam": cam}


def test_orbit_cam_expanded_to_five_elements(cam_on) -> None:
    s = _shot("镜头缓慢环绕人物移动")
    _expand_camera([s])
    assert "弧形环绕" in s["cam"] and "恒定距离" in s["cam"]
    assert "缓慢匀速" in s["cam"]
    assert "原设定：镜头缓慢环绕人物移动" in s["cam"]  # 原设定回引保留
    # 11-13 链路检验：展开器不再自带「严格执行」尾注——由时间轴组装器
    # 统一追加（双尾注=token 冗余缺陷根修）
    assert "严格执行" not in s["cam"]


def test_fixed_cam_untouched(cam_on) -> None:
    s = _shot("固定机位")
    _expand_camera([s])
    assert s["cam"] == "固定机位"  # 固定不走展开（运镜铁律分支依赖原文）


def test_unknown_cam_untouched(cam_on) -> None:
    s = _shot("希区柯克式变焦")  # 词表未命中 → 宁缺毋滥
    _expand_camera([s])
    assert s["cam"] == "希区柯克式变焦"


def test_speed_word_detected(cam_on) -> None:
    s = _shot("镜头快速推进至人物面部特写")
    _expand_camera([s])
    assert "快速" in s["cam"] and "缓慢匀速" not in s["cam"]


def test_switch_off_untouched(monkeypatch) -> None:
    monkeypatch.setitem(_cfg.setdefault("manga", {}), "h3_camera_expand", False)
    s = _shot("镜头缓慢环绕人物移动")
    _expand_camera([s])
    assert s["cam"] == "镜头缓慢环绕人物移动"


# ── 批5：OpenH3-IR 候选规则 lint（manga.h3_rules_lint 默认 True 纯告警）──


def test_rules_r13_sheet_artefact() -> None:
    refs = [{"kind": "character", "name": "主角四视图", "src": None}]
    hits = _lint_contract_rules([{"text": "正常画面内容描写。", "cam": ""}], refs)
    assert any(h.startswith("R13") for h in hits)


def test_rules_r17_r19_r20_r28() -> None:
    dup = "剑客在演武场中央持剑站立，目光扫过木人桩，衣摆随风微动。"
    shots = [
        {"text": dup, "cam": ""},
        {"text": dup, "cam": ""},          # R17：相邻拍全同
        {"text": "见第2镜", "cam": ""},     # R28-30：元语言泄漏
        {"text": "中景侧面，镜头缓慢环绕人物移动。", "cam": "固定机位"},  # R20
    ]
    hits = _lint_contract_rules(shots, [])
    assert any(h.startswith("R17") for h in hits)
    assert any(h.startswith("R28-30") for h in hits)
    assert any(h.startswith("R20") for h in hits)
    hits2 = _lint_contract_rules([{"text": "剑客", "cam": ""}], [])
    assert any(h.startswith("R19") for h in hits2)


def test_rules_clean_prompt_zero_hits() -> None:
    hits = _lint_contract_rules(
        [{"text": "剑客收剑回身，衣摆随风落定，尘幕缓缓沉降。", "cam": ""}], [])
    assert hits == []
