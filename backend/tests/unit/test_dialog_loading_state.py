"""对话引擎装载期状态如实上报（2026-09-08：模型切换进度条的根）

用户报「切换模型时进度条不出来」：vl/text/gguf 后端装载期间
get_status().state 停在 unavailable/unloaded，右栏 DialogWarmupBar
（认 loading/booting）因此永不显示。修复=load_model 进入实际装载段
即置 _state="loading"。本测试用假后端挂住装载窗口，断言装载期间
对外状态为 loading、完成后 ready——零 GPU、时序可控。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import backend.services.inference.dialog_engine as de

pytestmark = pytest.mark.smoke


class _FakeBackend:
    name = "gguf"
    lora_version = ""

    def __init__(self) -> None:
        self.release = threading.Event()

    def load(self, mid, path, required_gb) -> bool:  # noqa: ANN001
        self.release.wait(timeout=15)
        return True

    def last_error(self) -> str:
        return ""


def test_loading_state_during_model_load(monkeypatch):
    # 事件日志打桩：防止测试污染真实 logs/events
    import backend.services.event_log as ev
    monkeypatch.setattr(ev, "log_event", lambda *a, **k: None)

    eng = de.DialogEngine()
    fake = _FakeBackend()
    monkeypatch.setattr(de, "create_backend", lambda kind: fake)
    monkeypatch.setattr(eng, "_pick_model",
                        lambda mid: ("fake-m", Path("x"), 1.0, "gguf"))

    t = threading.Thread(target=lambda: eng.load_model("fake-m"))
    t.start()
    time.sleep(0.6)  # 进入装载窗口
    assert eng.get_status()["state"] == "loading", "装载期间应如实报 loading"

    fake.release.set()
    t.join(timeout=15)
    assert t.is_alive() is False
    assert eng.get_status()["state"] == "ready"
    assert eng.get_status()["model"] == "fake-m"


def test_pick_failure_overwrites_loading(monkeypatch):
    """选型失败（全候选不可承载）时 loading 必须被收口为 unavailable。"""
    import backend.services.event_log as ev
    monkeypatch.setattr(ev, "log_event", lambda *a, **k: None)

    eng = de.DialogEngine()
    monkeypatch.setattr(eng, "_pick_model", lambda mid: None)
    assert eng.load_model("nope") is False
    assert eng.get_status()["state"] == "unavailable"
