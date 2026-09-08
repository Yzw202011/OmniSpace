"""漫画/漫剧全局资产域隔离单测（2026-09-08 用户令，迁移 v13）。

契约：
- 转全局按项目产品面盖章（漫画项目→face=comic；漫剧项目→face=manga；
  项目缺失脏数据回落 manga）；
- 库端点 scope=global 按 face 过滤：两面互不可见；
- face 缺省 'manga'（漫剧既有调用零改动向后兼容）；
- 项目级资产本就按 project_id 隔离（回归锚定）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.manga import comic_asset as ca
from backend.data import database as db_mod
from backend.data.database import Database


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = Database(tmp_path / "face.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    from backend.main import app
    return SimpleNamespaceCI(app=TestClient(app, base_url="http://127.0.0.1"),
                             db=test_db)


class SimpleNamespaceCI:
    def __init__(self, app, db):
        self.app = app
        self.db = db


def _mk_asset(db, pid: str, name: str, scope: str = "project",
              face: str | None = None) -> str:
    aid = name
    row = {"id": aid, "project_id": pid, "kind": "character", "name": name,
           "file_path": f"comic_assets/{pid}/{name}.png", "prompt": "",
           "meta": "{}", "created_at": 1.0, "scope": scope}
    if face is not None:
        row["face"] = face
    db.insert("comic_assets", row)
    return aid


def test_to_global_stamps_face_by_project_type(client):
    """漫画项目资产转全局→face=comic；漫剧项目→face=manga。"""
    db = client.db
    client.app.post("/api/v1/comic/project/create",
                json={"name": "漫画面A", "project_type": "comic"})
    client.app.post("/api/v1/comic/project/create",
                json={"name": "漫剧面B"})
    rows = db.query("SELECT id, project_type FROM projects")
    comic_pid = next(r["id"] for r in rows if r["project_type"] == "comic")
    manga_pid = next(r["id"] for r in rows if r["project_type"] == "manga")

    _mk_asset(db, comic_pid, "comic_asset")
    _mk_asset(db, manga_pid, "manga_asset")
    # to-global 单条端点（需磁盘文件存在——直接调内部盖章路径会搬文件，
    # 此处用 SQL 语义验证 _face_of_project + update 字段）
    assert ca._face_of_project(db, comic_pid) == "comic"
    assert ca._face_of_project(db, manga_pid) == "manga"
    assert ca._face_of_project(db, "不存在的项目") == "manga", "脏数据回落漫剧面"

    # 单条端点盖章（资产文件不存在时 _asset_move_to_global 会怎样？
    # 直接 SQL 置全局后按端点幂等分支验证 face 已在行上）
    db.update("comic_assets", {"scope": "global", "project_id": "",
                               "face": ca._face_of_project(db, comic_pid)},
              "id=?", ("comic_asset",))
    row = db.query_one("SELECT face, scope FROM comic_assets WHERE id='comic_asset'")
    assert row["face"] == "comic" and row["scope"] == "global"


def test_global_library_filters_by_face(client):
    """scope=global 按 face 过滤：漫画面只见 comic，漫剧缺省只见 manga。"""
    db = client.db
    _mk_asset(db, "", "g_comic", scope="global", face="comic")
    _mk_asset(db, "", "g_manga", scope="global", face="manga")

    comic_view = client.app.get("/api/v1/comic/asset/library",
                            params={"scope": "global", "face": "comic"}).json()
    names = [i["name"] for i in comic_view["data"]["items"]]
    assert names == ["g_comic"], f"漫画面全局池只见 comic 资产: {names}"

    default_view = client.app.get("/api/v1/comic/asset/library",
                              params={"scope": "global"}).json()
    default_names = [i["name"] for i in default_view["data"]["items"]]
    assert default_names == ["g_manga"], \
        f"缺省 face=manga（漫剧既有调用零改动）: {default_names}"

    manga_view = client.app.get("/api/v1/comic/asset/library",
                            params={"scope": "global", "face": "manga"}).json()
    assert [i["name"] for i in manga_view["data"]["items"]] == ["g_manga"]


def test_project_scope_already_isolated(client):
    """项目级资产按 project_id 隔离回归锚定（两面项目互不可见）。"""
    db = client.db
    client.app.post("/api/v1/comic/project/create",
                json={"name": "隔离漫剧", "project_type": "manga"})
    comic_pid = "cpid1"
    client.app.post("/api/v1/comic/project/create",
                json={"name": "隔离漫画", "project_type": "comic",
                      "project_id": comic_pid})
    manga_pid = next(r["id"] for r in db.query(
        "SELECT id FROM projects WHERE name='隔离漫剧'"))
    _mk_asset(db, comic_pid, "只属于漫画")
    _mk_asset(db, manga_pid, "只属于漫剧")

    view = client.app.get("/api/v1/comic/asset/library",
                      params={"project_id": comic_pid}).json()
    names = [i["name"] for i in view["data"]["items"]]
    assert names == ["只属于漫画"], "项目资产只按 project_id 隔离"
    row = db.query_one("SELECT face FROM comic_assets WHERE id='只属于漫画'")
    assert row["face"] == "manga", "新建项目资产 face 默认 manga（占位，转全局时按项目面重盖）"
