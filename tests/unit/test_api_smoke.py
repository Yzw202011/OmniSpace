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

from src.data import database as db_mod
from src.data.database import Database

pytestmark = pytest.mark.smoke


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离数据库的 TestClient（不触发 lifespan）。"""
    test_db = Database(tmp_path / "smoke.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


# ── 数据库模式：新库即含全部迁移列 ───────────────────────────────

@pytest.mark.schema
def test_fresh_db_has_all_migrated_columns(tmp_path):
    """新库初始化后，全部迁移登记中每个 (表, 列) 必须真实存在。

    拦截场景：往 _SCHEMA 加了列但忘记登记迁移 → 老库升级缺列；
    或登记了迁移但 _SCHEMA 漏建 → 新库缺列（work_mode 事故的镜像）。
    """
    db = Database(tmp_path / "fresh.db")
    conn = db._conn()
    assert db.schema_version(conn) == Database.SCHEMA_VERSION, "新库应为最新版本"
    for table, column, _ddl in db._COLUMN_MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert column in cols, f"新库 {table}.{column} 缺失（work_mode 类事故）"


@pytest.mark.schema
def test_future_schema_version_rejected(tmp_path):
    """库版本高于代码版本时必须拒绝启动（防旧程序写坏新库）。"""
    path = tmp_path / "future.db"
    db = Database(path)
    conn = db._conn()
    conn.execute(f"PRAGMA user_version = {Database.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="高于程序支持版本"):
        Database(path)


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


@pytest.mark.schema
def test_v3_migration_encrypts_legacy_plaintext(tmp_path):
    """v3 数据迁移（要求#36 / RTM A-02）：存量明文一次性加密，解密回读零丢失。"""
    from src.data.crypto import decrypt_text, is_encrypted

    path = tmp_path / "legacy_plain.db"
    Database(path)
    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO dialog_messages (id, session_id, role, content) "
                "VALUES ('m1', 's1', 'user', '存量明文对话')")
    raw.executescript("""
        CREATE TABLE behavior_logs (
            event_id   TEXT PRIMARY KEY,
            event_type TEXT NOT NULL DEFAULT '',
            content    TEXT DEFAULT '',
            context    TEXT DEFAULT '',
            "before"   TEXT DEFAULT '',
            "after"    TEXT DEFAULT '',
            timestamp  REAL NOT NULL DEFAULT 0,
            feature    TEXT DEFAULT ''
        );
        INSERT INTO behavior_logs
            (event_id, event_type, content, context, "before", "after")
        VALUES ('e1', 'edit', '行为内容', '行为上下文', '旧值', '新值');
    """)
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    raw.close()

    db = Database(path)  # 触发 v3 数据迁移
    msg = db.query_one("SELECT content FROM dialog_messages WHERE id='m1'")
    assert msg is not None and is_encrypted(msg["content"]), "存量对话明文应已加密"
    assert decrypt_text(msg["content"]) == "存量明文对话", "加密迁移必须零内容丢失"

    evt = db.query_one('SELECT content, context, "before", "after" '
                       "FROM behavior_logs WHERE event_id='e1'")
    assert evt is not None
    for col in ("content", "context", "before", "after"):
        assert is_encrypted(evt[col]), f"behavior_logs.{col} 应已加密"
    assert decrypt_text(evt["content"]) == "行为内容"
    assert decrypt_text(evt["after"]) == "新值"


@pytest.mark.schema
def test_v3_migration_idempotent_and_skips_encrypted(tmp_path):
    """v3 迁移幂等：已加密行二次执行原样跳过，不重复加密不破坏内容。"""
    from src.data.crypto import decrypt_text, is_encrypted

    path = tmp_path / "idem_enc.db"
    Database(path)
    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO dialog_messages (id, session_id, role, content) "
                "VALUES ('m1', 's1', 'user', '幂等内容')")
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    raw.close()

    db1 = Database(path)
    first = db1.query_one(
        "SELECT content FROM dialog_messages WHERE id='m1'")["content"]
    assert is_encrypted(first)

    conn = db1._conn()
    conn.execute("PRAGMA user_version = 2")  # 回退版本号，强制重跑 v3 迁移
    db2 = Database(path)
    second = db2.query_one(
        "SELECT content FROM dialog_messages WHERE id='m1'")["content"]
    assert second == first, "已加密行重复迁移必须原样跳过"
    assert decrypt_text(second) == "幂等内容"


# ── API 冒烟：关键端点可用且响应信封正确 ─────────────────────────

def test_health_endpoint(client):
    """/health 契约：200 + success 信封 + data.status == healthy。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert body.get("success") is True, f"信封异常: {body}"
    assert body["data"]["status"] == "healthy"
    assert body["data"]["db"] == "ok"


def test_system_version(client):
    resp = client.get("/api/v1/system/version")
    assert resp.status_code == 200
    body = resp.json()
    data = body.get("data", body)
    assert any(k in data for k in ("version", "app_version")), "版本信息缺失"


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


def test_unknown_api_path_no_crash(client):
    """未知 API 路径不得击穿到堆栈：静态挂载(/)提供 SPA fallback 返回
    200 index.html（项目为 Hash 路由，此为现状契约）。断言响应合法且
    不泄露异常细节。"""
    resp = client.get("/api/v1/definitely/not/exist")
    assert resp.status_code in (200, 404)
    assert "Traceback" not in resp.text


# ── TASK-P0-03：上传端点类型闸门（.exe 伪装扩展名拦截）──────────
# ADR-01 统一信封：拒绝 = HTTP 200 + success:false（非 HTTP 4xx）

_EXE_BYTES = (b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"
              b"\xb8\x00\x00\x00\x00\x00\x00\x00@\x00\x00\x00\x00\x00\x00\x00")


def test_upload_exe_disguised_as_jsonl_rejected(client, tmp_path, monkeypatch):
    """/learn/dataset/upload：.exe 改名 .jsonl 必须被拒（P0-03 主场景）。"""
    monkeypatch.setattr("src.api.learn.TRAIN_DATA_DIR", tmp_path)
    resp = client.post(
        "/api/v1/learn/dataset/upload",
        files={"file": ("evil.jsonl", _EXE_BYTES, "application/octet-stream")})
    body = resp.json()
    assert body["success"] is False, f"伪装可执行体未被拦截: {body}"
    assert "可执行" in body["error"]["message"]


def test_upload_exe_disguised_as_pdf_rejected(client):
    """/knowledge/import-document：.exe 改名 .pdf 必须被拒。"""
    resp = client.post(
        "/api/v1/knowledge/import-document",
        files={"file": ("evil.pdf", _EXE_BYTES, "application/pdf")},
        data={"topic": "测试"})
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "UNSUPPORTED_FORMAT"


def test_upload_exe_disguised_as_png_rejected(client):
    """/style/upload：.exe 改名 .png 必须被拒。"""
    resp = client.post(
        "/api/v1/style/upload",
        files={"file": ("evil.png", _EXE_BYTES, "image/png")})
    body = resp.json()
    assert body["success"] is False


def test_upload_normal_jsonl_still_works(client, tmp_path, monkeypatch):
    """闸门不得误伤：正常 UTF-8 JSONL 上传仍成功（P0-03 完成标准）。"""
    monkeypatch.setattr("src.api.learn.TRAIN_DATA_DIR", tmp_path)
    payload = b'{"instruction":"a","input":"","output":"b"}\n'
    resp = client.post(
        "/api/v1/learn/dataset/upload",
        files={"file": ("ok.jsonl", payload, "text/plain")})
    body = resp.json()
    assert body["success"] is True, f"正常数据集被误拦: {body}"
    assert body["data"]["size_bytes"] == len(payload)
# 本项目仅供学习使用，商业授权请+Q 3559331368
