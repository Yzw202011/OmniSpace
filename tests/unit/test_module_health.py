"""批2 P31/P32（2026-09-19）单测：功能舱壁账本 + 自愈三闸。

全部经 monkeypatch 隔离真实引擎恢复（绝不拉模型）与 WS 广播。
"""
# 本项目仅供学习使用，商业授权请+Q 3553191368
from __future__ import annotations

import time

import pytest

from src.services import module_health as mh
from src.services import self_heal as sh


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """每测隔离：清账本/清自愈态；广播与自愈触发全部替换为记录器。"""
    mh.reset_all()
    sh.reset_all()
    events: list[tuple] = []
    recoveries: list[str] = []
    monkeypatch.setattr(mh, "_broadcast", lambda m: events.append(("bc", m)))
    monkeypatch.setattr(sh, "_emit", lambda m, k, s, msg: events.append(("emit", m, k)))
    monkeypatch.setattr(sh, "_run_recovery", lambda m, t: recoveries.append(m))
    monkeypatch.setattr(mh, "DEGRADE_THRESHOLD", 3)
    yield {"events": events, "recoveries": recoveries}


def test_code_to_module_mapping() -> None:
    assert mh.module_for_code("MODEL_NOT_LOADED") == "dialog"
    assert mh.module_for_code("PAINT_ENGINE_NOT_READY") == "paint"
    assert mh.module_for_code("MANGA_VIDEO_FAILED") == "video"
    assert mh.module_for_code("TRAINING_QUEUED") == "training"
    assert mh.module_for_code("SYSTEM_PARAM_INVALID") == ""  # 非重舱不计


def test_degrade_only_after_three_consecutive(_isolated) -> None:
    mh.record_failure("PAINT_ENGINE_NOT_READY", "e1")
    mh.record_failure("PAINT_TIMEOUT", "e2")
    snap = mh.snapshot()
    assert snap["modules"]["paint"]["state"] == "ok"
    assert snap["modules"]["paint"]["consecutive_failures"] == 2
    assert _isolated["recoveries"] == []  # 未达阈值不触发自愈
    mh.record_failure("PAINT_GENERATE_FAILED", "e3")
    assert mh.snapshot()["modules"]["paint"]["state"] == "degraded"
    assert _isolated["recoveries"] == ["paint"]  # degrade 即触发自动恢复
    assert ("bc", "paint") in _isolated["events"]


def test_success_resets_degraded(_isolated) -> None:
    for i in range(3):
        mh.record_failure("VIDEO_TASK_FAILED", f"e{i}")
    assert mh.snapshot()["modules"]["video"]["state"] == "degraded"
    mh.record_success("video")
    snap = mh.snapshot()
    assert snap["modules"]["video"]["state"] == "ok"
    assert snap["modules"]["video"]["consecutive_failures"] == 0
    assert snap["degraded"] == []


def test_other_module_unaffected(_isolated) -> None:
    """舱壁语义：绘画舱连续失败，对话舱账本不动。"""
    for i in range(3):
        mh.record_failure("PAINT_FAILED", f"e{i}")
    assert mh.snapshot()["modules"]["dialog"]["state"] == "ok"
    assert mh.snapshot()["modules"]["dialog"]["consecutive_failures"] == 0


def test_self_heal_single_flight_and_cooldown(_isolated, monkeypatch) -> None:
    """三闸之一：同舱单飞（在跑时不重复发起）。"""
    monkeypatch.setattr(sh, "COOLDOWN_S", 0.0)  # 只测单飞
    assert sh.try_recover("paint", "test") is True
    assert sh.try_recover("paint", "test") is False  # 在跑→拒
    # 模拟恢复线程结束（真实由线程 finally 清；测试直接清）
    with sh._lock:
        sh._running.discard("paint")
    assert sh.try_recover("paint", "test") is True


def test_self_heal_cooldown(_isolated, monkeypatch) -> None:
    """三闸之二：冷却窗内拒绝重复发起。"""
    monkeypatch.setattr(sh, "COOLDOWN_S", 3600.0)
    assert sh.try_recover("dialog", "test") is True
    with sh._lock:
        sh._running.discard("dialog")
        sh._last_attempt["dialog"] = time.time()
    assert sh.try_recover("dialog", "test") is False  # 冷却中


def test_self_heal_unknown_module(_isolated) -> None:
    assert sh.try_recover("nonexistent") is False
    assert sh.try_recover("") is False


def test_manual_reset_attempts(_isolated) -> None:
    sh.reset_module_attempts("paint")
    assert sh.auto_attempts_left("paint") == sh.MAX_AUTO_ATTEMPTS
