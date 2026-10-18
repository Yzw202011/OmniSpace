"""训练中心缺口修复回归（2026-10-18，测试计划 G-01/G-06）。

- G-06：D3 维护期 /learn/train 与 /style/train API 与 UI 同闸
  （TRAINING_MAINTENANCE）；解冻路径放行到原有参数校验链。
- G-01：train_tasks.error 列存在（新库 DDL + 存量库迁移 v15）、
  失败原因经 _row_to_train_task 透出到 /learn/tasks。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database, get_db_safe
from src.services import training_common


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


# ── G-06：维护闸 ────────────────────────────────────────────

def test_learn_train_sealed_in_maintenance(api_client, monkeypatch):
    monkeypatch.setattr(training_common, "KNOWLEDGE_TRAINING_MAINTENANCE",
                        True)
    r = api_client.post("/api/v1/learn/train",
                        json={"base_model": "qwen3-vl-4b"})
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "TRAINING_MAINTENANCE"


def test_style_train_sealed_in_maintenance(api_client, monkeypatch):
    monkeypatch.setattr(training_common, "STYLE_TRAINING_MAINTENANCE", True)
    r = api_client.post("/api/v1/style/train", json={"dataset_id": "x"})
    assert r.json()["error"]["code"] == "TRAINING_MAINTENANCE"


def test_learn_train_unsealed_falls_through(api_client, monkeypatch):
    """解冻路径：闸放行后落到原有校验链（此处缺 base_model 被拒）。"""
    monkeypatch.setattr(training_common, "KNOWLEDGE_TRAINING_MAINTENANCE",
                        False)
    r = api_client.post("/api/v1/learn/train", json={})
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] != "TRAINING_MAINTENANCE"


def test_style_train_unsealed_falls_through(api_client, monkeypatch):
    monkeypatch.setattr(training_common, "STYLE_TRAINING_MAINTENANCE", False)
    r = api_client.post("/api/v1/style/train", json={})
    assert r.json()["error"]["code"] == "SYSTEM_PARAM_INVALID"


# ── G-01：error 列与透出 ────────────────────────────────────

def test_fresh_db_has_error_column(tmp_path):
    db = Database(tmp_path / "fresh.db")
    cols = {r["name"] for r in db.query("PRAGMA table_info(train_tasks)")}
    assert "error" in cols


def test_legacy_db_migration_adds_error_column(tmp_path):
    """存量库（user_version=14，无 error 列）打开后自动补列并推进版本。"""
    p = tmp_path / "legacy.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE train_tasks ("
        " id TEXT PRIMARY KEY, base_model TEXT NOT NULL,"
        " lora_rank INTEGER DEFAULT 16, lora_alpha INTEGER DEFAULT 32,"
        " learning_rate REAL DEFAULT 1e-4, epochs INTEGER DEFAULT 3,"
        " dataset_path TEXT NOT NULL, status TEXT DEFAULT 'queued',"
        " progress REAL DEFAULT 0.0, priority INTEGER DEFAULT 0,"
        " created_at REAL NOT NULL DEFAULT 0,"
        " updated_at REAL NOT NULL DEFAULT 0)")
    conn.execute("PRAGMA user_version = 14")
    conn.commit()
    conn.close()
    db = Database(p)
    cols = {r["name"] for r in db.query("PRAGMA table_info(train_tasks)")}
    assert "error" in cols
    assert db.schema_version() == Database.SCHEMA_VERSION


def test_error_persists_and_flows_through_api(api_client):
    from src.api.learn import _row_to_train_task
    from src.services.lora_training_service import _TRAIN_TASK_COLUMNS

    assert "error" in _TRAIN_TASK_COLUMNS
    db = get_db_safe()
    assert db is not None
    db.insert("train_tasks", {
        "id": "t-err-1", "base_model": "models/qwen3-vl-4b",
        "dataset_path": "data/training/x.jsonl", "status": "error",
        "progress": 0.0, "error": "基座模型不存在或权重未下载完成",
        "created_at": 1.0, "updated_at": 1.0,
    })
    row = db.query_one("SELECT * FROM train_tasks WHERE id='t-err-1'")
    assert row is not None
    assert _row_to_train_task(row)["error"] == "基座模型不存在或权重未下载完成"

    r = api_client.get("/api/v1/learn/tasks")
    items = r.json()["data"]["items"]
    target = next(i for i in items if i["id"] == "t-err-1")
    assert target["error"] == "基座模型不存在或权重未下载完成"
    # 批1 UI 测试抓缺（2026-10-19）：created_at 必须透出——此前被
    # _row_to_train_task 丢弃，前端盖 Date.now() 致「创建: 刚刚」假象
    assert target["created_at"] == 1.0
    assert target["updated_at"] == 1.0
    # G-05：priority 透出（徽章真源）
    assert target["priority"] == 0


def test_learn_models_ready_flag_matches_disk(api_client, tmp_path,
                                              monkeypatch):
    """G-09：/learn/models 必须下发与服务层准入同口径的 ready 盘检——
    此前从不下发 ready，前端「（未就绪）置灰」永不生效。"""
    from src.config import ROOT_DIR
    real_models = ROOT_DIR / "models"
    ready_ids = {m["id"] for m in
                 (api_client.get("/api/v1/learn/models")
                  .json()["data"]["items"]) if m["ready"]}
    # 磁盘复核：ready 的必在盘且有权重/索引；不在盘的必不 ready
    for mid in ("qwen3-vl-4b", "qwen2-vl-2b", "sdxl-base-1.0"):
        on_disk = False
        for cand in (real_models / mid, real_models / "paint" / mid):
            if cand.is_dir() and (
                any(cand.glob("*.safetensors"))
                or any(cand.glob("*.bin"))
                or any(cand.glob("*.gguf"))
                or (cand / "model.safetensors.index.json").is_file()):
                on_disk = True
                break
        assert (mid in ready_ids) == on_disk, (
            f"{mid}: ready={mid in ready_ids} 与磁盘实测 {on_disk} 不一致")
