"""ADR-003 P2 验收测试：BaseEngine 协议与品类注册表。

锁定三类不变量：
  ① 新增品类仅经 register_engine_module() 注册即可被 ModelManager
    管理（缓存/解析/注销），ModelManager 本体零改动；
  ② 四引擎（dialog/paint/video/voice）isinstance BaseEngine，既有
    模块级单例 getter 兼容（与 _get_engine 解析结果同一实例）；
  ③ 未注册/损坏品类容错降级 None（与迁移前 if/elif 语义对齐）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import pytest

from src.services.inference.base_engine import (
    ENGINE_MODULES,
    BaseEngine,
    register_engine_module,
    resolve_engine,
    unregister_engine_module,
)

pytestmark = pytest.mark.smoke


# ── ① 注册表驱动：fake 品类仅注册即可被管理 ──────────────────────────

class _FakeEngine(BaseEngine):
    name = "fake"
    serves_categories = ("fake_cat",)

    def __init__(self) -> None:
        super().__init__()
        self.loaded_id: str = ""
        self.unload_count: int = 0

    def load_model(self, model_id: str | None = None) -> bool:
        self.loaded_id = model_id or ""
        return True

    def unload_model(self) -> bool:
        self.unload_count += 1
        return True


def _make_fake_engine() -> _FakeEngine:
    return _FakeEngine()


def _bare_manager():
    """构造仅带 _engines 缓存字段的裸 ModelManager（避免 NVML/torch 初始化）。"""
    from src.services.model_manager import ModelManager

    mgr = ModelManager.__new__(ModelManager)
    mgr._engines = {}
    return mgr


def test_fake_category_resolvable_via_registration_only() -> None:
    register_engine_module("fake_cat",
                           "tests.unit.test_base_engine",
                           "_make_fake_engine",
                           class_name="_FakeEngine")
    try:
        mgr = _bare_manager()
        eng = mgr._get_engine("fake_cat")
        assert isinstance(eng, _FakeEngine)
        # 引擎缓存：同品类二次解析返回同一实例
        assert mgr._get_engine("fake_cat") is eng
    finally:
        assert unregister_engine_module("fake_cat") is True
    # 注销后按未注册品类降级
    assert _bare_manager()._get_engine("fake_cat") is None


def test_unregistered_and_broken_categories_degrade_to_none() -> None:
    assert resolve_engine("") is None
    assert resolve_engine("auxiliary") is None      # 无引擎品类语义不变
    assert resolve_engine("embedding") is None
    assert _bare_manager()._get_engine("no_such_engine") is None
    # 注册指向不存在模块 → 容错 None（绝不抛异常）
    register_engine_module("broken_cat", "tests.unit.no_such_module_xyz")
    try:
        assert resolve_engine("broken_cat") is None
    finally:
        unregister_engine_module("broken_cat")


# ── ② 协议接入与 getter 兼容 ────────────────────────────────────────

def test_all_first_batch_engines_implement_protocol() -> None:
    from src.services.inference.dialog_engine import DialogEngine
    from src.services.inference.paint_engine import PaintEngine
    from src.services.inference.video_engine import VideoEngine
    from src.services.inference.voice_engine import VoiceEngine

    for cls in (DialogEngine, PaintEngine, VideoEngine, VoiceEngine):
        assert issubclass(cls, BaseEngine), f"{cls.__name__} 未接入 BaseEngine"
        eng = cls()
        # 既有约定：is_ready 为 @property（四引擎 2026-08-29 实测一致）
        assert isinstance(eng.is_ready, bool)
        status = eng.get_status()
        assert isinstance(status, dict) and status.get("engine")


def test_builtin_registry_covers_legacy_alias_groups() -> None:
    """迁移等价性：if/elif 时代的全部品类别名组在注册表中逐一可解析。"""
    for cat in ("dialog", "language", "omni",
                "paint", "vision", "image",
                "video", "video_gen", "voice"):
        assert cat in ENGINE_MODULES, f"品类 {cat} 未迁移进注册表"


def test_singleton_getter_parity_with_manager_resolution() -> None:
    """getter 兼容零破坏：_get_engine 解析结果与既有模块级单例同一实例。"""
    from src.services.inference.dialog_engine import get_dialog_engine
    from src.services.inference.paint_engine import get_paint_engine

    mgr = _bare_manager()
    assert mgr._get_engine("paint") is get_paint_engine()
    assert mgr._get_engine("dialog") is get_dialog_engine()
    # voice 为 P2 新接入：getter 与管理器解析同一单例
    from src.services.inference.voice_engine import get_voice_engine
    assert mgr._get_engine("voice") is get_voice_engine()


# ── BaseEngine 协议本身 ─────────────────────────────────────────────

def test_base_engine_is_abstract() -> None:
    with pytest.raises(TypeError):
        BaseEngine()  # noqa: B015 - 抽象类禁止实例化即协议锁定


def test_base_engine_default_status_and_error_channel() -> None:
    class _Minimal(BaseEngine):
        def load_model(self, model_id: str | None = None) -> bool:
            self._note_error("boom")
            return False

        def unload_model(self) -> bool:
            return False

    eng = _Minimal()
    assert eng.is_ready is False
    assert eng.load_model() is False
    assert eng.last_error() == "boom"
    status = eng.get_status()
    assert status["ready"] is False and status["last_error"] == "boom"
    assert status["engine"] == "_Minimal"


def test_voice_unload_releases_refs_and_resets_retry_flag() -> None:
    from src.services.inference.voice_engine import VoiceEngine

    eng = VoiceEngine()
    eng._model = object()      # 模拟已装载
    eng._loaded = True
    eng._tts_autoload_attempted = True
    assert eng.unload_model() is True
    assert eng._model is None and eng._loaded is False
    assert eng._tts_autoload_attempted is False
    # 空载卸载：False（非错误语义）
    assert eng.unload_model() is False
