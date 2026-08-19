"""遥测诚实性测试（TASK-P1-05，审计 P07/P11）。

锁定的契约：硬件探测失败时后端必须显式暴露"未知"（None + available=False
+ degraded_probes），绝不回填假读数；显存守门人无静默失败。

历史缺陷（本测试防回归）：
  - hardware.py 探测失败回填 35.0%/8192MB/52°C/"Mock CPU" 等编造数据
  - vram_manager 四处 except Exception: pass——探测失败完全不可观测
"""
from __future__ import annotations

import pytest

from backend.api import hardware as hw
from backend.engines import vram_manager as vm_mod
from backend.engines.vram_manager import VramManager

# ── hardware.py：实时遥测失败必须诚实标记未知 ─────────────────────

def test_realtime_psutil_unavailable_all_unknown():
    """psutil 不可用：全部 available=False、数值 None，无任何编造读数。"""
    orig = hw._try_psutil
    hw._try_psutil = lambda: None
    try:
        data = hw._realtime_data()
    finally:
        hw._try_psutil = orig

    assert data["gpu"]["available"] is False
    assert data["cpu"]["available"] is False
    assert data["ram"]["available"] is False
    for section in ("gpu", "cpu", "ram"):
        for key, value in data[section].items():
            if key in ("available",):
                continue
            assert value is None, f"{section}.{key} 探测失败应为 None，实得 {value!r}"
    # 曾经的假读数必须绝迹
    assert data["gpu"]["usage_percent"] != 35.0
    assert data["gpu"]["vram_used_mb"] != 8192


def test_realtime_gpu_probe_failure_marks_unknown(monkeypatch):
    """HardwareMonitor 抛异常：GPU 遥测 available=False，不回退零值假读数。"""

    class _Boom:
        def get_gpu(self):
            raise RuntimeError("nvml 不可用")

    monkeypatch.setattr(hw, "_monitor", _Boom())
    gpu = hw._realtime_gpu()
    assert gpu["available"] is False
    assert gpu["usage_percent"] is None
    assert gpu["vram_total_mb"] is None
    assert gpu["temp_celsius"] is None


def test_hardware_profile_failure_no_mock_data(monkeypatch):
    """整链探测失败：画像为未知（零值 + unknown），不再出现 Mock CPU/32GB/512GB。"""
    monkeypatch.setattr(hw, "_try_psutil", lambda: None)
    profile = hw._build_hardware_profile()

    assert profile["cpu"]["name"] != "Mock CPU"
    assert profile["cpu"]["name"] == "未检测到独立显卡" or "未知" in profile["cpu"]["name"] \
        or profile["cpu"]["name"] == ""
    assert profile["ram"]["total_gb"] == 0.0
    assert profile["disk"]["total_gb"] == 0.0
    assert profile["power"] == "unknown"


def test_realtime_normal_path_marks_available():
    """psutil 正常（真实环境可用）：available=True 且带真实读数。"""
    psutil = hw._try_psutil()
    if psutil is None:
        pytest.skip("本机无 psutil，正常路径无法验证")
    data = hw._realtime_data()
    assert data["cpu"]["available"] is True
    assert isinstance(data["cpu"]["usage_percent"], (int, float))


# ── vram_manager：探测失败必须可观测（degraded_probes）────────────

class _FakeCuda:
    """模拟 CUDA 探测全链失败的 torch 桩。"""

    class cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def get_device_properties(idx):
            raise RuntimeError("device lost")

        @staticmethod
        def memory_allocated(idx):
            raise RuntimeError("context destroyed")

        @staticmethod
        def mem_get_info(idx):
            raise RuntimeError("nvml down")

        @staticmethod
        def empty_cache():
            raise RuntimeError("cache flush failed")


def _fresh_manager(monkeypatch, fake) -> VramManager:
    monkeypatch.setattr(vm_mod, "_torch", fake)
    return VramManager()


def test_vram_total_probe_failure_recorded(monkeypatch):
    """总显存探测失败：degraded_probes 记录 + 警告可观测，不再静默。"""
    mgr = _fresh_manager(monkeypatch, _FakeCuda)
    assert "vram_total_mb" in mgr._degraded_probes
    assert mgr._vram_total_mb == 0.0


def test_vram_usage_exposes_degraded_probes(monkeypatch):
    """get_usage 携带 degraded_probes：上层可判断读数是探测还是纯记账。"""
    mgr = _fresh_manager(monkeypatch, _FakeCuda)
    usage = mgr.get_usage()
    assert "degraded_probes" in usage
    assert "memory_allocated" in usage["degraded_probes"]


def test_vram_available_fallback_recorded(monkeypatch):
    """mem_get_info 失败：退化记账差值并记录，不再静默 pass。"""
    mgr = _fresh_manager(monkeypatch, _FakeCuda)
    assert mgr.get_available_mb() == 0.0  # 记账为 0 的退化值
    assert "mem_get_info" in mgr._degraded_probes


def test_vram_no_torch_is_pure_bookkeeping(monkeypatch):
    """无 torch（CPU 环境）：不产生降级记录（非失败，是明确的无 CUDA）。"""
    mgr = _fresh_manager(monkeypatch, None)
    usage = mgr.get_usage()
    assert usage["degraded_probes"] == {}
    assert usage["total_mb"] == 0.0
