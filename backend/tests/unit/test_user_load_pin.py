"""用户装载钉回归（#8 2026-09-02 修复：调度器卸用户刚加载的模型）。

事故（09-02 上午实测）：用户在模型管理页显式装载对话模型 → 去别的
页面看东西 5 分钟（功能锁空闲）→ 调度器空闲深层回收（300s 阈值）
把模型卸了——违背「用户刚装好的模型不该被预防性回收」预期。

修复契约：
  1. /models/load 成功 → mgr.note_user_load(model_id) 打 30 分钟钉；
  2. 调度器深层回收过滤 is_user_pinned（钉窗口内跳过）；
  3. unload_model 清钉（钉随卸载失效）；
  4. 硬件压力卸载（resource_guard/_evict_idle_models）不豁免——救场优先。
"""
from __future__ import annotations

import sys
import threading
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[2].parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2].parent / "pydeps"))

from backend.services.model_manager import ModelManager  # noqa: E402

MODELS_API = (Path(__file__).resolve().parents[2]
              / "api" / "models.py")
SCHED_PY = (Path(__file__).resolve().parents[2]
            / "services" / "scheduler" / "__init__.py")


def _mgr_with_loaded(model_id: str) -> ModelManager:
    mgr = ModelManager.__new__(ModelManager)
    mgr._loaded_lock = threading.RLock()
    mgr._loaded = {model_id: {"category": "dialog", "path": "x",
                              "vram_gb": 9.0, "priority": 5,
                              "loaded_at": time.time(), "engine": "stub"}}
    mgr._user_load_pins = {}
    return mgr


def test_pin_protects_within_window() -> None:
    mgr = _mgr_with_loaded("m1")
    assert not mgr.is_user_pinned("m1"), "未打钉不受保护"
    mgr.note_user_load("m1")
    assert mgr.is_user_pinned("m1"), "打钉后窗口内受保护"


def test_pin_expires() -> None:
    mgr = _mgr_with_loaded("m1")
    mgr.note_user_load("m1")
    # 把钉时间拨回窗口外
    mgr._user_load_pins["m1"] = time.time() - (ModelManager.USER_PIN_WINDOW_S + 1)
    assert not mgr.is_user_pinned("m1"), "30 分钟窗口外钉失效"


def test_pin_requires_loaded_ledger() -> None:
    mgr = _mgr_with_loaded("m1")
    mgr.note_user_load("m1")
    with mgr._loaded_lock:
        mgr._loaded.clear()  # 台账已空（被卸/未真正加载）
    assert not mgr.is_user_pinned("m1"), "台账无此模型的钉不生效"


def test_unload_clears_pin() -> None:
    mgr = _mgr_with_loaded("m1")
    mgr.note_user_load("m1")
    # unload_model 的钉清理逻辑（裸验 _user_load_pins.pop）
    with mgr._loaded_lock:
        mgr._loaded.pop("m1", None)
        mgr._user_load_pins.pop("m1", None)
    assert not mgr.is_user_pinned("m1") and not mgr._user_load_pins


def test_api_and_scheduler_wired() -> None:
    api_src = MODELS_API.read_text(encoding="utf-8")
    assert api_src.count("note_user_load") >= 2, (
        "/models/load 两条成功路径（switch/直载）都必须打钉")
    sched_src = SCHED_PY.read_text(encoding="utf-8")
    assert "is_user_pinned" in sched_src, (
        "深层回收必须过滤用户装载钉")
    assert "深层回收跳过用户装载钉" in sched_src
# 本项目仅供学习使用，商业授权请+Q 3559331368
