"""插件运行时测试（OSP v1 P1，2026-09-16；2026-09-17 适配 video-making 移除）。

覆盖面：基类生命周期/记忆、加载器（真插件源码+真 CuteMamen 包）、
登记闸/RAM 闸/软超时 faulty、invoke 帧落盘+路径防穿越（本地测试插件）、
API 端点（TestClient，对齐 test_api_smoke 模式）。
纯 CPU，不触 GPU。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.config import ROOT_DIR
from src.data import database as db_mod
from src.data.database import Database
from src.services.plugin_runtime import base as pr_base
from src.services.plugin_runtime import loader as pr_loader
from src.services.plugin_runtime import registry as pr_registry

REAL_PLUGIN_PY = ROOT_DIR / "src" / "cutemamen" / "rust_coding.py"
REAL_PKG = ROOT_DIR / "plugin" / "RustCoding.CuteMamen"

# 本地测试插件：返回帧序列（验证运行时 save_dirname 落盘/防穿越通用能力）
FRAMES_PLUGIN_PY = (
    "import numpy as np\n"
    "from omnispace.plugin import ExpertPlugin\n\n\n"
    "class FramesPlugin(ExpertPlugin):\n"
    "    CAPABILITY = 'test frames'\n\n"
    "    def on_think(self, event, ctx):\n"
    "        return {'frames': [np.zeros((4, 4, 3)) for _ in range(3)],\n"
    "                'summary': {'n_frames': 3}}\n"
)


# ── 夹具 ─────────────────────────────────────────────────────
@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """TestClient（不进 with = 不触发 lifespan，对齐 smoke 模式）。"""
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    monkeypatch.setattr(pr_registry, "OUTPUT_ROOT", tmp_path / "plug_out")
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture()
def frames_plugin_py(tmp_path) -> str:
    """写入本地帧测试插件源码并返回路径。"""
    path = tmp_path / "frames_plugin.py"
    path.write_text(FRAMES_PLUGIN_PY, encoding="utf-8")
    return str(path)


@pytest.fixture()
def fresh_runtime(tmp_path):
    """独立运行时实例（不污染单例；供闸门类直测）。"""
    monkey = pytest.MonkeyPatch()
    monkey.setattr(pr_registry, "OUTPUT_ROOT", tmp_path / "out")
    rt = pr_registry.PluginRuntime()
    yield rt
    monkey.undo()


# ── 基类 ─────────────────────────────────────────────────────
def test_plugin_memory_levels():
    # 统一版（2026-09-16）：单一真源 = src/cutemamen/plugin.py 的记忆模型
    mem = pr_base.PluginMemory()
    # working：键值（set/get）
    mem.set("a", 1)
    assert mem.get("a") == 1
    # semantic：蒸馏知识（remember/recall，LRU）
    mem.remember("b", 2)
    assert mem.recall("b") == 2
    # episodic：事件流水（record/recent），consolidate 蒸馏到 semantic
    mem.record("demo", summary="s1")
    counts = mem.consolidate()
    assert counts == {"demo": 1}
    assert mem.recall("topic:demo") == {"activations": 1}
    # archive/restore 往返（archive 列表形态）
    arc = mem.archive()
    mem2 = pr_base.PluginMemory()
    mem2.restore(arc)
    assert mem2.get("a") == 1 and mem2.recall("b") == 2
    # restore 容错：宿主 loader 的 {key: value} 字典形态 + 坏层级跳过
    mem.restore({"working": {"a": 9}, "bogus": 1})
    assert mem.get("a") == 9


def test_plugin_context_emit_silent_without_fn():
    ctx = pr_base.PluginContext()  # 无 emit_fn → 静默降级
    ctx.emit("t", {"x": 1})  # 不炸即过
    caught = []
    ctx2 = pr_base.PluginContext(emit_fn=lambda t, p: caught.append((t, p)))
    ctx2.emit("t", {"x": 1})
    assert caught == [("t", {"x": 1})]
    # 内核形态：总线优先于 emit_fn（统一后双构造等价）
    class _Bus:
        def __init__(self):
            self.published = []

        def publish(self, topic, payload):
            self.published.append((topic, payload))

    bus = _Bus()
    ctx3 = pr_base.PluginContext(bus=bus, plugin_name="p")
    ctx3.emit("k", {"v": 1})
    assert bus.published == [("k", {"v": 1})]


def test_expert_plugin_defaults():
    p = pr_base.ExpertPlugin("demo", route="r1")
    assert p.route == "r1" and p.memory is not None
    assert p.on_think({}, None) is None
    assert p.stats()["name"] == "demo"
    m = p.build_manifest(extra_key="v")
    assert m["extra_key"] == "v" and m["capability"] == ""


# ── 加载器 ───────────────────────────────────────────────────
def test_load_real_plugin_module_and_classes():
    pr_loader._ensure_host_module()
    assert "omnispace.plugin" in sys.modules
    mod = pr_loader.load_plugin_module(REAL_PLUGIN_PY)
    classes = pr_loader.find_plugin_classes(mod)
    assert classes, "真插件内必须发现 ExpertPlugin 子类"
    instance = classes[0](name="test-plugin")
    assert instance.route == "rust"


def test_read_real_cutemamen_pkg():
    pkg = pr_loader.read_cutemamen_pkg(REAL_PKG)
    assert pkg.manifest.get("name") == "rust-coding"
    assert pkg.manifest.get("format") == "CuteMamen"
    # 读出层权重随包注入（proto_<类别> 原型向量）
    assert any(k.startswith("proto_") for k in pkg.weights)
    assert set(pkg.memory) <= {"working", "episodic", "semantic"}


def test_read_cutemamen_pkg_rejects_missing():
    with pytest.raises(pr_loader.PluginLoadError):
        pr_loader.read_cutemamen_pkg(ROOT_DIR / "plugin" / "nope.CuteMamen")


def test_frames_to_png_roundtrip(tmp_path):
    frames = [np.random.rand(8, 8, 3).astype(np.float64) for _ in range(3)]
    names = pr_loader.frames_to_png_files(frames, tmp_path / "o")
    assert len(names) == 3
    for n in names:
        assert (tmp_path / "o" / n).stat().st_size > 0
    back = np.asarray(Image.open(tmp_path / "o" / names[0]))
    assert back.shape == (8, 8, 3) and back.dtype == np.uint8


# ── 登记闸 / RAM 闸 / 软超时（独立运行时直测） ───────────────
def test_registry_gate_rejects_unknown(fresh_runtime):
    with pytest.raises(pr_registry.PluginRuntimeError) as ei:
        asyncio.run(fresh_runtime.invoke("ghost", {}))
    assert ei.value.code == "PLUGIN_NOT_REGISTERED"


def test_ram_gate_rejects_low_memory(fresh_runtime, monkeypatch):
    monkeypatch.setattr(
        pr_registry.psutil, "virtual_memory",
        lambda: SimpleNamespace(available=int(0.5 * (1 << 30))))
    with pytest.raises(pr_registry.PluginRuntimeError) as ei:
        asyncio.run(fresh_runtime.invoke(
            "rust-coding", {"code": "fn main() {}"}))
    assert ei.value.code == "PLUGIN_RAM_LOW"


def test_invoke_timeout_marks_faulty(fresh_runtime, tmp_path):
    slow_py = tmp_path / "slow_plugin.py"
    slow_py.write_text(
        "import time\n"
        "from omnispace.plugin import ExpertPlugin\n\n\n"
        "class SlowPlugin(ExpertPlugin):\n"
        "    CAPABILITY = 'test slow'\n\n"
        "    def on_think(self, event, ctx):\n"
        "        time.sleep(2.0)\n"
        "        return {'summary': {'late': True}}\n",
        encoding="utf-8")
    fresh_runtime.register("slow", slow_py)
    with pytest.raises(pr_registry.PluginRuntimeError) as ei:
        asyncio.run(fresh_runtime.invoke("slow", {}, timeout_s=0.5))
    assert ei.value.code == "PLUGIN_TIMEOUT"
    entry = fresh_runtime._registry["slow"]
    assert entry.state == pr_registry.STATE_FAULTY
    fresh_runtime.unload("slow")  # 复位通道
    assert entry.state == pr_registry.STATE_UNLOADED


# ── invoke 帧落盘 + 防穿越（本地测试插件；运行时通用能力） ───
def test_invoke_roundtrip_with_save(fresh_runtime, frames_plugin_py):
    fresh_runtime.register("frames-demo", Path(frames_plugin_py))
    result = asyncio.run(fresh_runtime.invoke(
        "frames-demo", {}, save_dirname="run1"))
    assert result["summary"]["n_frames"] == 3
    assert len(result["saved_files"]) == 3
    out = Path(result["output_dir"])
    assert out.is_dir() and out.parent.name == "frames-demo"
    for name in result["saved_files"]:
        assert (out / name).is_file()
    assert "frames" not in result  # 帧本体绝不进返回值（内存红线）


def test_invoke_save_dirname_traversal_sanitized(fresh_runtime,
                                                 frames_plugin_py):
    fresh_runtime.register("frames-demo", Path(frames_plugin_py))
    result = asyncio.run(fresh_runtime.invoke(
        "frames-demo", {}, save_dirname="../../evil"))
    out = Path(result["output_dir"]).resolve()
    assert out.name == "evil" and ".." not in str(out)
    assert out.parent.name == "frames-demo"
    assert out.parent.parent == pr_registry.OUTPUT_ROOT.resolve()


# ── API 端点（TestClient） ───────────────────────────────────
def test_api_list_plugins(api_client):
    r = api_client.get("/api/v1/plugins")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    names = [p["name"] for p in body["data"]["plugins"]]
    assert "rust-coding" in names
    rc = next(p for p in body["data"]["plugins"]
              if p["name"] == "rust-coding")
    assert rc["trust"] == "repo_curated" and rc["state"] in (
        "unloaded", "loaded")


def test_api_invoke_unknown_plugin(api_client):
    r = api_client.post("/api/v1/plugins/ghost/invoke", json={
        "data": {"code": "fn main() {}"}})
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_NOT_REGISTERED"


def test_api_load_unload_cycle(api_client):
    r = api_client.post("/api/v1/plugins/rust-coding/load")
    assert r.json()["data"]["state"] == "loaded"
    r = api_client.post("/api/v1/plugins/rust-coding/unload")
    assert r.json()["data"]["state"] == "unloaded"


# ── RustCoding 登记 + 通用 invoke 形态（2026-09-16 拍板） ──────
def test_api_rust_invoke_generic_form(api_client):
    """rust-coding 经通用 data 形态 invoke：出 label/confidence 透传。"""
    r = api_client.post("/api/v1/plugins/rust-coding/invoke", json={
        "data": {"code": 'fn f() { let s = String::from("x"); '
                         'let t = s; let u = s; }'}})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True, body
    label = body["data"]["data"]["label"]
    assert label in ("move", "borrow", "lifetime", "type", "ok")
    assert 0.0 <= body["data"]["data"]["confidence"] <= 1.0


def test_api_invoke_requires_data(api_client):
    r = api_client.post("/api/v1/plugins/rust-coding/invoke", json={})
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SPEC_MISMATCH"


def test_api_kernel_status_and_think(api_client):
    """内核接线端点：状态轻量可用 + think 路由 rust 主题出分类。"""
    r = api_client.get("/api/v1/plugins/kernel")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True, body
    assert "kernel" in body["data"] and "plugins" in body["data"]

    r2 = api_client.post("/api/v1/plugins/kernel/think", json={
        "topic": "rust",
        "data": {"code": 'fn f() { let s = String::from("x"); '
                         'let t = s; let u = s; }'}})
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["success"] is True, body2
    assert body2["data"]["routed"] is True
    label = body2["data"]["results"][0]["label"]
    assert label in ("move", "borrow", "lifetime", "type", "ok")
