"""本地算力 · 省钱账本（2026-09-08 用户拍板：保守口径只算 AI 输出侧）

覆盖 GET /system/local-savings：
  ① 空库全零、金额 0；
  ② 本地助手输出计入（content+reasoning 字数 ×0.75 折 tokens），
     user 消息不计；
  ③ 云端排除哨兵：dialog model_used cloud:: 前缀 / comic_assets meta
     cloud / video model_used cloud: 前缀的行全部不计——云端部分是
     真实开销，算省钱就是虚报；
  ④ 无产物路径（file_path 空）的行不计；
  ⑤ 金额对账：tokens×文本参考价/百万 + 图张数×单价 + 视频条数×单价
     与响应 cny_est 逐位一致；
  ⑥ 响应带 scope_note 与 prices（前端要明标估算/参考价）。

全部 TestClient 离线跑（tmp 库隔离，不触发 lifespan/GPU）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.data import database as db_mod
from backend.data.database import Database

pytestmark = pytest.mark.smoke


@pytest.fixture()
def client(tmp_path, monkeypatch):
    test_db = Database(tmp_path / "savings.db")
    # 懒建表补齐（keyframes/comic_assets/paint_history/video_tasks 由
    # 各功能模块首次使用时创建；测试直接插行须先建最小结构）
    test_db.executescript("""
    CREATE TABLE IF NOT EXISTS keyframes (
      id TEXT PRIMARY KEY, row_id TEXT, project_id TEXT, file_path TEXT,
      status TEXT, meta TEXT);
    CREATE TABLE IF NOT EXISTS comic_assets (
      id TEXT PRIMARY KEY, project_id TEXT, kind TEXT, file_path TEXT,
      meta TEXT);
    CREATE TABLE IF NOT EXISTS paint_history (
      task_id TEXT PRIMARY KEY, prompt TEXT, file_path TEXT,
      created_at REAL);
    CREATE TABLE IF NOT EXISTS video_tasks (
      id TEXT PRIMARY KEY, storyboard_row_id TEXT, file_path TEXT,
      model_used TEXT, status TEXT);
    """)
    monkeypatch.setattr(db_mod, "_db_instance", test_db)
    from backend.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def _msg(db: Any, role: str, content: str, model_used: str = "", reasoning: str = ""):
    if not db.query_one("SELECT id FROM dialog_sessions WHERE id='s1'"):
        db.insert("dialog_sessions", {"id": "s1", "title": "测试"})
    db.insert("dialog_messages", {
        "session_id": "s1", "role": role, "content": content,
        "model_used": model_used, "reasoning": reasoning,
        "timestamp": 1.0,
    })


def test_empty_db_all_zero(client):
    r = client.get("/api/v1/system/local-savings")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["text"]["messages"] == 0
    assert d["text"]["tokens_est"] == 0
    assert d["images"]["count"] == 0
    assert d["videos"]["count"] == 0
    assert d["money"]["cny_est"] == 0.0


def test_local_text_counted_user_and_cloud_excluded(client):
    db = db_mod._db_instance
    assert db is not None  # _db_instance fixture 已注入
    _msg(db, "assistant", "字" * 100, "qwen3-vl-4b", reasoning="考" * 20)
    _msg(db, "assistant", "词" * 50, "")                     # 早期无引擎记录
    _msg(db, "user", "问" * 999)                              # 用户输入不计
    _msg(db, "assistant", "云" * 999, "cloud::prov::m")       # 云端=花钱，排除

    d = client.get("/api/v1/system/local-savings").json()["data"]
    t = d["text"]
    assert t["messages"] == 2
    assert t["chars"] == 170
    assert t["tokens_est"] == int(170 * 0.75)  # 127


def test_images_videos_counted_cloud_markers_excluded(client):
    db = db_mod._db_instance
    assert db is not None  # _db_instance fixture 已注入
    db.insert("keyframes", {"row_id": "r1", "project_id": "p1",
                            "file_path": "a.png", "status": "done"})
    db.insert("keyframes", {"row_id": "r2", "project_id": "p1",
                            "file_path": "", "status": "error"})
    db.insert("comic_assets", {"project_id": "p1", "kind": "character",
                               "file_path": "b.png", "meta": "{}"})
    db.insert("comic_assets", {"project_id": "p1", "kind": "character",
                               "file_path": "c.png",
                               "meta": '{"engine": "cloud"}'})
    db.insert("paint_history", {"task_id": "t1", "prompt": "x",
                                "file_path": "d.png", "created_at": 1.0})
    db.insert("video_tasks", {"storyboard_row_id": "r1", "status": "done",
                              "file_path": "v.mp4", "model_used": "h3"})
    db.insert("video_tasks", {"storyboard_row_id": "r2", "status": "done",
                              "file_path": "v2.mp4",
                              "model_used": "cloud:prov:m"})

    d = client.get("/api/v1/system/local-savings").json()["data"]
    img = d["images"]
    # keyframes 有效 1 + comic_assets 有效 1（cloud 扣除）+ paint 1
    assert img == {"count": 3, "keyframes": 1, "comic_assets": 1, "paint": 1}
    assert d["videos"]["count"] == 1


def test_money_reconciles_exactly(client):
    db = db_mod._db_instance
    assert db is not None  # _db_instance fixture 已注入
    _msg(db, "assistant", "甲" * 1_000)          # 750 tokens
    for i in range(3):
        db.insert("paint_history", {"task_id": f"t{i}", "prompt": "x",
                                    "file_path": f"p{i}.png",
                                    "created_at": 1.0})
    db.insert("video_tasks", {"storyboard_row_id": "r", "status": "done",
                              "file_path": "v.mp4", "model_used": "h3"})

    d = client.get("/api/v1/system/local-savings").json()["data"]
    expect = (750 / 1_000_000 * 8.0) + 3 * 0.2 + 1 * 1.5
    assert d["money"]["cny_est"] == round(expect, 2)


def test_response_shape_notes_prices(client):
    d = client.get("/api/v1/system/local-savings").json()["data"]
    assert "保守口径" in d["scope_note"]
    p = d["money"]["prices"]
    assert set(p) == {"text_cny_per_mtok", "image_cny_each", "video_cny_each"}
