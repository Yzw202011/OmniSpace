"""让渡协调器与批3 生命周期单源测试（显存调度机制批3，2026-09-10）。

覆盖：去抖唤醒判定（V9-β 语义泛化——跨 source 只认最新排空/非空闲
跳过/默认走 vLLM）、heavy_generation_idle 默认判定、ComfyUI 空闲
豁免（批2 登记簿首次被消费）、evict 驱逐统一语言（活跃功能保护）。
全部 mock，不触真 GPU（GPU 测试活动门铁律）。方案 §3.4。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from backend.services.inference.gpu_budget import (
    VramYieldCoordinator,
    get_busy_registry,
    heavy_generation_idle,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from pytest import MonkeyPatch


def _wait_until(pred: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(what)


@pytest.fixture(autouse=True)
def _clean_busy_registry() -> Generator[None, None, None]:
    """每用例后清空登记簿单例（防跨用例残留）。"""
    yield
    get_busy_registry().clear()


def _fake_lock(monkeypatch: MonkeyPatch, active: str | None) -> None:
    """monkeypatch feature_lock.get_feature_lock 为指定持有者。"""
    monkeypatch.setattr(
        "backend.middleware.feature_lock.get_feature_lock",
        lambda: SimpleNamespace(active_feature=active))


class TestDebouncedWake:
    """协调器去抖（V9-β 泛化：判定单源，唤醒动作可注入）。"""

    def test_fires_when_idle(self) -> None:
        c = VramYieldCoordinator()
        fired: list[str] = []
        c.schedule_wake_if_idle(
            "t", lambda: True, debounce_s=0.1,
            wake=lambda: fired.append("wake"))
        _wait_until(lambda: fired == ["wake"], 2.0, "去抖后应唤醒")

    def test_skipped_when_not_idle(self) -> None:
        c = VramYieldCoordinator()
        fired: list[str] = []
        c.schedule_wake_if_idle(
            "t", lambda: False, debounce_s=0.1,
            wake=lambda: fired.append("wake"))
        time.sleep(0.5)
        assert fired == []

    def test_newer_source_supersedes_older_timer(self) -> None:
        """跨 source：旧定时器让位、新定时器只唤醒一次。"""
        c = VramYieldCoordinator()
        fired: list[str] = []
        c.schedule_wake_if_idle(
            "a", lambda: True, debounce_s=0.3,
            wake=lambda: fired.append("a"))
        time.sleep(0.12)
        c.schedule_wake_if_idle(
            "b", lambda: True, debounce_s=0.15,
            wake=lambda: fired.append("b"))
        time.sleep(0.8)
        assert fired == ["b"]

    def test_default_wake_calls_vllm(self, monkeypatch: MonkeyPatch) -> None:
        """wake 未注入 → 协调器 wake_now 直调 vLLM。"""
        calls: list[int] = []
        monkeypatch.setattr(
            "backend.engines.vllm_service.get_vllm_service",
            lambda: SimpleNamespace(
                wake_from_paint=lambda: calls.append(1)))
        c = VramYieldCoordinator()
        c.schedule_wake_if_idle("t", lambda: True, debounce_s=0.1)
        _wait_until(lambda: calls == [1], 2.0, "默认唤醒应直调 vLLM")

    def test_sleep_calls_vllm(self, monkeypatch: MonkeyPatch) -> None:
        calls: list[int] = []
        monkeypatch.setattr(
            "backend.engines.vllm_service.get_vllm_service",
            lambda: SimpleNamespace(
                sleep_for_paint=lambda: calls.append(1)))
        VramYieldCoordinator().sleep_for_generation("t")
        assert calls == [1]

    def test_best_effort_never_raises(self, monkeypatch: MonkeyPatch) -> None:
        """引擎侧异常只记日志，绝不向调用方抛出。"""
        def _boom() -> object:
            raise RuntimeError("engine down")

        monkeypatch.setattr(
            "backend.engines.vllm_service.get_vllm_service", _boom)
        c = VramYieldCoordinator()
        c.sleep_for_generation("t")  # 不抛
        c.wake_now("t")  # 不抛


class TestHeavyGenerationIdle:
    """去抖唤醒默认空闲判定（keyframe 等无队列状态的编排点用）。"""

    def test_locked_paint_not_idle(self, monkeypatch: MonkeyPatch) -> None:
        _fake_lock(monkeypatch, "paint")
        assert heavy_generation_idle() is False

    def test_locked_video_gen_not_idle(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        _fake_lock(monkeypatch, "video_gen")
        assert heavy_generation_idle() is False

    def test_dialog_lock_counts_idle(self, monkeypatch: MonkeyPatch) -> None:
        """对话锁不算重型生成（不拦 vLLM 唤醒——唤醒的正是它）。"""
        _fake_lock(monkeypatch, "dialog")
        assert heavy_generation_idle() is True

    def test_busy_registry_paint_not_idle(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        _fake_lock(monkeypatch, None)
        registry = get_busy_registry()
        token = registry.register("paint", "local", "x")
        assert heavy_generation_idle() is False
        registry.unregister(token)
        assert heavy_generation_idle() is True


class TestComfyIdleExemption:
    """ComfyUI 空闲计时豁免（批2 登记簿首次被消费）。"""

    @staticmethod
    def _mgr() -> object:
        from backend.services.inference.comfy_proc import ComfyProcManager

        return ComfyProcManager.__new__(ComfyProcManager)

    def test_locked_paint_freezes(self, monkeypatch: MonkeyPatch) -> None:
        _fake_lock(monkeypatch, "paint")
        assert self._mgr()._generation_active() is True  # type: ignore[attr-defined]

    def test_registry_video_gen_freezes(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        _fake_lock(monkeypatch, None)
        registry = get_busy_registry()
        token = registry.register("video_gen", "local", "v")
        assert self._mgr()._generation_active() is True  # type: ignore[attr-defined]
        registry.unregister(token)
        assert self._mgr()._generation_active() is False  # type: ignore[attr-defined]

    def test_dialog_lock_not_freezes(self, monkeypatch: MonkeyPatch) -> None:
        """对话持锁不冻结 ComfyUI 计时——杀 ComfyUI 腾显存给对话
        恰是期望行为（保持原语义）。"""
        _fake_lock(monkeypatch, "dialog")
        assert self._mgr()._generation_active() is False  # type: ignore[attr-defined]


class TestEvictUnifiedLanguage:
    """驱逐统一语言：持锁活跃功能所需类别受保护（VACE 误卸收敛）。"""

    @staticmethod
    def _mgr(video: bool = True, dialog: bool = True) -> object:
        from backend.services.model_manager import ModelManager

        mgr = ModelManager.__new__(ModelManager)
        mgr._loaded = {}
        if video:
            mgr._loaded["vid"] = {
                "category": "video", "priority": 3,
                "loaded_at": 1.0, "vram_gb": 13.0}
        if dialog:
            mgr._loaded["dlg"] = {
                "category": "dialog", "priority": 5,
                "loaded_at": 2.0, "vram_gb": 12.0}
        mgr._loaded_lock = threading.Lock()
        return mgr

    def test_active_feature_protected(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.services.model_manager import ModelManager

        unloaded: list[str] = []

        def _record_unload(self: object, mid: str) -> bool:
            unloaded.append(mid)
            return True

        monkeypatch.setattr(ModelManager, "unload_model", _record_unload)
        _fake_lock(monkeypatch, "dialog")
        mgr = self._mgr()
        assert mgr.evict_lowest_priority() is True  # type: ignore[attr-defined]
        # dialog 持锁 → dialog 类被保护，驱逐 video（尽管其优先级更高）
        assert unloaded == ["vid"]

    def test_no_lock_evicts_lowest_priority(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.services.model_manager import ModelManager

        unloaded: list[str] = []

        def _record_unload(self: object, mid: str) -> bool:
            unloaded.append(mid)
            return True

        monkeypatch.setattr(ModelManager, "unload_model", _record_unload)
        _fake_lock(monkeypatch, None)
        mgr = self._mgr()
        assert mgr.evict_lowest_priority() is True  # type: ignore[attr-defined]
        assert unloaded == ["vid"]  # 无锁：按优先级最低者（video=3）

    def test_all_protected_returns_false(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.services.model_manager import ModelManager

        monkeypatch.setattr(
            ModelManager, "unload_model",
            lambda self, mid: True)
        _fake_lock(monkeypatch, "dialog")
        mgr = self._mgr(video=False, dialog=True)  # 只有 dialog 类
        assert mgr.evict_lowest_priority() is False  # type: ignore[attr-defined]
