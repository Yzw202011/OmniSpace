"""从本作品移除（软删真身）单测（2026-10-24 用户拍板 A）。

背景事故：10-05 引入改借用（不复制）后，作品清单里是全局原件本尊；
「作品中删除」走真删端点把全局原件删掉（10-24 实测：林澈/成片主角
借用后删除即全局消失，后端日志 16:23 借用→16:23/16:24 DELETE 实锤）。
修=DELETE /comic/asset/{id} 增 remove_from_project 参数：
- 借用件（scope=global ∧ borrowed_for 含本项目）→ 摘借用+清本项目
  行绑定+标关键帧过期，全局行与文件不动；
- 项目自有件 → 维持真删；
- 未借用 → SYSTEM_PARAM_INVALID 拒绝。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.api.manga import comic_asset as ca
from src.data import database as db_mod
from src.data.database import Database


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = Database(tmp_path / "asset_unborrow.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    monkeypatch.setattr(ca, "_COMIC_ASSET_DIR", tmp_path / "comic_assets")
    monkeypatch.setattr(ca, "DATA_DIR", tmp_path)
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def _mk_global_borrowed(db: Database, aid: str = "g1",
                        borrowed: list[str] | None = None) -> None:
    d = ca._COMIC_ASSET_DIR / "global" / "characters" / "林澈"
    d.mkdir(parents=True, exist_ok=True)
    (d / "portrait.png").write_bytes(b"\x89PNG fake")
    meta = {"borrowed_for": borrowed or []}
    db.insert("comic_assets", {
        "id": aid, "project_id": "", "kind": "character", "name": "林澈",
        "file_path": "comic_assets/global/characters/林澈/portrait.png",
        "prompt": "p", "meta": json.dumps(meta), "created_at": 1.0,
        "scope": "global"})


def _mk_project_with_row(db: Database, pid: str, rid: str,
                         bind: list[str]) -> None:
    db.insert("projects", {"id": pid, "name": f"p_{pid}", "path": "",
                           "created_at": 1.0, "updated_at": 1.0})
    db.insert("storyboards", {"id": f"sb_{pid}", "project_id": pid,
                              "name": "s", "created_at": 1.0,
                              "updated_at": 1.0})
    db.insert("storyboard_rows", {
        "id": rid, "storyboard_id": f"sb_{pid}", "shot_number": 1,
        "description": "d", "asset_id": bind[0] if bind else "",
        "asset_ids": json.dumps(bind), "sort_index": 1,
        "generation_status": "pending"})


def test_unborrow_keeps_global_and_strips_bindings(client):
    """借用件从作品移除：全局行/文件保留、borrowed_for 只摘本项目、
    本项目行绑定清空（他项目不动）、本行关键帧标过期。"""
    db = db_mod._db_instance
    _mk_global_borrowed(db, borrowed=["p1", "p2"])
    _mk_project_with_row(db, "p1", "r1", ["g1"])
    db.insert("keyframes", {"id": "kf1", "row_id": "r1", "project_id": "p1",
                            "version": 1, "is_current": 1, "status": "done",
                            "file_path": "", "created_at": 1.0})
    _mk_project_with_row(db, "p2", "r2", ["g1"])  # 另一借用方——不得牵连

    r = client.delete(
        "/api/v1/comic/asset/g1?remove_from_project=p1").json()["data"]
    assert r["unborrowed"] is True and r["deleted"] is False
    assert db.query_one("SELECT id FROM comic_assets WHERE id='g1'")
    assert (ca._COMIC_ASSET_DIR / "global" / "characters" / "林澈"
            / "portrait.png").exists()
    meta = json.loads(db.query_one(
        "SELECT meta FROM comic_assets WHERE id='g1'")["meta"])
    assert meta["borrowed_for"] == ["p2"]
    row1 = db.query_one(
        "SELECT asset_id, asset_ids FROM storyboard_rows WHERE id='r1'")
    assert row1["asset_id"] == "" and json.loads(row1["asset_ids"]) == []
    row2 = db.query_one(
        "SELECT asset_id, asset_ids FROM storyboard_rows WHERE id='r2'")
    assert json.loads(row2["asset_ids"]) == ["g1"]
    kf = db.query_one("SELECT stale_reason FROM keyframes WHERE id='kf1'")
    assert kf["stale_reason"] == "asset_changed"


def test_remove_project_owned_still_hard_deletes(client):
    """项目自有件从作品移除=真删（自有件无库副本，行为不变）。"""
    db = db_mod._db_instance
    d = ca._COMIC_ASSET_DIR / "p1" / "characters" / "阿哲"
    d.mkdir(parents=True, exist_ok=True)
    (d / "portrait.png").write_bytes(b"\x89PNG fake")
    db.insert("comic_assets", {
        "id": "own1", "project_id": "p1", "kind": "character",
        "name": "阿哲",
        "file_path": "comic_assets/p1/characters/阿哲/portrait.png",
        "prompt": "p", "meta": "{}", "created_at": 1.0, "scope": "project"})
    r = client.delete(
        "/api/v1/comic/asset/own1?remove_from_project=p1").json()["data"]
    assert r["deleted"] is True and not r.get("unborrowed")
    assert not db.query_one("SELECT id FROM comic_assets WHERE id='own1'")
    assert not d.exists()


def test_remove_not_borrowed_rejected(client):
    """全局件未被该项目借用：参数错拒绝，行不删。"""
    db = db_mod._db_instance
    _mk_global_borrowed(db, aid="g2", borrowed=[])
    body = client.delete(
        "/api/v1/comic/asset/g2?remove_from_project=p9").json()
    assert body["success"] is False
    assert body["error"]["code"] == "SYSTEM_PARAM_INVALID"
    assert db.query_one("SELECT id FROM comic_assets WHERE id='g2'")
