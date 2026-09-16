"""提交内存闸门补口回归（2026-09-05 A2，架构升级计划 P0-2）。

静默死亡根因 = WER RADAR_PRE_LEAK_64 提交（commit）耗尽，旧守卫只盯
物理 RAM 百分比盯不住它。本文件锁定：commit 占比越线必须走同一卸载链
（卸空闲模型 → gc → 工作集收缩 → shed_memory 危急收缩）；采样失败 /
非 Windows（None）静默跳过不阻断主链；冷却期内不重复触发。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

from src.services import resource_guard as rg


def _fresh_guard() -> rg.ResourceGuard:
    """直接实例化（绕开模块单例），测试间互不污染。"""
    return rg.ResourceGuard()


def _quiet(g: rg.ResourceGuard, monkeypatch) -> dict:
    """屏蔽副作用出口（日志/广播），返回调用记录字典。"""
    calls: dict = {}
    monkeypatch.setattr(g, "_evict_idle_models",
                        lambda reason: calls.setdefault("reason", reason)
                        or 2)
    monkeypatch.setattr(g, "_shrink_working_set", lambda: None)
    monkeypatch.setattr(g, "_broadcast",
                        lambda event, message, warning=False:
                        calls.setdefault("event", event))
    monkeypatch.setattr(rg, "_log_event",
                        lambda *a, **k: calls.setdefault("logged", True))
    return calls


class _FakeMgr:
    def shed_memory(self) -> dict:
        return {"cache_unloaded": 1, "scan_cache_dropped": 1}


def test_commit_over_threshold_triggers_shed(monkeypatch) -> None:
    g = _fresh_guard()
    calls = _quiet(g, monkeypatch)
    monkeypatch.setattr(rg, "_commit_charge_ratio", lambda: 0.95)
    monkeypatch.setattr("src.services.model_manager.get_model_manager",
                        lambda: _FakeMgr())
    g.check(50.0, 0.5)
    assert calls["reason"] == "commit"
    assert calls["event"] == "resource_commit_high"
    assert g.get_status()["session_commit_shed_count"] == 1
    assert g.get_status()["commit_used_ratio"] == 0.95


def test_commit_below_threshold_noop(monkeypatch) -> None:
    g = _fresh_guard()
    calls = _quiet(g, monkeypatch)
    monkeypatch.setattr(rg, "_commit_charge_ratio", lambda: 0.50)
    g.check(50.0, 0.5)
    assert "reason" not in calls
    assert g.get_status()["session_commit_shed_count"] == 0


def test_commit_sample_none_silent(monkeypatch) -> None:
    # 非 Windows / GetPerformanceInfo 失败：跳过维度，RAM/VRAM 主链不受阻
    g = _fresh_guard()
    calls = _quiet(g, monkeypatch)
    monkeypatch.setattr(rg, "_commit_charge_ratio", lambda: None)
    g.check(50.0, 0.5)
    assert "reason" not in calls
    assert g.get_status()["commit_used_ratio"] == 0.0


def test_commit_cooldown_suppresses_repeat(monkeypatch) -> None:
    g = _fresh_guard()
    calls = _quiet(g, monkeypatch)
    monkeypatch.setattr(rg, "_commit_charge_ratio", lambda: 0.97)
    monkeypatch.setattr("src.services.model_manager.get_model_manager",
                        lambda: _FakeMgr())
    g.check(50.0, 0.5)
    g.check(50.0, 0.5)  # 冷却期内第二次越线：不重复卸载
    assert g.get_status()["session_commit_shed_count"] == 1
    assert calls["reason"] == "commit"


def test_real_sample_returns_ratio_or_none() -> None:
    # 真实采样冒烟：本机（Windows）应返回 0~1 的占比；其他平台 None。
    # 只锁类型契约，不锁数值。
    ratio = rg._commit_charge_ratio()
    assert ratio is None or 0.0 < ratio < 1.0
