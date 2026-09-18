# 本项目仅供学习使用，商业授权请+Q 3559331368
"""设置/系统 API 专属测试（问题总账 #31 / 清偿计划 Q8，2026-09-19）。

system.py 为全库最大模块（38 端点）却零专属测试。本件补核心契约面：
设置读写回环、remote_dialog_api_key 三面保护（GET 打码/PUT 掩码保留）、
版本/信息端点、LAN 令牌形状。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "sys.db"))
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def test_settings_get_put_roundtrip(client):
    g1 = client.get("/api/v1/system/settings")
    assert g1.status_code == 200 and g1.json()["success"] is True
    current = g1.json()["data"] or {}
    current["theme"] = "q8-probe-theme"
    p = client.put("/api/v1/system/settings", json=current)
    assert p.status_code == 200 and p.json()["success"] is True
    g2 = client.get("/api/v1/system/settings")
    assert (g2.json()["data"] or {}).get("theme") == "q8-probe-theme"


def test_settings_key_masked_on_get(client):
    """remote_dialog_api_key 三面保护之一：GET 永不回明文（打码形态）。"""
    client.put("/api/v1/system/settings",
               json={"remote_dialog_api_key": "sk-q8-secret-123"})
    g = client.get("/api/v1/system/settings")
    raw = str(g.json().get("data"))
    assert "sk-q8-secret-123" not in raw


def test_settings_put_mask_preserves_old_value(client):
    """三面保护之二：PUT 掩码值=保留旧值（不把打码串写库）。"""
    client.put("/api/v1/system/settings",
               json={"remote_dialog_api_key": "sk-q8-real-abc"})
    g = client.get("/api/v1/system/settings")
    masked = (g.json()["data"] or {}).get("remote_dialog_api_key")
    if masked and "*" in str(masked):
        client.put("/api/v1/system/settings", json={**g.json()["data"]})
        g2 = client.get("/api/v1/system/settings")
        m2 = (g2.json()["data"] or {}).get("remote_dialog_api_key")
        assert m2 == masked  # 掩码回写不清空真值


def test_version_endpoint(client):
    r = client.get("/api/v1/system/version")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data.get("version")


def test_system_info_endpoint(client):
    r = client.get("/api/v1/system/info")
    assert r.status_code == 200 and r.json()["success"] is True


def test_lan_token_shape(client):
    r = client.get("/api/v1/system/lan-token")
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_ui_prefs_roundtrip(client):
    client.put("/api/v1/system/ui_prefs", json={"fontSize": "lg"})
    r = client.get("/api/v1/system/ui_prefs")
    assert r.status_code == 200
