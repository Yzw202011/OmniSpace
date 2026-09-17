"""训练中心聚合 API 测试（P1，2026-09-17 用户拍板 2A）。

覆盖：两源合并与归一化 / created_at 倒序（缺失沉底）/ 单源 fail-soft
（一侧炸不拖垮整队，sources 如实披露）/ 空队。两源函数以 monkeypatch
桩替（聚合逻辑是被测对象，源查询各有自己的测试）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database


def _envelope(items):
    return {"success": True, "data": {"items": items, "total": len(items)}}


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def test_merged_and_sorted(api_client, monkeypatch):
    from src.api import training as tr

    monkeypatch.setattr(tr, "character_lora_tasks", lambda: _envelope([]))
    monkeypatch.setattr(tr, "learn_tasks", lambda: _envelope([
        {"id": "k1", "status": "running", "progress": 40,
         "created_at": 200.0, "base_model": "qwen3-vl-4b"}]))
    monkeypatch.setattr(tr, "style_tasks", lambda: _envelope([
        {"id": "s1", "name": "国风水墨", "status": "queued", "progress": 0,
         "created_at": 300.0},
        {"id": "s2", "name": "赛博", "status": "done", "progress": 100,
         "created_at": None}]))
    r = api_client.get("/api/v1/training/tasks")
    body = r.json()
    assert body["success"] is True, body
    items = body["data"]["items"]
    # 倒序 + None 沉底
    assert [i["id"] for i in items] == ["s1", "k1", "s2"]
    # 归一化字段
    assert items[0]["kind"] == "style" and items[0]["kind_label"] == "风格训练"
    assert items[1]["kind_label"] == "知识训练"
    assert items[1]["name"] == "qwen3-vl-4b"  # 无 name/style_prompt 回落 base_model
    assert items[0]["progress"] == 0.0 and items[1]["progress"] == 40.0
    assert body["data"]["sources"] == {
        "knowledge": {"ok": True, "count": 1, "error": None},
        "style": {"ok": True, "count": 2, "error": None},
        "character": {"ok": True, "count": 0, "error": None}}


def test_fail_soft_one_source_down(api_client, monkeypatch):
    from src.api import training as tr
    from src.middleware.error_handler import ApiError

    monkeypatch.setattr(tr, "character_lora_tasks", lambda: _envelope([]))
    monkeypatch.setattr(tr, "learn_tasks", lambda: _envelope([
        {"id": "k9", "status": "pending", "created_at": 1.0}]))
    def _boom():
        raise ApiError(80014, "风格源故障")
    monkeypatch.setattr(tr, "style_tasks", _boom)

    r = api_client.get("/api/v1/training/tasks")
    body = r.json()
    assert body["success"] is True, body
    items = body["data"]["items"]
    assert [i["id"] for i in items] == ["k9"]  # 好的一侧照常出数
    src = body["data"]["sources"]
    assert src["style"]["ok"] is False and "风格源故障" in src["style"]["error"]
    assert src["knowledge"]["ok"] is True


def test_empty_queue(api_client, monkeypatch):
    from src.api import training as tr

    monkeypatch.setattr(tr, "learn_tasks", lambda: _envelope([]))
    monkeypatch.setattr(tr, "style_tasks", lambda: _envelope([]))
    monkeypatch.setattr(tr, "character_lora_tasks", lambda: _envelope([]))
    r = api_client.get("/api/v1/training/tasks")
    body = r.json()
    assert body["success"] is True
    assert body["data"]["items"] == [] and body["data"]["total"] == 0
