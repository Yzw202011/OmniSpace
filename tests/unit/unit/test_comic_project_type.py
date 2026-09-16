"""漫画模块 M1（2026-09-07）：projects.project_type 产品面区分契约。

漫剧库与漫画页共用全部生成底座（分镜/资产/关键帧链），只靠
project_type 区分前端入口。契约四条：
- create 必传 project_type（白名单 manga/comic；缺省拒绝——2026-09-10 收口，
  此前缺省默认 manga 曾致漫画项目静默落漫剧面）；
- list ?type= 按产品面过滤，缺省不过滤（兼容漫剧库旧调用）；
- 旧库迁移：v8 库打开后自动补列，存量项目归漫剧面；
- update 可随行更新 art_style（漫画页「换风格重生成」的数据面，
  关键帧生成经 _project_style_pack 实时读取项目画风）。

tmp 库隔离，不触发 lifespan，绝不触碰 data/omnispace.db。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = Database(tmp_path / "comic_type.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def test_create_missing_type_rejected(client):
    """缺省 project_type 拒绝：不再静默落漫剧面（2026-09-10 收口）。"""
    r = client.post("/api/v1/comic/project/create", json={"name": "缺类型"})
    assert r.status_code == 422 or r.json().get("success") is False
    # 确认未落库（拒绝而非默认建库）
    items = client.get("/api/v1/comic/project/list").json()["data"]["items"]
    assert items == [], f"缺类型不应创建项目: {items}"


def test_comic_type_filter(client):
    """?type= 产品面过滤：漫画页与漫剧库互不可见，缺省全量。"""
    client.post("/api/v1/comic/project/create",
                json={"name": "漫画A", "project_type": "comic"})
    client.post("/api/v1/comic/project/create",
                json={"name": "漫剧B", "project_type": "manga"})

    r_comic = client.get("/api/v1/comic/project/list", params={"type": "comic"})
    names_comic = [it["name"] for it in r_comic.json()["data"]["items"]]
    assert names_comic == ["漫画A"], f"漫画面应只见漫画项目: {names_comic}"
    assert r_comic.json()["data"]["items"][0]["project_type"] == "comic"

    r_manga = client.get("/api/v1/comic/project/list", params={"type": "manga"})
    names_manga = [it["name"] for it in r_manga.json()["data"]["items"]]
    assert names_manga == ["漫剧B"], f"漫剧面应只见漫剧项目: {names_manga}"

    r_all = client.get("/api/v1/comic/project/list")
    assert r_all.json()["data"]["total"] == 2, "缺省不过滤=全部（兼容旧调用）"


def test_invalid_type_rejected(client):
    """白名单外 project_type 拒绝（防脏值进库后两个入口都看不见）。"""
    r = client.post("/api/v1/comic/project/create",
                    json={"name": "脏值", "project_type": "game"})
    assert r.status_code == 422 or r.json().get("success") is False


def test_update_art_style(client):
    """PUT 随行更新 art_style；不传则保持（漫画页换风格重生成的数据面）。"""
    r = client.post("/api/v1/comic/project/create",
                    json={"name": "换风格", "project_type": "comic",
                          "art_style": "anime"})
    pid = r.json()["data"]["project_id"]

    ru = client.put(f"/api/v1/comic/project/{pid}",
                    json={"name": "换风格", "art_style": "guofeng"})
    assert ru.json()["success"]
    items = client.get("/api/v1/comic/project/list",
                       params={"type": "comic"}).json()["data"]["items"]
    assert items[0]["art_style"] == "guofeng"

    # 只改名不带 art_style：画风保持不丢
    client.put(f"/api/v1/comic/project/{pid}", json={"name": "换风格2"})
    items = client.get("/api/v1/comic/project/list",
                       params={"type": "comic"}).json()["data"]["items"]
    assert items[0]["name"] == "换风格2"
    assert items[0]["art_style"] == "guofeng", "不传 art_style 不应清空画风"


def test_v8_library_migrates_project_type(tmp_path):
    """旧库（user_version=8）打开即补列，存量项目归漫剧面。"""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 8")
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " path TEXT DEFAULT '', work_mode TEXT NOT NULL DEFAULT 'regular',"
        " art_style TEXT DEFAULT '', created_at REAL NOT NULL DEFAULT 0,"
        " updated_at REAL NOT NULL DEFAULT 0)")
    conn.execute(
        "INSERT INTO projects (id, name, created_at, updated_at)"
        " VALUES ('p1', '存量项目', 1, 1)")
    conn.commit()
    conn.close()

    db = Database(db_path)  # 打开即触发 _init_schema → v8→v9 补列
    row = db.query_one("SELECT project_type FROM projects WHERE id='p1'")
    assert row is not None and row["project_type"] == "manga", "存量项目应归漫剧面"
    assert db.schema_version() == db.SCHEMA_VERSION
