"""RAM 危急线收缩回归（2026-09-02 静默死亡事故后续）。

事故链见 test_vllm_ram_gate.py：85% 动作线卸完空闲模型后 RAM 仍可能
持续高位（可再生缓存占着）→ 提交耗尽原生硬死。修复 = resource_guard
新增 ≥90% 危急线，调用 ModelManager.shed_memory() 吐出可再生内存
（权重缓存全量卸载 + 磁盘扫描缓存丢弃），不碰已加载模型与功能锁。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[2].parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2].parent / "pydeps"))

from src.services import resource_guard as rg  # noqa: E402
from src.services.model_manager import ModelManager  # noqa: E402

GUARD_PY = (Path(__file__).resolve().parents[2]
            / "services" / "resource_guard.py")


def _bare_manager() -> ModelManager:
    """跳过 __init__ 的裸实例（不触发引擎/配置重装配）。"""
    mgr = ModelManager.__new__(ModelManager)
    import threading
    mgr._loaded_lock = threading.RLock()
    mgr._loaded = {}
    mgr._user_load_pins = {}

    class _FakeCache:
        unloaded = 0

        def full_unload(self, except_keys=None):
            self.unloaded += 1
            return 2

    mgr.cache = _FakeCache()
    mgr._disk_scan_cache = {"m1": {"a": 1}, "m2": {"b": 2}}
    mgr._disk_scan_ts = time.time()
    return mgr


def test_shed_memory_drops_regenerables() -> None:
    mgr = _bare_manager()
    out = mgr.shed_memory()
    assert out["cache_unloaded"] == 2
    assert out["scan_cache_dropped"] == 2
    assert mgr._disk_scan_cache == {} and mgr._disk_scan_ts == 0.0


def test_guard_critical_line_calls_shed(monkeypatch) -> None:
    calls: list[dict] = []

    def _fake_get_mgr():
        return _bare_manager()

    import src.services.model_manager as mm
    monkeypatch.setattr(mm, "get_model_manager", _fake_get_mgr)
    g = rg.ResourceGuard.__new__(rg.ResourceGuard)
    import threading
    g._lock = threading.Lock()
    g._last_ram_shed = 0.0
    g._last_critical_shed = 0.0
    g._last_soft_collect = 0.0
    g._ram_shed_count = 0
    g._ram_percent = 94.0
    g._last_event = "normal"
    g._last_event_at = 0.0
    g._last_detail = ""
    g._last_broadcast = 0.0
    monkeypatch.setattr(g, "_evict_idle_models", lambda src: 0)
    monkeypatch.setattr(g, "_broadcast", lambda *a, **k: None)
    monkeypatch.setattr(g, "_shrink_working_set", lambda: None)
    monkeypatch.setattr(
        rg, "_log_event",
        lambda ev, detail, level=None: calls.append((ev, detail)))
    g._shed_ram(94.0)
    assert any(ev == "ram_critical_shed" for ev, _ in calls), (
        "RAM ≥90% 必须触发危急收缩事件")
    assert any(ev == "ram_shed" for ev, _ in calls)


def test_guard_critical_cooldown(monkeypatch) -> None:
    import src.services.model_manager as mm
    monkeypatch.setattr(
        mm, "get_model_manager", lambda: _bare_manager())
    g = rg.ResourceGuard.__new__(rg.ResourceGuard)
    import threading
    g._lock = threading.Lock()
    g._last_ram_shed = 0.0
    g._last_critical_shed = time.monotonic()  # 刚收缩过
    g._last_soft_collect = 0.0
    g._ram_shed_count = 0
    g._ram_percent = 94.0
    g._last_event = "normal"
    g._last_event_at = 0.0
    g._last_detail = ""
    g._last_broadcast = 0.0
    monkeypatch.setattr(g, "_evict_idle_models", lambda src: 0)
    monkeypatch.setattr(g, "_broadcast", lambda *a, **k: None)
    monkeypatch.setattr(g, "_shrink_working_set", lambda: None)
    seen: list[str] = []
    monkeypatch.setattr(
        rg, "_log_event",
        lambda ev, detail, level=None: seen.append(ev))
    g._shed_ram(94.0)
    assert "ram_critical_shed" not in seen, "120s 冷却期内不得重复收缩"


def test_source_pins_threshold_and_lever() -> None:
    src = GUARD_PY.read_text(encoding="utf-8")
    assert "RAM_CRITICAL_RATIO" in src
    assert "shed_memory" in src, "危急线必须接 ModelManager.shed_memory"
    assert "_RAM_CRITICAL_COOLDOWN_S = 120.0" in src
