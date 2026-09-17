"""技能插座批0测试（2026-09-17，方案 docs/插件技能层接线方案-2026-09-17.md）。

覆盖：validate_skills 校验闸（feature 白名单/id 正则/数量上限/input
枚举/非列表）、登记表 skills_info 过滤与停用排除、GET /plugins/skills
端点（含 feature 非法拒绝）、导入链 skills 校验拒收与合法登记持久化、
出厂技能面为空（D3=A 插座先行）。纯 CPU 不触 GPU。
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database
from src.services.plugin_runtime import kernel_gateway
from src.services.plugin_runtime import registry as pr_registry
from src.services.plugin_runtime.registry import PluginRuntimeError

LEGAL_SKILL = {"id": "polish-dialog", "feature": "chat",
               "title": "台词润色", "description": "润色当段对白",
               "input": "text"}


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """隔离环境（对齐 test_plugin_import 夹具）。"""
    monkeypatch.setattr(pr_registry, "USER_PLUGIN_DIR",
                        tmp_path / "imported")
    monkeypatch.setattr(pr_registry, "USER_REGISTRY_PATH",
                        tmp_path / "user_registry.json")
    monkeypatch.setattr(pr_registry, "FACTORY_OVERRIDE_PATH",
                        tmp_path / "factory_overrides.json")
    monkeypatch.setattr(pr_registry, "_runtime", None)
    monkeypatch.setattr(kernel_gateway, "_kernel", _FakeKernel())
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


class _FakeKernel:
    def __init__(self) -> None:
        self.registry: dict[str, str] = {}

    def register_pkg(self, path: str) -> dict:
        self.registry[path] = path
        return {}


@pytest.fixture()
def fresh_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(pr_registry, "USER_PLUGIN_DIR",
                        tmp_path / "imported")
    monkeypatch.setattr(pr_registry, "USER_REGISTRY_PATH",
                        tmp_path / "user_registry.json")
    monkeypatch.setattr(pr_registry, "FACTORY_OVERRIDE_PATH",
                        tmp_path / "factory_overrides.json")
    rt = pr_registry.PluginRuntime()
    yield rt


def _make_pkg(name: str, skills: object = None) -> bytes:
    """手工构造纯数据包（rust.coding 底座），可选 skills 注入 manifest。"""
    manifest: dict = {
        "standard_version": "2.0.0", "name": name,
        "base_model": "rust.coding", "capability": "测试用", "route": "rust",
        "lifecycle": ["on_load", "on_think", "on_unload"],
        "memory_levels": ["working", "episodic", "semantic"],
        "format": "CuteMamen",
    }
    if skills is not None:
        manifest["skills"] = skills
    weights = io.BytesIO()
    np.savez(weights)
    buf = io.BytesIO()

    def _add(tar: tarfile.TarFile, n: str, data: bytes) -> None:
        info = tarfile.TarInfo(n)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add(tar, "manifest.json",
             json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
        _add(tar, "weights/weights.npz", weights.getvalue())
        _add(tar, "memory/working.json", b'{"entries": {}}')
        _add(tar, "memory/episodic.json", b'{"entries": {}}')
        _add(tar, "memory/semantic.json", b'{"entries": {}}')
    return buf.getvalue()


def _post_import(client: TestClient, pkg_bytes: bytes) -> object:
    return client.post(
        "/api/v1/plugins/import",
        files={"package": ("p.CuteMamen", pkg_bytes, "application/gzip")})


# ── validate_skills 校验闸 ───────────────────────────────────
def test_validate_skills_none_and_legal():
    assert pr_registry.validate_skills(None) == []
    out = pr_registry.validate_skills([LEGAL_SKILL])
    assert out[0]["id"] == "polish-dialog"
    assert out[0]["feature"] == "chat"


def test_validate_skills_rejects():
    with pytest.raises(PluginRuntimeError) as ei:
        pr_registry.validate_skills({"id": "x"})
    assert ei.value.code == "PLUGIN_SPEC_MISMATCH"
    # feature 白名单
    with pytest.raises(PluginRuntimeError):
        pr_registry.validate_skills([dict(LEGAL_SKILL, feature="video")])
    # id 正则
    with pytest.raises(PluginRuntimeError):
        pr_registry.validate_skills([dict(LEGAL_SKILL, id="Bad_Id!")])
    # input 枚举
    with pytest.raises(PluginRuntimeError):
        pr_registry.validate_skills([dict(LEGAL_SKILL, input="audio")])
    # 数量上限
    with pytest.raises(PluginRuntimeError):
        pr_registry.validate_skills([LEGAL_SKILL] * 5)
    # title 必填
    with pytest.raises(PluginRuntimeError):
        pr_registry.validate_skills([dict(LEGAL_SKILL, title="  ")])


def test_validate_skills_truncates_description():
    skill = dict(LEGAL_SKILL, description="长" * 600)
    out = pr_registry.validate_skills([skill])
    assert len(out[0]["description"]) == 500


# ── 登记表 skills_info ──────────────────────────────────────
def test_skills_info_filter_and_disabled(fresh_runtime):
    src = Path("src/cutemamen/rust_coding.py")
    fresh_runtime.register(
        "demo-skill", src, trust="user_data",
        skills=[LEGAL_SKILL,
                dict(LEGAL_SKILL, id="panel-clean", feature="comic")])
    # feature 过滤
    chat = fresh_runtime.skills_info("chat")
    assert len(chat) == 1 and chat[0]["plugin"] == "demo-skill"
    assert chat[0]["trust_label"] == "用户·纯数据"
    comic = fresh_runtime.skills_info("comic")
    assert len(comic) == 1 and comic[0]["id"] == "panel-clean"
    # 全量
    assert len(fresh_runtime.skills_info()) == 2
    # 停用即排除
    fresh_runtime.set_enabled("demo-skill", False)
    assert fresh_runtime.skills_info("chat") == []
    # feature 非法
    with pytest.raises(PluginRuntimeError):
        fresh_runtime.skills_info("video")


def test_factory_skill_surface_empty(fresh_runtime):
    """D3=A：v1 不新增出厂技能（插座先行，出厂面为空）。"""
    assert fresh_runtime.skills_info() == []


# ── API 端点 ─────────────────────────────────────────────────
def test_api_skills_endpoint_empty(api_client):
    r = api_client.get("/api/v1/plugins/skills")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["skills"] == []
    assert "chat" in body["data"]["features"]
    # feature 非法 → 语义错误码
    r2 = api_client.get("/api/v1/plugins/skills?feature=video")
    assert r2.json()["error"]["code"] == "PLUGIN_SPEC_MISMATCH"


def test_import_rejects_illegal_skills(api_client):
    bad = _make_pkg("bad-skill", skills=[dict(LEGAL_SKILL, feature="video")])
    r = _post_import(api_client, bad)
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SPEC_MISMATCH"


def test_import_legal_skills_persisted(api_client, tmp_path):
    good = _make_pkg("good-skill", skills=[LEGAL_SKILL])
    r = _post_import(api_client, good)
    assert r.json()["success"] is True, r.text
    # 端点可见（无需加载插件）
    skills = api_client.get(
        "/api/v1/plugins/skills?feature=chat").json()["data"]["skills"]
    assert [s["plugin"] for s in skills] == ["good-skill"]
    # 登记表持久化
    reg = json.loads(
        (tmp_path / "user_registry.json").read_text("utf-8"))
    assert reg[0]["skills"][0]["id"] == "polish-dialog"
    # 插件清单也带 skills
    plugins = api_client.get("/api/v1/plugins").json()["data"]["plugins"]
    entry = next(p for p in plugins if p["name"] == "good-skill")
    assert entry["skills"][0]["feature"] == "chat"
