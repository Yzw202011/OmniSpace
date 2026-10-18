# 本项目仅供学习使用，商业授权请+Q 3559331368
"""N-2 家族补丁单测（2026-10-19）：设置 PUT 严格落库。

背景：PUT /system/settings 先改内存再落库，落库静默失败（kv_set
吞异常）时「本页已生效、重启静默回退」且用户毫不知情。补丁改为
先严格落库、成功才提交内存；失败显式 SYSTEM_DB_BUSY 人话报错。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "s.db"))
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def test_settings_put_roundtrip_ok(client):
    r = client.put("/api/v1/system/settings", json={"theme": "dali"})
    assert r.status_code == 200 and r.json()["success"] is True
    g = client.get("/api/v1/system/settings").json()
    assert g["data"]["theme"] == "dali"


def test_settings_put_failure_is_loud_and_memory_untouched(
        client, monkeypatch):
    """落库失败必须显式报错且内存副本不提交。"""
    import src.data.settings_kv as skv

    def _boom(key, value):
        raise OSError("database is locked")

    monkeypatch.setattr(skv, "kv_set_strict", _boom)
    r = client.put("/api/v1/system/settings", json={"theme": "kaboom"})
    b = r.json()
    assert b["success"] is False
    assert b["error"]["code"] == "SYSTEM_DB_BUSY"
    assert "未生效" in b["error"]["message"]
    # 内存副本不得被半提交：GET 回不到 kaboom
    g = client.get("/api/v1/system/settings").json()
    assert g["data"].get("theme") != "kaboom"
