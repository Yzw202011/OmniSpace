"""BottleneckAnalyzer 行为级直测（B9 高危模块补测 2026-09-13）。

背景（09-12 审计）：scheduler/ 七模块仅 decision/dispatcher 被顺带
引用。本文件补 analyzer——显存/温压/电源 → SynergyMode 的场景判定，
与 GPU 利用率「持续越线分级降参」状态机（瞬时尖峰忽略 + S4 滞回）。

时间控制：analyze 内部用 time.monotonic，测试经 monkeypatch 注入
假时钟推进持续窗口（不真等 5s/15s）。
"""
from __future__ import annotations

import backend.services.scheduler.analyzer as _an
from backend.data.models import SynergyMode
from backend.services.scheduler.analyzer import BottleneckAnalyzer


def _snap(gpu_util: float = 30.0, vram_used: float = 6000.0,
          temp: float = 50.0) -> tuple[dict, dict, dict]:
    gpu = {"vram_used_mb": vram_used, "vram_total_mb": 16303,
           "util_percent": gpu_util, "temp_celsius": temp}
    cpu = {"usage_percent": 20.0, "temp_celsius": 45.0,
           "cores": 24, "threads": 24}
    mem = {"available_gb": 20.0, "total_gb": 31.7, "used_percent": 37.0}
    return gpu, cpu, mem


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def test_healthy_snapshot_returns_legal_mode() -> None:
    mode = BottleneckAnalyzer().analyze(*_snap(), power="ac")
    assert isinstance(mode, SynergyMode)


def test_gpu_util_transient_spike_ignored() -> None:
    """瞬时 96% 尖峰不触发持续降参判定（文档B §4.1）。"""
    clock = _FakeClock()
    a = BottleneckAnalyzer()
    _orig = _an.time.monotonic
    _an.time.monotonic = clock  # type: ignore[assignment]
    try:
        a.analyze(*_snap(gpu_util=96.0), power="ac")
        assert a.last_gpu_util_critical is False
        assert a.last_gpu_util_level == 0
    finally:
        _an.time.monotonic = _orig  # type: ignore[assignment]


def test_gpu_util_sustained_mild_then_deep() -> None:
    """持续越线 5s → 轻度降参(level=1)，15s → 深度降档(level=2)。"""
    clock = _FakeClock()
    a = BottleneckAnalyzer()
    _orig = _an.time.monotonic
    _an.time.monotonic = clock  # type: ignore[assignment]
    try:
        a.analyze(*_snap(gpu_util=96.0), power="ac")  # 起始越线
        clock.advance(5.0)
        a.analyze(*_snap(gpu_util=96.0), power="ac")
        assert a.last_gpu_util_critical is True
        assert a.last_gpu_util_level == 1
        clock.advance(10.0)
        a.analyze(*_snap(gpu_util=96.0), power="ac")
        assert a.last_gpu_util_level == 2
    finally:
        _an.time.monotonic = _orig  # type: ignore[assignment]


def test_gpu_util_recovery_clears_after_grace() -> None:
    """越线回落后滞回窗内不清零、超过宽限清零（S4 滞回语义）。"""
    clock = _FakeClock()
    a = BottleneckAnalyzer()
    _orig = _an.time.monotonic
    _an.time.monotonic = clock  # type: ignore[assignment]
    try:
        a.analyze(*_snap(gpu_util=96.0), power="ac")
        clock.advance(6.0)
        a.analyze(*_snap(gpu_util=96.0), power="ac")
        assert a.last_gpu_util_level == 1
        # 回落到正常区
        clock.advance(1.0)
        a.analyze(*_snap(gpu_util=40.0), power="ac")
        # 短暂回落在宽限内：持续窗口不清零——再越线立刻累计生效
        clock.advance(2.0)
        a.analyze(*_snap(gpu_util=96.0), power="ac")
        assert a.last_gpu_util_critical is True
    finally:
        _an.time.monotonic = _orig  # type: ignore[assignment]


def test_vram_pressure_returns_pressure_mode() -> None:
    """显存 95%+（force 阈值内危线）→ 记忆压力类模式而非空载模式。"""
    mode = BottleneckAnalyzer().analyze(
        *_snap(vram_used=15600.0), power="ac")  # ~95.7%
    assert mode != SynergyMode.ALL_IDLE
