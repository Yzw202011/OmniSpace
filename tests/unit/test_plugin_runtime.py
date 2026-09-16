"""插件运行时测试（OSP v1 P1，2026-09-16）。

覆盖面：基类生命周期/记忆、加载器（真插件源码+真 CuteMamen 包）、
登记闸/RAM 闸/软超时 faulty、invoke 全链（真渲染+帧落盘+路径防穿越）、
API 四端点（TestClient，对齐 test_api_smoke 模式）。
纯 CPU 小图（32×48），不触 GPU。
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

REAL_PLUGIN_PY = ROOT_DIR / "src" / "cutemamen" / "video_making.py"
REAL_PKG = ROOT_DIR / "plugin" / "VideoMaking.CuteMamen"


# ── 夹具 ─────────────────────────────────────────────────────
@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """TestClient（不进 with = 不触发 lifespan，对齐 smoke 模式）。"""
    monkeypatch.setattr(db_mod, "_db_instance", Database(tmp_path / "t.db"))
    monkeypatch.setattr(pr_registry, "OUTPUT_ROOT", tmp_path / "plug_out")
    from src.main import app
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture()
def keyframe_png(tmp_path) -> str:
    """生成一张小关键帧图（32×48 渐变）。"""
    img = Image.fromarray(
        (np.random.rand(32, 48, 3) * 255).astype(np.uint8), mode="RGB")
    path = tmp_path / "kf.png"
    img.save(path, format="PNG")
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
    assert instance.route == "video"
    motions = getattr(instance, "available_motions", None)
    assert callable(motions) and "zoom_in" in motions()


def test_read_real_cutemamen_pkg():
    pkg = pr_loader.read_cutemamen_pkg(REAL_PKG)
    assert pkg.manifest.get("name") == "video-making"
    assert pkg.manifest.get("format") == "CuteMamen"
    assert "__stats__" in pkg.weights
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
        asyncio.run(fresh_runtime.invoke("ghost", {"keyframes": []}))
    assert ei.value.code == "PLUGIN_NOT_REGISTERED"


def test_ram_gate_rejects_low_memory(fresh_runtime, monkeypatch):
    monkeypatch.setattr(
        pr_registry.psutil, "virtual_memory",
        lambda: SimpleNamespace(available=int(0.5 * (1 << 30))))
    with pytest.raises(pr_registry.PluginRuntimeError) as ei:
        asyncio.run(fresh_runtime.invoke(
            "video-making", {"keyframes": [np.zeros((8, 8, 3))]}))
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


# ── invoke 全链（真渲染 + 落盘 + 防穿越） ────────────────────
def test_invoke_roundtrip_with_save(fresh_runtime, keyframe_png, tmp_path):
    frame = pr_loader.read_image_as_frame(Path(keyframe_png))
    result = asyncio.run(fresh_runtime.invoke(
        "video-making",
        {"keyframes": [frame, frame], "fps": 12,
         "shots": [{"motion": "zoom_in", "duration_s": 0.4},
                   {"motion": "pan_left", "duration_s": 0.3,
                    "transition": "crossfade"}]},
        save_dirname="run1"))
    assert result["summary"]["n_frames"] >= 4
    assert len(result["saved_files"]) == result["summary"]["n_frames"]
    out = Path(result["output_dir"])
    assert out.is_dir() and out.parent.name == "video-making"
    for name in result["saved_files"]:
        assert (out / name).is_file()
    assert "frames" not in result  # 帧本体绝不进返回值（内存红线）


def test_invoke_save_dirname_traversal_sanitized(fresh_runtime,
                                                 keyframe_png):
    frame = pr_loader.read_image_as_frame(Path(keyframe_png))
    result = asyncio.run(fresh_runtime.invoke(
        "video-making",
        {"keyframes": [frame], "fps": 12, "shots": []},
        save_dirname="../../evil"))
    out = Path(result["output_dir"]).resolve()
    assert out.name == "evil" and ".." not in str(out)
    assert out.parent.name == "video-making"
    assert out.parent.parent == pr_registry.OUTPUT_ROOT.resolve()


# ── API 端点（TestClient） ───────────────────────────────────
def test_api_list_plugins(api_client):
    r = api_client.get("/api/v1/plugins")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    names = [p["name"] for p in body["data"]["plugins"]]
    assert "video-making" in names
    vm = next(p for p in body["data"]["plugins"]
              if p["name"] == "video-making")
    assert vm["trust"] == "repo_curated" and vm["state"] in (
        "unloaded", "loaded")


def test_api_invoke_full_flow(api_client, keyframe_png):
    r = api_client.post("/api/v1/plugins/video-making/invoke", json={
        "keyframes": [keyframe_png, keyframe_png],
        "fps": 12,
        "shots": [{"motion": "zoom_in", "duration_s": 0.4},
                  {"motion": "orbit_left", "duration_s": 0.3,
                   "transition": "crossfade"}],
        "save_to": "apitest"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True, body
    data = body["data"]
    assert data["summary"]["n_frames"] >= 4
    assert len(data["saved_files"]) == data["summary"]["n_frames"]
    assert data["plan"][0]["motion"] == "zoom_in"


def test_api_invoke_spec_mismatch_shots(api_client, keyframe_png):
    r = api_client.post("/api/v1/plugins/video-making/invoke", json={
        "keyframes": [keyframe_png],
        "shots": [{"motion": "static"}, {"motion": "static"}]})
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SPEC_MISMATCH"


def test_api_invoke_spec_mismatch_motion(api_client, keyframe_png):
    r = api_client.post("/api/v1/plugins/video-making/invoke", json={
        "keyframes": [keyframe_png],
        "shots": [{"motion": "no_such_motion"}]})
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_SPEC_MISMATCH"
    assert "zoom_in" in body["error"]["suggestion"]  # 合法清单在出路提示里


def test_api_invoke_missing_keyframe_file(api_client, tmp_path):
    r = api_client.post("/api/v1/plugins/video-making/invoke", json={
        "keyframes": [str(tmp_path / "nope.png")]})
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_KEYFRAME_UNREADABLE"


def test_api_invoke_unknown_plugin(api_client, keyframe_png):
    r = api_client.post("/api/v1/plugins/ghost/invoke", json={
        "keyframes": [keyframe_png]})
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "PLUGIN_NOT_REGISTERED"


def test_api_load_unload_cycle(api_client):
    r = api_client.post("/api/v1/plugins/video-making/load")
    assert r.json()["data"]["state"] == "loaded"
    r = api_client.post("/api/v1/plugins/video-making/unload")
    assert r.json()["data"]["state"] == "unloaded"
