"""模型「彻底删除」路由单测（2026-09-16 批3）。

背景：前端 modelApi.purgeModelFiles 调 DELETE /models/{id}/files，
后端此前无此路由（必 404，已固化进 dist）。本批补路由：卸载+注销+
删盘 + 路径安全闸（MODELS_DIR 内或登记的绝对外部路径，深度>2）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.api import models as models_api  # noqa: E402
from src.middleware.error_handler import ApiError  # noqa: E402


class _StubMgr:
    @staticmethod
    def unload_model(model_id: str) -> None:  # noqa: ARG004
        pass


def _patch(monkeypatch: pytest.MonkeyPatch, rec: dict[str, Any] | None,
           ) -> None:
    monkeypatch.setattr(
        models_api, "_find_model",
        lambda mid: rec if rec is not None and mid == rec["id"] else None)
    monkeypatch.setattr(models_api, "get_model_manager", lambda: _StubMgr())
    monkeypatch.setattr(models_api, "get_db_safe", lambda: None)


def test_purge_deletes_files_and_registry(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch
                                          ) -> None:
    model_dir = tmp_path / "test-model"
    model_dir.mkdir()
    # 256 MiB = 0.25 GiB 整，freed_gb 断言可精确对账
    (model_dir / "weights.bin").write_bytes(b"x" * (256 * 1024 * 1024))
    rec = {"id": "test-model", "file_path": str(model_dir),
           "category": "llm"}
    _patch(monkeypatch, rec)
    models_api._models["test-model"] = dict(rec)

    out = models_api.models_purge_files("test-model")

    data = out["data"]
    assert data["deleted"] == "test-model"
    assert data["path"] == str(model_dir)
    assert data["freed_gb"] == pytest.approx(0.25, abs=0.01)
    assert not model_dir.exists()
    assert "test-model" not in models_api._models


def test_purge_safety_gate_rejects_shallow_path(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # E:\x 解析后 parts=('E:\\', 'x') 深度 2 → 必须拒绝（盘根/一级目录防线）
    rec = {"id": "shallow", "file_path": "E:/x", "category": "llm"}
    _patch(monkeypatch, rec)
    with pytest.raises(ApiError):
        models_api.models_purge_files("shallow")


def test_purge_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, None)
    with pytest.raises(ApiError):
        models_api.models_purge_files("ghost")


def test_purge_registry_only_entry_rejected(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """无磁盘路径的纯注册条目：指引用普通删除，不做 purge。"""
    _patch(monkeypatch, {"id": "no-path", "file_path": "", "category": "llm"})
    with pytest.raises(ApiError):
        models_api.models_purge_files("no-path")
