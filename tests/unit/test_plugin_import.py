"""插件用户导入测试（2026-09-16 拍板功能；规范=docs/插件开发规范.md）。

覆盖：纯数据包导入 / 源码安检闸（危险原语逐类拒）/ 确认门 /
试装载 / 重名拒 / 魔数拒 / 未知类型缺源码拒 / invoke 透传 /
停用-启用 / 删除清场（含出厂保护）/ 登记表持久化重载。
全链走 TestClient + 真包真源码字节，纯 CPU 不触 GPU。
"""
from __future__ import annotations

import io
import json
import tarfile
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.data import database as db_mod
from src.data.database import Database
from src.services.plugin_runtime import kernel_gateway
from src.services.plugin_runtime import registry as pr_registry


class _FakeKernel:
    """内核替身：避免测试触碰真实 data/ 目录。"""

    def __init__(self) -> None:
        self.registry: dict[str, str] = {}
        self.plugins: dict[str, Any] = {}
        self.route_index: dict[str, str] = {}
        self.routes: dict[str, str] = {}

    def register_pkg(self, path: str) -> dict:
        self.registry[path] = path
        return {}


ECHO_SRC = '''
from typing import Any

try:
    from .plugin import ExpertPlugin, PluginContext
except ImportError:
    from omnispace.plugin import ExpertPlugin, PluginContext


class EchoPlugin(ExpertPlugin):
    BASE_MODEL = "echo.demo"
    CAPABILITY = "回显测试插件"

    def on_think(self, event, ctx):
        return {"echo": event.get("data")}
'''


def _tar_add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _make_pkg(name: str, base_model: str = "video.making",
              route: str = "video") -> bytes:
    """手工构造合法 .CuteMamen 包（空权重+空记忆）。"""
    manifest = {
        "standard_version": "2.0.0", "name": name, "base_model": base_model,
        "capability": "测试用", "route": route,
        "lifecycle": ["on_load", "on_think", "on_unload"],
        "memory_levels": ["working", "episodic", "semantic"],
        "format": "CuteMamen",
    }
    weights = io.BytesIO()
    np.savez(weights)  # 空 npz（合法）
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _tar_add(tar, "manifest.json",
                 json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
        _tar_add(tar, "weights/weights.npz", weights.getvalue())
        _tar_add(tar, "memory/working.json", b'{"entries": {}}')
        _tar_add(tar, "memory/episodic.json", b'{"entries": {}}')
        _tar_add(tar, "memory/semantic.json", b'{"entries": {}}')
    return buf.getvalue()


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """隔离环境：用户插件目录/登记表进 tmp，运行时与内核单例复位。"""
    monkeypatch.setattr(pr_registry, "USER_PLUGIN_DIR",
                        tmp_path / "imported")
    monkeypatch.setattr(pr_registry, "USER_REGISTRY_PATH",
                        tmp_path / "user_registry.json")
    monkeypatch.setattr(pr_registry, "_runtime", None)
    monkeypatch.setattr(kernel_gateway, "_kernel", _FakeKernel())
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


def _post_import(client: TestClient, pkg_bytes: bytes, pkg_name: str,
                  source: tuple[str, bytes] | None = None,
                  confirm: bool = False):
    files = {"package": (pkg_name, pkg_bytes, "application/gzip")}
    if source is not None:
        files["source"] = (source[0], source[1], "text/x-python")
    data = {"confirm_source": "true"} if confirm else {}
    return client.post("/api/v1/plugins/import", files=files, data=data)


# ── 纯数据档 ─────────────────────────────────────────────────
def test_import_data_pkg_known_base(api_client, tmp_path):
    r = _post_import(api_client, _make_pkg("demo-video"),
                     "demo.CuteMamen")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True, body
    assert body["data"]["trust"] == "user_data"
    assert body["data"]["trust_label"] == "用户·纯数据"
    # 文件与登记表落盘
    assert (tmp_path / "imported" / "demo-video.CuteMamen").is_file()
    reg = json.loads((tmp_path / "user_registry.json").read_text("utf-8"))
    assert reg[0]["name"] == "demo-video"
    # 清单端点带用户档标识
    names = {p["name"]: p for p in
             api_client.get("/api/v1/plugins").json()["data"]["plugins"]}
    assert names["demo-video"]["origin"] == "user"
    assert names["demo-video"]["trust_label"] == "用户·纯数据"


def test_import_unknown_base_requires_source(api_client):
    r = _post_import(api_client, _make_pkg("brand-new", base_model="brand.new"),
                     "brand.CuteMamen")
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SOURCE_REQUIRED"


def test_import_bad_magic_rejected(api_client):
    r = _post_import(api_client, b"NOT_GZIP_AT_ALL_1234", "fake.CuteMamen")
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_PACKAGE_INVALID"
    assert "gzip" in body["error"]["message"]


def test_import_name_collision_with_factory(api_client):
    r = _post_import(api_client, _make_pkg("video-making"),
                     "dup.CuteMamen")
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_ALREADY_REGISTERED"


# ── 源码档：安检闸 / 确认门 / 试装载 ─────────────────────────
def test_import_source_subprocess_rejected(api_client):
    bad = "import subprocess\n" + ECHO_SRC
    r = _post_import(api_client, _make_pkg("bad-sub", base_model="echo.demo"),
                     "bad.CuteMamen", source=("bad.py", bad.encode()),
                     confirm=True)
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SOURCE_FORBIDDEN"
    assert "subprocess" in body["error"]["suggestion"]


def test_import_source_open_call_rejected(api_client):
    bad = ECHO_SRC + "\n\ndef _sneaky():\n    return open('x').read()\n"
    r = _post_import(api_client, _make_pkg("bad-open", base_model="echo.demo"),
                     "bad.CuteMamen", source=("bad.py", bad.encode()),
                     confirm=True)
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SOURCE_FORBIDDEN"
    assert "open" in body["error"]["suggestion"]


def test_import_source_confirm_gate(api_client):
    pkg = _make_pkg("demo-echo", base_model="echo.demo")
    # 未确认 → 拒
    r = _post_import(api_client, pkg, "echo.CuteMamen",
                     source=("echo.py", ECHO_SRC.encode()))
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SOURCE_CONFIRM_REQUIRED"
    # 确认 → 过
    r2 = _post_import(api_client, pkg, "echo.CuteMamen",
                      source=("echo.py", ECHO_SRC.encode()), confirm=True)
    assert r2.status_code == 200
    assert r2.json()["data"]["trust"] == "user_source"


def test_import_source_no_plugin_class_rejected(api_client):
    src = "x = 1\n"
    r = _post_import(api_client, _make_pkg("no-class", base_model="echo.demo"),
                     "no.CuteMamen", source=("no.py", src.encode()),
                     confirm=True)
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SOURCE_INVALID"


def test_import_source_and_invoke_roundtrip(api_client):
    r = _post_import(api_client, _make_pkg("demo-echo", base_model="echo.demo"),
                     "echo.CuteMamen", source=("echo.py", ECHO_SRC.encode()),
                     confirm=True)
    assert r.json()["success"] is True
    r2 = api_client.post("/api/v1/plugins/demo-echo/invoke",
                         json={"data": {"hello": "world"}})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["success"] is True, body
    assert body["data"]["data"]["echo"]["hello"] == "world"


# ── 治理：停用 / 删除 / 持久化 ────────────────────────────────
def test_disable_blocks_invoke_and_enable_restores(api_client):
    _post_import(api_client, _make_pkg("demo-echo", base_model="echo.demo"),
                 "echo.CuteMamen", source=("echo.py", ECHO_SRC.encode()),
                 confirm=True)
    r = api_client.post("/api/v1/plugins/demo-echo/disable")
    assert r.json()["data"]["enabled"] is False
    r2 = api_client.post("/api/v1/plugins/demo-echo/invoke",
                         json={"data": {"x": 1}})
    body = r2.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_DISABLED"
    r3 = api_client.post("/api/v1/plugins/demo-echo/enable")
    assert r3.json()["data"]["enabled"] is True
    r4 = api_client.post("/api/v1/plugins/demo-echo/invoke",
                         json={"data": {"x": 1}})
    assert r4.json()["success"] is True


def test_delete_user_plugin_cleans_files(api_client, tmp_path):
    _post_import(api_client, _make_pkg("demo-video"), "demo.CuteMamen")
    assert (tmp_path / "imported" / "demo-video.CuteMamen").is_file()
    r = api_client.delete("/api/v1/plugins/demo-video")
    assert r.json()["data"]["deleted"] is True
    assert not (tmp_path / "imported" / "demo-video.CuteMamen").exists()
    reg = json.loads((tmp_path / "user_registry.json").read_text("utf-8"))
    assert reg == []
    names = [p["name"] for p in
             api_client.get("/api/v1/plugins").json()["data"]["plugins"]]
    assert "demo-video" not in names


def test_delete_factory_plugin_protected(api_client):
    r = api_client.delete("/api/v1/plugins/video-making")
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_FACTORY_PROTECTED"


def test_user_registry_persists_across_restart(api_client, tmp_path,
                                               monkeypatch):
    _post_import(api_client, _make_pkg("demo-video"), "demo.CuteMamen")
    # 模拟重启：丢弃单例，按登记表重建
    monkeypatch.setattr(pr_registry, "_runtime", None)
    fresh = pr_registry.PluginRuntime()
    assert fresh.is_registered("demo-video")
