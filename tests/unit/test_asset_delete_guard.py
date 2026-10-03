"""资产删除共享目录防连坐单测（2026-10-04 用户令：同名不同描述词资产
合法共存——删一行不得端掉别人还在用的目录；角色/场景/道具全类型）。

背景两起事故：林小曼四视图（项目内同名两行）、别墅区街角大门（全局
转全局链共享目录）——均"删一行全灭"。修=删除端点先查同目录引用行，
有则只删行保文件（响应带 dir_kept），查无引用才 rmtree。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.manga import comic_asset as ca
from src.data import database as db_mod
from src.data.database import Database


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = Database(tmp_path / "asset_delete_guard.db")
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    # 资产目录指到 tmp（防触真实 data/）
    monkeypatch.setattr(ca, "_COMIC_ASSET_DIR", tmp_path / "comic_assets")
    monkeypatch.setattr(ca, "DATA_DIR", tmp_path)
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def _mk_asset(db: Database, aid: str, name: str, subdir: str,
              with_image: bool, project: str = "") -> None:
    # 目录层级 comic_assets/{owner}/{kind}/{name}：项目资产 owner=项目
    # id，全局资产 owner=global（真实落盘约定）
    owner = project if project else "global"
    d = ca._COMIC_ASSET_DIR / owner / subdir / name
    d.mkdir(parents=True, exist_ok=True)
    if with_image:
        (d / "image.png").write_bytes(b"\x89PNG fake")
    db.insert("comic_assets", {
        "id": aid, "project_id": project, "kind": subdir[:-1],
        "name": name,
        "file_path": f"comic_assets/{owner}/{subdir}{name}/image.png"
        if with_image else "",
        "prompt": "p", "meta": "{}", "created_at": 1.0,
        "scope": "project" if project else "global"})


def test_delete_keeps_shared_dir_and_files(client, tmp_path):
    """同名两行（同名不同描述词共存）共享目录：删一行→目录文件保留、
    响应 dir_kept；删最后一行→目录真删。"""
    db = db_mod._db_instance
    _mk_asset(db, "prop_a", "行李箱", "props/", True)
    _mk_asset(db, "prop_b", "行李箱", "props/", True)
    d = ca._COMIC_ASSET_DIR / "global" / "props" / "行李箱"
    assert (d / "image.png").exists()

    r1 = client.delete("/api/v1/comic/asset/prop_a").json()["data"]
    assert r1["deleted"] is True and r1["dir_kept"] is True
    assert r1["shared_rows"] == 1
    assert (d / "image.png").exists(), "共享目录文件必须保留"
    assert db.query_one("SELECT id FROM comic_assets WHERE id='prop_b'")

    r2 = client.delete("/api/v1/comic/asset/prop_b").json()["data"]
    assert r2.get("dir_kept") is not True, "最后一行应真删目录"
    assert not d.exists()


def test_delete_last_reference_removes_dir(client):
    """单行独占目录：删除即清目录（旧行为不受影响）。"""
    db = db_mod._db_instance
    _mk_asset(db, "scene_x", "海边", "scenes/", True)
    d = ca._COMIC_ASSET_DIR / "global" / "scenes" / "海边"
    r = client.delete("/api/v1/comic/asset/scene_x").json()["data"]
    assert r["deleted"] is True and r.get("dir_kept") is not True
    assert not d.exists()


def test_delete_project_row_keeps_global_sibling(client):
    """跨域同名（项目+全局各自目录）互不连坐；同域共享才保。"""
    db = db_mod._db_instance
    _mk_asset(db, "sc_proj", "望云亭", "scenes/", True, project="p1")
    _mk_asset(db, "sc_glob", "望云亭", "scenes/", True)
    proj_dir = ca._COMIC_ASSET_DIR / "p1" / "scenes" / "望云亭"
    glob_dir = ca._COMIC_ASSET_DIR / "global" / "scenes" / "望云亭"
    r = client.delete("/api/v1/comic/asset/sc_proj").json()["data"]
    assert r["deleted"] is True
    assert not proj_dir.exists(), "项目侧独占目录应删"
    assert (glob_dir / "image.png").exists(), "全局同名资产不受影响"

def test_library_hard_delete_cascades_project_copy(client):
    """全部可用删除=真删（2026-10-04 用户裁定）：全局资产带 cascade_
    project 删除 → 本项目同名同类副本一并移除（行+文件），计数回传；
    不带参数=仅删全局行（项目中副本保留=软删语义反面）。"""
    db = db_mod._db_instance
    _mk_asset(db, "g_sum", "夏沐沐", "characters/", True)
    _mk_asset(db, "p_sum", "夏沐沐", "characters/", True, project="p1")
    proj_dir = ca._COMIC_ASSET_DIR / "p1" / "characters" / "夏沐沐"

    # 软删反面：不带级联参数只删全局行
    r0 = client.delete("/api/v1/comic/asset/g_sum").json()["data"]
    assert r0["deleted"] is True and r0["cascade_removed"] == 0
    # （重建全局行再测级联）
    _mk_asset(db, "g_sum", "夏沐沐", "characters/", True)

    r = client.delete(
        "/api/v1/comic/asset/g_sum?cascade_project=p1").json()["data"]
    assert r["deleted"] is True and r["cascade_removed"] == 1
    assert db.query_one(
        "SELECT id FROM comic_assets WHERE id='p_sum'") is None,         "本作品同名副本必须一并移除"
    assert not proj_dir.exists(), "副本目录应清"
    assert db.query_one(
        "SELECT id FROM comic_assets WHERE id='g_sum'") is None
