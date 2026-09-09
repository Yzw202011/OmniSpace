"""GGUF 共存档单测（显存调度机制批4，2026-09-10，D2=A）。

覆盖：kind=llama 路由 / GGUF 目录探测 / 装载量估算（不二次折减）/
自动选档前移（空闲装不下 9B W4A16 → 落 GGUF 档+可见性事件）/
llama_service 门控（权重缺失拒绝、软准入拒绝）/ backend 转调。
全部 mock 或轻量路径——**不真起 llama-server 子进程**（实弹需 GPU
活动门检查，见 e2e 窗口）。方案 §3.5。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from backend.config import MODELS_DIR

if TYPE_CHECKING:
    from pytest import MonkeyPatch

_GGUF_DIR = MODELS_DIR / "qwen35-9b-gguf-q4km"


def _gguf_ready() -> bool:
    return _GGUF_DIR.is_dir() and any(_GGUF_DIR.glob("*.gguf"))


class TestRouting:
    def test_detect_backend_gguf_dir_is_llama(self, tmp_path: Path) -> None:
        (tmp_path / "model.Q4_K_M.gguf").write_bytes(b"x")
        from backend.services.inference.dialog_engine import _detect_backend

        assert _detect_backend(tmp_path) == "llama"

    def test_detect_backend_empty_dir_unchanged(self, tmp_path: Path) -> None:
        from backend.services.inference.dialog_engine import _detect_backend

        assert _detect_backend(tmp_path) == ""

    def test_create_backend_llama(self) -> None:
        from backend.services.inference.backends import create_backend
        from backend.services.inference.backends.llama_backend import (
            LlamaServerBackend,
        )

        backend = create_backend("llama")
        assert isinstance(backend, LlamaServerBackend)
        assert backend.name == "llama"
        assert backend.supports_images is False


class TestEstimate:
    def test_llama_estimate_file_size_x1p10(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        """GGUF 估算=体积×1.10（权重常驻+KV 余量），不做二次折减。"""
        from backend.services.inference import dialog_engine as de

        monkeypatch.setattr(
            de, "_dir_weight_bytes", lambda p: int(5.5 * 1024 ** 3))
        assert de._estimate_vram_gb(tmp_path, "llama") == 6.05
        # 与老 gguf 通道同口径（llama-cpp-python 路径不受影响）
        assert de._estimate_vram_gb(tmp_path, "gguf") == 6.05


class TestPickModelAutoTier:
    """自动选档前移：9B W4A16 装不下 → 落 GGUF 档（可见、不静默）。"""

    @pytest.mark.skipif(not _gguf_ready(), reason="GGUF 权重未就位")
    def test_auto_select_falls_to_gguf_when_tight(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.services.inference import dialog_engine as de

        monkeypatch.setattr(de, "_cuda_free_gb", lambda: 12.0)
        events: list[str] = []
        monkeypatch.setattr(
            "backend.services.event_log.log_event",
            lambda *a, **k: events.append(str(a[1])))

        engine = de.DialogEngine.__new__(de.DialogEngine)
        picked = engine._pick_model(None)

        assert picked is not None
        mid, _path, vram, kind = picked
        assert mid == "qwen35-9b-gguf-q4km"
        assert kind == "llama"
        assert vram == 6.0
        # 选档可见性：自动落档记大白话事件（不静默换档）
        assert "model_tier_autoselect" in events

    @pytest.mark.skipif(not _gguf_ready(), reason="GGUF 权重未就位")
    def test_explicit_gguf_request(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.services.inference import dialog_engine as de

        monkeypatch.setattr(de, "_cuda_free_gb", lambda: 12.0)
        engine = de.DialogEngine.__new__(de.DialogEngine)
        picked = engine._pick_model("qwen35-9b-gguf-q4km")
        assert picked is not None
        assert picked[0] == "qwen35-9b-gguf-q4km"
        assert picked[3] == "llama"


class TestLlamaServiceGates:
    """llama_service 门控（不起子进程）。"""

    def test_missing_weights_rejected(self) -> None:
        from backend.engines.llama_service import LlamaService

        svc = LlamaService()
        assert svc.start("Z:/nonexistent/model.gguf") is False
        assert "不存在" in svc._last_error

    def test_missing_runtime_rejected(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.engines import llama_service as ls

        gguf = tmp_path / "m.gguf"
        gguf.write_bytes(b"x")
        monkeypatch.setattr(ls, "LLAMA_EXE", tmp_path / "nope.exe")
        svc = ls.LlamaService()
        assert svc.start(gguf) is False
        assert "运行时缺失" in svc._last_error

    @pytest.mark.skipif(not _gguf_ready(), reason="GGUF 权重未就位")
    def test_admission_rejects_low_free(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        """软准入：空闲 3GB < 共存档 5.7GB → 诚实拒绝（不出路不明错误）。"""
        from backend.engines import llama_service as ls

        gguf = next(_GGUF_DIR.glob("*.gguf"))
        monkeypatch.setattr(
            "backend.services.inference.gpu_budget.read_physical_bytes",
            lambda device=0, *, torch_only=False: (
                True, int(3.0 * 1024 ** 3), int(16.0 * 1024 ** 3)))
        svc = ls.LlamaService()
        assert svc.start(gguf) is False
        assert "低于" in svc._last_error
        assert svc.state == "error"

    def test_healthy_requires_health_endpoint(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.engines import llama_service as ls

        svc = ls.LlamaService()
        monkeypatch.setattr(ls, "_http_get", lambda url, timeout=2.0: True)
        svc._state = "ready"
        assert svc.is_healthy() is True
        svc._state = "booting"
        assert svc.is_healthy() is False


class TestLlamaBackendDelegation:
    def test_load_delegates_to_service(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.engines import llama_service as ls
        from backend.services.inference.backends.llama_backend import (
            LlamaServerBackend,
        )

        gguf = tmp_path / "m.gguf"
        gguf.write_bytes(b"x")
        started: list[str] = []

        def _fake_start(p: object) -> bool:
            started.append(str(p))
            return True

        fake = SimpleNamespace(
            start=_fake_start,
            stop=lambda: True,
            is_healthy=lambda: True,
            _last_error="",
        )
        monkeypatch.setattr(ls, "get_llama_service", lambda: fake)

        backend = LlamaServerBackend()
        assert backend.load("m-id", tmp_path, 6.0) is True
        assert backend.model_id == "m-id"
        assert len(started) == 1

    def test_load_failure_reports_reason(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.engines import llama_service as ls
        from backend.services.inference.backends.llama_backend import (
            LlamaServerBackend,
        )

        (tmp_path / "m.gguf").write_bytes(b"x")
        fake = SimpleNamespace(
            start=lambda p: False,
            stop=lambda: True,
            is_healthy=lambda: False,
            _last_error="llama-server 启动失败: boom",
        )
        monkeypatch.setattr(ls, "get_llama_service", lambda: fake)

        backend = LlamaServerBackend()
        assert backend.load("m-id", tmp_path, 6.0) is False
        assert "boom" in backend.last_error()

    def test_no_gguf_in_dir_rejected(
        self, tmp_path: Path
    ) -> None:
        from backend.services.inference.backends.llama_backend import (
            LlamaServerBackend,
        )

        backend = LlamaServerBackend()
        assert backend.load("m-id", tmp_path, 6.0) is False
        assert "无 .gguf" in backend.last_error()
