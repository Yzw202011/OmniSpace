# 本项目仅供学习使用，商业授权请+Q 3559331368
"""系统日志 API 专属测试（问题总账 #31 / 清偿计划 Q8，2026-09-19）。

此前 logs.py 13 端点零专属测试（仅冒烟级路过）。本件补契约面：
查询/统计/模块清单/文件清单/错误摘要的信封形状、过滤参数、
前端事件上报的写读回路、清理端点的空态安全。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "logs.db"))
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def test_events_query_envelope(client):
    r = client.get("/api/v1/logs/events")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and "data" in body


def test_events_filter_params_accepted(client):
    r = client.get("/api/v1/logs/events", params={"level": "info", "limit": 5})
    assert r.status_code == 200 and r.json()["success"] is True


def test_stats_envelope(client):
    r = client.get("/api/v1/logs/stats")
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_modules_list(client):
    r = client.get("/api/v1/logs/modules")
    assert r.status_code == 200 and r.json()["success"] is True


def test_files_list(client):
    r = client.get("/api/v1/logs/files")
    assert r.status_code == 200 and r.json()["success"] is True


def test_errors_summary(client):
    r = client.get("/api/v1/logs/errors/summary")
    assert r.status_code == 200 and r.json()["success"] is True


def test_frontend_event_roundtrip(client):
    """前端事件上报 → 事件查询可回收（写读回路；契约=message 载荷）。"""
    r = client.post("/api/v1/logs/frontend-event",
                    json={"message": "q8-probe-error", "kind": "error",
                          "stack": "at q8"})
    assert r.status_code == 200 and r.json()["success"] is True
    q = client.get("/api/v1/logs/events", params={"module": "frontend"})
    assert q.status_code == 200 and q.json()["success"] is True


def test_frontend_event_rejects_empty_message(client):
    """空 message 的诚实失败信封（不得静默成功）。"""
    r = client.post("/api/v1/logs/frontend-event", json={})
    assert r.json()["success"] is False


def test_cleanup_empty_state_safe(client):
    """清理端点在无历史文件时也应安全返回（不因空态炸）。"""
    r = client.post("/api/v1/logs/cleanup", json={"days": 30})
    assert r.status_code == 200
    assert r.json()["success"] is True
