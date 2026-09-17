"""写作台插件技能测试（技能插座批2，2026-09-17）。

覆盖：/novel/chapter/{id}/skill 端点（参数校验/章不存在/空正文拒/
技能不存在/真插件往返建议产出）——纯 CPU 不触 GPU，不写章表。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database
from src.services.plugin_runtime import registry as pr_registry
from src.services.plugin_runtime.registry import get_plugin_runtime

NOVEL_SKILL_PLUGIN_PY = '''
from typing import Any

try:
    from .plugin import ExpertPlugin
except ImportError:
    from omnispace.plugin import ExpertPlugin


class NovelSkillPlugin(ExpertPlugin):
    BASE_MODEL = "novel.skill"
    CAPABILITY = "novel skill test"

    def on_think(self, event, ctx):
        data = event.get("data") or {}
        return {"text": "整理稿：\\n" + str(data.get("text", ""))[:50]}
'''

NOVEL_SKILL = {"id": "tidy", "feature": "novel", "title": "全章整理",
               "description": "整理章节行文", "input": "text"}


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setattr(pr_registry, "USER_PLUGIN_DIR",
                        tmp_path / "imported")
    monkeypatch.setattr(pr_registry, "USER_REGISTRY_PATH",
                        tmp_path / "user_registry.json")
    monkeypatch.setattr(pr_registry, "FACTORY_OVERRIDE_PATH",
                        tmp_path / "factory_overrides.json")
    monkeypatch.setattr(pr_registry, "OUTPUT_ROOT", tmp_path / "plug_out")
    monkeypatch.setattr(pr_registry, "_runtime", None)
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    db = db_mod._db_instance
    db.insert("novel_projects", {
        "id": "p1", "name": "测试书", "genre": "都市",
        "description": "", "style_notes": "", "status": "active",
        "meta": None, "created_at": 1.0, "updated_at": 1.0})
    db.insert("novel_chapters", {
        "id": "c1", "project_id": "p1", "outline_id": "",
        "chapter_index": 1, "title": "第一章", "content": "风雨交加的夜里，门被敲响了。",
        "word_count": 14, "status": "done",
        "created_at": 1.0, "updated_at": 1.0})
    db.insert("novel_chapters", {
        "id": "c2", "project_id": "p1", "outline_id": "",
        "chapter_index": 2, "title": "空章", "content": "",
        "word_count": 0, "status": "pending",
        "created_at": 1.0, "updated_at": 1.0})
    from src.main import app
    src = tmp_path / "novel_skill_plugin.py"
    src.write_text(NOVEL_SKILL_PLUGIN_PY, encoding="utf-8")
    rt = get_plugin_runtime()
    rt.register("novel-demo", src, trust="user_data", skills=[NOVEL_SKILL])
    return TestClient(app, base_url="http://127.0.0.1")


def _skill(client: TestClient, cid: str, payload: dict) -> dict:
    return client.post(f"/api/v1/novel/chapter/{cid}/skill",
                       json=payload).json()


def test_skill_requires_params(api_client):
    body = _skill(api_client, "c1", {})
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SPEC_MISMATCH"


def test_skill_chapter_not_found(api_client):
    body = _skill(api_client, "ghost", {"plugin": "novel-demo",
                                        "skill_id": "tidy"})
    assert body["success"] is False
    assert body["error"]["code"] == "NOVEL_CHAPTER_NOT_FOUND"


def test_skill_empty_chapter_rejected(api_client):
    body = _skill(api_client, "c2", {"plugin": "novel-demo",
                                     "skill_id": "tidy"})
    assert body["success"] is False
    assert body["error"]["code"] == "NOVEL_CHAPTER_EMPTY"


def test_skill_not_found(api_client):
    body = _skill(api_client, "c1", {"plugin": "novel-demo",
                                     "skill_id": "nope"})
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SKILL_NOT_FOUND"


def test_skill_roundtrip_suggestion_no_write(api_client):
    body = _skill(api_client, "c1", {"plugin": "novel-demo",
                                     "skill_id": "tidy"})
    assert body["success"] is True, body
    data = body["data"]
    assert data["suggestion"].startswith("整理稿：")
    assert data["original_chars"] > 0
    assert 0.0 <= data["similarity"] <= 1.0
    # 不写库：章正文保持原文
    db = db_mod._db_instance
    row = db.query_one("SELECT content FROM novel_chapters WHERE id='c1'")
    assert row["content"] == "风雨交加的夜里，门被敲响了。"
