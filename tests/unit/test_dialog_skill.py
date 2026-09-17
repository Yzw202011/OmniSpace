"""对话插件技能测试（技能插座批1，2026-09-17）。

覆盖：/dialog/skill 端点（参数校验/技能不存在/真插件往返）、
plugin_context 解析闸（非列表拒/条数上限/8K 截断/空文本跳过/标题缺省）。
纯 CPU 不触 GPU，不进 SSE 主路径（主路径注入块由代码审读+后续真浏览器
总验收覆盖）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.dialog import _parse_plugin_context
from src.data import database as db_mod
from src.data.database import Database
from src.services.plugin_runtime import registry as pr_registry
from src.services.plugin_runtime.registry import get_plugin_runtime

CHAT_SKILL_PLUGIN_PY = '''
from typing import Any

try:
    from .plugin import ExpertPlugin
except ImportError:
    from omnispace.plugin import ExpertPlugin


class ChatSkillPlugin(ExpertPlugin):
    BASE_MODEL = "chat.skill"
    CAPABILITY = "chat skill test"

    def on_think(self, event, ctx):
        data = event.get("data") or {}
        return {"text": "已处理: " + str(data.get("text", ""))}
'''

CHAT_SKILL = {"id": "polish", "feature": "chat", "title": "台词润色",
              "description": "润色当段对白", "input": "text"}


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
    from src.main import app
    # 登记测试技能插件（进程内档，避免沙箱子进程拖慢单测）
    src = tmp_path / "chat_skill_plugin.py"
    src.write_text(CHAT_SKILL_PLUGIN_PY, encoding="utf-8")
    rt = get_plugin_runtime()
    rt.register("chat-demo", src, trust="user_data", skills=[CHAT_SKILL])
    return TestClient(app, base_url="http://127.0.0.1")


def _skill(client: TestClient, payload: dict) -> dict:
    return client.post("/api/v1/dialog/skill", json=payload).json()


# ── 端点 ─────────────────────────────────────────────────────
def test_skill_requires_params(api_client):
    body = _skill(api_client, {})
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SPEC_MISMATCH"


def test_skill_empty_text_rejected(api_client):
    body = _skill(api_client, {"plugin": "chat-demo",
                               "skill_id": "polish", "text": "  "})
    assert body["success"] is False
    assert body["error"]["code"] == "SYSTEM_PARAM_INVALID"


def test_skill_not_found(api_client):
    body = _skill(api_client, {"plugin": "chat-demo",
                               "skill_id": "nope", "text": "hi"})
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SKILL_NOT_FOUND"
    # 未登记插件同样落到技能不存在（不泄露登记面差异）
    body2 = _skill(api_client, {"plugin": "ghost", "skill_id": "x",
                                "text": "hi"})
    assert body2["error"]["code"] == "PLUGIN_SKILL_NOT_FOUND"


def test_skill_roundtrip(api_client):
    body = _skill(api_client, {"plugin": "chat-demo",
                               "skill_id": "polish", "text": "你好"})
    assert body["success"] is True, body
    data = body["data"]
    assert data["output"] == "已处理: 你好"
    assert data["title"] == "台词润色"
    assert data["plugin"] == "chat-demo"


# ── plugin_context 解析闸 ────────────────────────────────────
def test_plugin_context_none_and_legal():
    assert _parse_plugin_context(None) == []
    assert _parse_plugin_context([]) == []
    out = _parse_plugin_context([{"title": "润色", "text": "内容"}])
    assert out == [{"title": "润色", "text": "内容"}]


def test_plugin_context_not_list_rejected():
    from src.middleware.error_handler import ApiError
    with pytest.raises(ApiError) as ei:
        _parse_plugin_context({"title": "x", "text": "y"})
    assert ei.value.code == "SYSTEM_PARAM_INVALID" or \
        "SYSTEM_PARAM_INVALID" in str(ei.value.code)


def test_plugin_context_item_limit_and_truncate():
    # 条数上限：只取第一条
    two = _parse_plugin_context([
        {"title": "a", "text": "1"}, {"title": "b", "text": "2"}])
    assert len(two) == 1 and two[0]["title"] == "a"
    # 8K 截断
    long = _parse_plugin_context([{"title": "t", "text": "长" * 9000}])
    assert len(long[0]["text"]) == 8000
    # 空文本跳过 / 标题缺省
    assert _parse_plugin_context([{"title": "x", "text": " "}]) == []
    assert _parse_plugin_context([{"text": "hi"}])[0]["title"] == "插件技能"
