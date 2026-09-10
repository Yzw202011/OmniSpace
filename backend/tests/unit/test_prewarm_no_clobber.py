"""预热/热身「就绪不拆台」语义锁定（2026-09-09 反向互踩根修）。

02:39 事故：用户刚手动装好 8B → 进对话页预热默认 9B → 就绪短路因
带模型匹配条件未命中 → 热切换顶掉 8B → 9B 又装不下 → 引擎变空。
修复后语义：预热/热身=「确保有模型可用」，引擎 ready 即短路返回，
**绝不**因目标模型不同而热切换。本测试锁死该行为。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.data import database as db_mod
from backend.data.database import Database

pytestmark = pytest.mark.smoke


class _ReadyEngine:
    """就绪于 8B 的引擎替身：记录 ensure_loaded 调用（拆台即留痕）。"""

    def __init__(self) -> None:
        self.ensure_calls: list[str | None] = []

    def get_status(self) -> dict[str, str]:
        return {"state": "ready", "model": "qwen3-vl-8b-awq"}

    def ensure_loaded(self, model_id: str | None = None) -> bool:
        self.ensure_calls.append(model_id)
        return True


@pytest.fixture()
def client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db_mod, "_db_instance",
                        Database(tmp_path / "prewarm.db"))
    from backend.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def test_prewarm_ready_engine_not_clobbered(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """引擎 ready（8B）时预热请求 9B：短路返回、零 ensure 调用。"""
    import backend.api.dialog as dialog_api

    engine = _ReadyEngine()
    monkeypatch.setattr(dialog_api, "get_dialog_engine", lambda: engine)
    r = client.post("/api/v1/dialog/prewarm",
                    json={"model_id": "qwen35-9b-w4a16"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["prewarmed"] is False
    assert data["state"] == "ready"
    assert data["model"] == "qwen3-vl-8b-awq"
    assert engine.ensure_calls == [], "就绪引擎不得被预热热切换拆台"


def test_warmup_ready_engine_not_clobbered(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """/models/warmup 同语义：ready 即达标，不按目标匹配切换。"""
    # models.py 的 dialog 分支为函数内 import——替身源模块符号
    import backend.services.inference.dialog_engine as de_mod
    engine = _ReadyEngine()
    monkeypatch.setattr(de_mod, "get_dialog_engine", lambda: engine)
    r = client.post("/api/v1/models/warmup",
                    json={"feature": "dialog",
                          "model_id": "qwen35-9b-w4a16"})
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["started"] is False
    assert body["data"]["reason"] == "already_ready"
    assert engine.ensure_calls == []


class _ColdEngine:
    """冷引擎替身：记录 ensure_loaded 调用（云端绑定下不得被点火）。"""

    def __init__(self) -> None:
        self.ensure_calls: list[str | None] = []

    def get_status(self) -> dict[str, str]:
        return {"state": "unloaded", "model": ""}

    def ensure_loaded(self, model_id: str | None = None) -> bool:
        self.ensure_calls.append(model_id)
        return True


def test_warmup_cloud_bound_skips_local(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """dialog.text 云端绑定期预热直接短路（2026-09-10 竞态根修）：
    预热线程曾跨越大显存等待窗口，在用户绑定云端后走完本地降级链
    （误导横幅+空拉 vLLM）。绑定生效时根本不该点火本地装载。"""
    import backend.services.inference.backends.remote_backend as rb
    import backend.services.inference.dialog_engine as de_mod

    engine = _ColdEngine()
    monkeypatch.setattr(rb, "is_remote_dialog_enabled", lambda: True)
    monkeypatch.setattr(de_mod, "get_dialog_engine", lambda: engine)
    r = client.post("/api/v1/models/warmup",
                    json={"feature": "dialog",
                          "model_id": "qwen35-9b-gguf-q4km"})
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["started"] is False
    assert body["data"]["reason"] == "cloud_bound"
    assert engine.ensure_calls == [], "云端绑定期不得点火本地装载"
