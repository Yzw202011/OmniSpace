# -*- coding: utf-8 -*-
"""API 冒烟测试 + 数据库模式测试。

目标：以最低成本拦截两类历史事故——
1. 端点 404 / 字段缺失（硬件 API disk.total_gb 事故）
2. 存量库缺列（projects.work_mode 事故，2026-08-14）

隔离策略：所有用例使用 tmp_path 临时数据库，不触碰 data/omnispace.db；
TestClient 不进入 with 块，避免触发 lifespan（嵌入模型 CUDA 预加载）。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.data import database as db_mod
from backend.data.database import Database

pytestmark = pytest.mark.smoke


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离数据库的 TestClient（不触发 lifespan）。"""
    test_db = Database(tmp_path / "smoke.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    from backend.main import app
    return TestClient(app, base_url="http://127.0.0.1")


# ── 数据库模式：新库即含全部迁移列 ───────────────────────────────

@pytest.mark.schema
def test_fresh_db_has_all_migrated_columns(tmp_path):
    """新库初始化后，_COLUMN_MIGRATIONS 中每个 (表, 列) 必须真实存在。

    拦截场景：往 _SCHEMA 加了列但忘记登记迁移 → 老库升级缺列；
    或登记了迁移但 _SCHEMA 漏建 → 新库缺列（work_mode 事故的镜像）。
    """
    db = Database(tmp_path / "fresh.db")
    conn = db._conn()
    for table, column, _ddl in Database._COLUMN_MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert column in cols, f"新库 {table}.{column} 缺失（work_mode 类事故）"


@pytest.mark.schema
def test_legacy_db_upgrade_adds_columns(tmp_path):
    """模拟 work_mode 事故现场：旧 schema 无新列，初始化后必须补齐。"""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript("""
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        INSERT INTO projects (name) VALUES ('旧项目');
    """)
    conn.commit()
    conn.close()

    db = Database(legacy)
    row = db.query_one("SELECT * FROM projects WHERE name = '旧项目'")
    assert row is not None
    assert row.get("work_mode") == "regular", "存量项目应默认 regular（向后兼容）"


@pytest.mark.schema
def test_migration_is_idempotent(tmp_path):
    """重复初始化同一库，迁移必须幂等不报错。"""
    path = tmp_path / "idem.db"
    Database(path)
    Database(path)


# ── API 冒烟：关键端点可用且响应信封正确 ─────────────────────────

def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "healthy"


def test_system_version(client):
    resp = client.get("/api/v1/system/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert "version" in body["data"]


def test_hardware_info_fields(client):
    """拦截 disk.total_gb / ram.total_mb 字段缺失导致前端崩溃的事故。"""
    resp = client.get("/api/v1/hardware/info")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "disk" in data and "ram" in data
    if data.get("disk"):
        assert "total_gb" in data["disk"], "disk.total_gb 缺失（前端 .toFixed 崩溃）"
    if data.get("ram"):
        assert "total_mb" in data["ram"], "ram.total_mb 缺失（前端 .toFixed 崩溃）"


def test_unknown_api_returns_envelope_404(client):
    """未知 API 路径必须返回标准信封错误，而非裸 404 HTML。"""
    resp = client.get("/api/v1/definitely/not/exist")
    assert resp.status_code == 404
    body = resp.json()
    assert "code" in body and body["code"] != 0
