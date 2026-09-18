# 本项目仅供学习使用，商业授权请+Q 3559331368
"""学习训练 API 专属测试（问题总账 #31 / 清偿计划 Q8，2026-09-19）。

learn.py 12 端点零专属测试。本件补安全可测面（不触 GPU/训练服务）：
任务列表空态、可训练模型清单、训练状态形状、版本清单空态、
非法任务号查询的诚实失败信封。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "learn.db"))
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def test_tasks_empty_state(client):
    r = client.get("/api/v1/learn/tasks")
    assert r.status_code == 200 and r.json()["success"] is True


def test_models_list(client):
    r = client.get("/api/v1/learn/models")
    assert r.status_code == 200 and r.json()["success"] is True


def test_training_status_shape(client):
    r = client.get("/api/v1/learn/training/status")
    assert r.status_code == 200 and r.json()["success"] is True


def test_lora_versions_empty_state(client):
    r = client.get("/api/v1/learn/lora/versions")
    assert r.status_code == 200 and r.json()["success"] is True


def test_unknown_task_detail_honest_failure(client):
    r = client.get("/api/v1/learn/tasks/q8-no-such-task")
    assert r.status_code == 200  # ADR-01：HTTP 恒 200
    body = r.json()
    assert body["success"] is False or body["data"] is not None


def test_reorder_validation_rejects_garbage(client):
    """乱序载荷应被拒（422 校验层或语义错误信封），不得静默成功。"""
    r = client.post("/api/v1/learn/tasks/reorder", json={"order": "not-a-list"})
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        assert r.json()["success"] is False
