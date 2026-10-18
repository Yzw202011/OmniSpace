# 本项目仅供学习使用，商业授权请+Q 3559331368
"""F-6（2026-10-19）主题删除同事务摘除知识主题标签。

背景：删除主题曾只删 learning_topics 行，knowledge_meta.topic 留孤儿
（「UAT图片知识」「漫剧剧本写作技巧」幽灵主题混入 topic-compare 实测）。
修复后：删主题 ⟺ 摘标签原子完成；知识本身留库；无 knowledge_meta 表
的新库删除不炸。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "f6.db"))
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def _ensure_kmeta(db: Database) -> None:
    db.execute_in_transaction(lambda conn: conn.execute(
        "CREATE TABLE IF NOT EXISTS knowledge_meta ("
        " id TEXT PRIMARY KEY, content TEXT DEFAULT '',"
        " type TEXT DEFAULT 'concept', topic TEXT DEFAULT '',"
        " created_at REAL DEFAULT 0)"))


def test_delete_topic_untags_knowledge(client):
    r = client.post("/api/v1/learn/topic/create",
                    json={"name": "T-F6 主题"})
    assert r.json()["success"] is True
    tid = r.json()["data"]["id"]
    db = db_mod._db_instance
    _ensure_kmeta(db)
    db.execute_in_transaction(lambda conn: conn.execute(
        "INSERT INTO knowledge_meta (id, content, topic) VALUES"
        " ('k-f6-1', '占位', 'T-F6 主题')"))
    # 同名主题 + 一条孤儿同topics名的知识
    r2 = client.request("DELETE", "/api/v1/learn/topic/delete",
                        params={"id": tid})
    assert r2.json()["success"] is True
    row = db.query_one("SELECT topic FROM knowledge_meta WHERE id='k-f6-1'")
    assert row is not None, "知识本身必须留库"
    assert (row["topic"] or "") == "", "主题标签必须被摘除"
    assert db.count("learning_topics", "id=?", (tid,)) == 0


def test_delete_topic_without_kmeta_table(client):
    """新库（无 knowledge_meta 表）删除不炸——F-6 守卫路径。"""
    r = client.post("/api/v1/learn/topic/create",
                    json={"name": "T-F6 裸库主题"})
    tid = r.json()["data"]["id"]
    r2 = client.request("DELETE", "/api/v1/learn/topic/delete",
                        params={"id": tid})
    assert r2.json()["success"] is True


def test_delete_topic_atomic_on_missing(client):
    r = client.request("DELETE", "/api/v1/learn/topic/delete",
                       params={"id": "no-such-id"})
    assert r.json()["success"] is False
    assert r.json()["error"]["code"] == "SYSTEM_RESOURCE_NOT_FOUND"
