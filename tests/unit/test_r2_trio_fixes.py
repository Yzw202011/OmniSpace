"""R2 根因系存量三修单测（2026-10-24 终结书移交项执行批）。

- R2-H1：promote-project 端点落地（此前前端两页在调、后端无路由，
  每次离开页面静默 404，资产作用域迁移从未生效）
- R2-H3：大纲重建事务化（此前逐语句 autocommit，中途失败=旧正文章
  已删新树未建=真丢数据）——本测试钉事务回滚保数据 + 源码顺序哨兵
- R2-H5：learning_sessions.ended_at 此前全库零写入（129 行实测全 0）
  ——UPSERT 带列 + 终态写入
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database

_SRC_NOVEL = (Path(__file__).resolve().parents[2]
              / "src" / "services" / "novel_service.py").read_text(
                  encoding="utf-8")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = Database(tmp_path / "t.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    from src.main import app
    ns = SimpleNamespace(app=TestClient(app, base_url="http://127.0.0.1"),
                         db=test_db)
    return ns


# ── R2-H1：promote-project ─────────────────────────────────────

def test_promote_project_endpoint_moves_scope(client):
    db = client.db
    client.app.post("/api/v1/comic/project/create",
                    json={"name": "离页转全局", "project_type": "manga"})
    pid = db.query_one(
        "SELECT id FROM projects WHERE name='离页转全局'")["id"]
    for n in ("角色A", "角色B"):
        db.insert("comic_assets", {
            "id": n, "project_id": pid, "kind": "character", "name": n,
            "file_path": f"comic_assets/{pid}/{n}.png", "prompt": "",
            "meta": "{}", "created_at": 1.0, "scope": "project"})
    r = client.app.post("/api/v1/comic/asset/promote-project",
                        json={"project_id": pid})
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["promoted"] == 2
    rows = db.query("SELECT scope, project_id FROM comic_assets")
    assert all(x["scope"] == "global" and x["project_id"] == ""
               for x in rows)
    # 幂等：再转一次 promoted=0
    r2 = client.app.post("/api/v1/comic/asset/promote-project",
                         json={"project_id": pid})
    assert r2.json()["data"]["promoted"] == 0


def test_promote_project_rejects_unsafe_pid(client):
    r = client.app.post("/api/v1/comic/asset/promote-project",
                        json={"project_id": "../../etc"})
    # 恒 200 信封（ADR-01）：校验拒绝也在 body 级（教训口径：断言必须
    # body 级复核，HTTP 状态码不反映业务失败）
    body = r.json()
    assert body["success"] is False, "路径不安全 project_id 必须被拒绝"


# ── R2-H3：大纲重建事务 ─────────────────────────────────────────

def test_outline_txn_rollback_preserves_old_chapters(tmp_path):
    """删旧+插新在一个事务里：中途失败=整体回滚，旧正文章无损。"""
    db = Database(tmp_path / "novel.db")
    db.insert("novel_projects", {
        "id": "p1", "name": "事务", "genre": "测试", "description": "",
        "style_notes": "", "status": "active", "meta": "{}",
        "created_at": 1.0, "updated_at": 1.0})
    db.insert("novel_chapters", {
        "id": "old1", "project_id": "p1", "outline_id": "",
        "chapter_index": 1, "title": "旧章", "content": "用户写好的正文",
        "summary": "", "word_count": 100, "status": "done",
        "progress": 1.0, "error": "", "meta": "{}",
        "created_at": 1.0, "updated_at": 1.0})

    import sqlite3

    def _failing_txn(conn) -> None:
        conn.execute("DELETE FROM novel_chapters WHERE project_id=?", ("p1",))
        conn.execute(
            "INSERT INTO novel_chapters (id, project_id, 不存在的列)"
            " VALUES ('new1', 'p1', 1)")  # 制造中途失败
    with pytest.raises(sqlite3.OperationalError):
        db.execute_in_transaction(_failing_txn)
    row = db.query_one(
        "SELECT content, status FROM novel_chapters WHERE id='old1'")
    assert row is not None and row["content"] == "用户写好的正文", \
        "事务中途失败必须整体回滚——旧正文丢了=R2-H3 复发"


def test_novel_outline_rebuild_is_transactional_nail():
    """源码哨兵：重建段必须走 execute_in_transaction，DELETE 在事务内。"""
    m = re.search(
        r"def _rebuild_txn.*?execute_in_transaction\(_rebuild_txn\)",
        _SRC_NOVEL, re.S)
    assert m, "大纲重建的事务段丢失（回退为逐语句 autocommit）"
    body = m.group(0)
    assert "DELETE FROM novel_chapters" in body
    assert "DELETE FROM novel_outlines" in body
    assert "INSERT INTO" in body
    # 反向钉：重建区段不得再出现逐行 db.insert（autocommit 旧病）
    assert "db.insert" not in body


# ── R2-H5：ended_at 落库 ────────────────────────────────────────

def test_learning_session_upsert_writes_ended_at(tmp_path, monkeypatch):
    db = Database(tmp_path / "learn.db")
    from src.api.learning import _persist_session_row, ensure_learning_tables
    # 2026-10-25 封闭补桩：ensure_learning_tables 的 _tables_ready 模块级
    # 旗标被更早用例在真库置 True 后恒短路——本库（tmp）从未建表，
    # upsert 即 no such table。重置旗标强制对 tmp 库执行 DDL。
    from src.services import browser_agent_service as _bas
    monkeypatch.setattr(_bas, "_tables_ready", False)
    db_mod._db_instance = db
    try:
        ensure_learning_tables()
        budget = SimpleNamespace(to_dict=lambda: {"pages": 10})
        running = SimpleNamespace(
            session_id="s1", topic_id="t1", goal="学", budget=budget,
            status="running", pages_visited=3, knowledge_extracted=1,
            coverage=0.3, stop_reason="", started_at=100.0,
            ended_at=0.0, created_at=99.0)
        _persist_session_row(running)
        row = db.query_one(
            "SELECT status, ended_at FROM learning_sessions WHERE id='s1'")
        assert row["status"] == "running" and row["ended_at"] == 0
        # 终态：_finalize_session 已置 ended_at（模拟）
        done = SimpleNamespace(
            session_id="s1", topic_id="t1", goal="学", budget=budget,
            status="completed", pages_visited=9, knowledge_extracted=5,
            coverage=0.9, stop_reason="goal_achieved", started_at=100.0,
            ended_at=888.0, created_at=99.0)
        _persist_session_row(done)
        row = db.query_one(
            "SELECT status, ended_at FROM learning_sessions WHERE id='s1'")
        assert row["status"] == "completed"
        assert row["ended_at"] == 888.0, "终态 ended_at 未落库=R2-H5 复发"
    finally:
        db_mod._db_instance = None
