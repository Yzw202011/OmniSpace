"""v0.7.2 CuteMamen 插件标准落地测试

覆盖: 通用固定内核 (路由器/工作记忆/插件注册表/内存预算淘汰) /
生命周期钩子 / 三级记忆存档 / 事件总线通信 / .CuteMamen 包格式与
解码器 / LoRA-Adapter 桥接 / FacePlugin (v0.7.0 模态面 pkg 首个特例) /
CubeGPTKernel 模型精简 / migrate-v1-to-v2 迁移工具。
"""

import io
import json
import os
import sys
import tarfile

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.distributedformer import CubeGPT
from src.cutemamen import (
    CubeGPTKernel,
    CuteMamenKernel,
    EventBus,
    ExpertPlugin,
    FacePlugin,
    LoRAAdapter,
    LoRABridgePlugin,
    PluginMemory,
    apply_lora,
    decode_manifest,
    load_pkg,
    lora_from_weight,
    read_manifest,
    save_pkg,
)
from src.cutemamen.migrate import main as migrate_main


# ── 测试用插件 ────────────────────────────────────────────

class EchoPlugin(ExpertPlugin):
    """回声插件: on_think 返回输入数据"""
    BASE_MODEL = "generic"
    calls = []

    def on_load(self, ctx):
        self.calls.append(("load", self.name))
        super().on_load(ctx)

    def on_think(self, event, ctx):
        super().on_think(event, ctx)
        self.calls.append(("think", self.name))
        return {"echo": event.get("data")}

    def on_unload(self):
        self.calls.append(("unload", self.name))
        super().on_unload()


@pytest.fixture
def kernel():
    return CuteMamenKernel(dim=16, working_memory_capacity=1000)


# ═══════════════════════════════════════════════════════════
# 1. 内核: 路由器 + 生命周期 + 注册表
# ═══════════════════════════════════════════════════════════

def test_lifecycle_hook_order(kernel):
    plugin = EchoPlugin("echo")
    kernel.mount(plugin)
    assert plugin.loaded and plugin.calls[-1] == ("load", "echo")

    out = kernel.think({"topic": "echo", "data": 42})
    assert out == [{"echo": 42}]
    assert plugin.think_count == 1

    kernel.unmount("echo")
    assert plugin.calls == [("load", "echo"), ("think", "echo"),
                            ("unload", "echo")]
    assert "echo" not in kernel.plugins


def test_router_dispatches_by_topic(kernel):
    kernel.mount(EchoPlugin("a"), route="topic-a")
    kernel.mount(EchoPlugin("b"), route="topic-b")
    assert kernel.think({"topic": "topic-a", "data": "x"}) == [{"echo": "x"}]
    assert kernel.think({"topic": "topic-b", "data": "y"}) == [{"echo": "y"}]
    # 未路由主题: 不报错, 广播 kernel.unrouted
    unrouted = []
    kernel.bus.subscribe("kernel.unrouted", lambda e: unrouted.append(e))
    assert kernel.think({"topic": "nope", "data": 1}) == []
    assert unrouted


def test_mount_duplicate_rejected(kernel):
    kernel.mount(EchoPlugin("dup"))
    with pytest.raises(ValueError, match="已挂载"):
        kernel.mount(EchoPlugin("dup"))


def test_registry_hot_load(kernel, tmp_path):
    """卸载 (自动存档) → think() 路由触发随用随载热加载"""
    rng = np.random.RandomState(0)
    ad = LoRAAdapter("w", a=rng.randn(2, 16), b=rng.randn(16, 2))
    plugin = LoRABridgePlugin("lazy", adapter=ad)
    kernel.mount(plugin)
    pkg = str(tmp_path / "lazy.CuteMamen")
    kernel.unmount("lazy", pkg_path=pkg)
    assert os.path.exists(pkg)
    assert kernel.list_plugins() == {"loaded": [], "registered": ["lazy"]}

    # 路由到已注册未加载的插件 → 现场热加载 (base_model 注册表分发)
    out = kernel.think({"topic": "lazy", "data": np.ones(16)})
    assert "lazy" in kernel.plugins
    assert kernel.list_plugins()["registered"] == []
    assert out and "output" in out[0]


def test_min_core_version_rejects(tmp_path):
    from src.cutemamen import check_core_version
    with pytest.raises(ValueError, match="拒绝加载"):
        check_core_version("99.0.0", "0.7.2")
    check_core_version("0.7.0", "0.7.2")  # 不抛


# ═══════════════════════════════════════════════════════════
# 2. 三级记忆存档
# ═══════════════════════════════════════════════════════════

def test_three_level_memory_roundtrip():
    mem = PluginMemory()
    mem.set("state", [1, 2, 3])
    mem.record("numeric", "spike burst")
    mem.record("text", "hello")
    mem.remember("rust:E0382", {"count": 5})
    counts = mem.consolidate()
    assert counts["numeric"] == 1

    archived = mem.archive()
    restored = PluginMemory()
    restored.restore(archived)
    assert restored.get("state") == [1, 2, 3]
    assert restored.recall("rust:E0382") == {"count": 5}
    assert restored.recall("topic:numeric") == {"activations": 1}
    assert len(restored.episodic) == 2


def test_memory_capacity_eviction():
    mem = PluginMemory(working_capacity=3, episodic_capacity=2,
                       semantic_capacity=2)
    for i in range(5):
        mem.set(f"k{i}", i)
        mem.record("t", i)
        mem.remember(f"s{i}", i)
    assert list(mem.working) == ["k2", "k3", "k4"]
    assert len(mem.episodic) == 2
    assert list(mem.semantic) == ["s3", "s4"]


def test_numpy_memory_values_json_safe():
    mem = PluginMemory()
    mem.set("vec", np.arange(3))
    mem.remember("arr", np.ones((2, 2)))
    archived = json.dumps(mem.archive())  # 不抛 = JSON 安全
    assert archived
    restored = PluginMemory()
    restored.restore(json.loads(archived))
    assert restored.get("vec") == {"__ndarray__": [0, 1, 2]}


# ═══════════════════════════════════════════════════════════
# 3. 事件总线通信
# ═══════════════════════════════════════════════════════════

def test_event_bus_pubsub():
    bus = EventBus()
    got = []
    unsub = bus.subscribe("plugin.a.output", lambda e: got.append(e["payload"]))
    bus.publish("plugin.a.output", {"value": 1})
    assert got == [{"value": 1}]
    unsub()
    bus.publish("plugin.a.output", {"value": 2})
    assert got == [{"value": 1}]


def test_event_bus_wildcard_and_error_isolation():
    bus = EventBus()
    all_events = []
    bus.subscribe("*", lambda e: all_events.append(e["topic"]))
    bus.subscribe("bad", lambda e: 1 / 0)  # 订阅方抛错不中断
    assert bus.publish("bad", None) == 1
    assert "bad" in all_events
    assert bus.errors


def test_plugin_to_plugin_communication(kernel):
    """插件 A 的输出 → 插件 B 订阅接收 (事件总线通信)"""
    received = []

    class ListenerPlugin(ExpertPlugin):
        def on_load(self, ctx):
            super().on_load(ctx)
            ctx.emit  # 总线可达
            self._unsub = ctx.bus.subscribe(
                "plugin.talker.output",
                lambda e: received.append(e["payload"]["result"]))

        def on_unload(self):
            self._unsub()
            super().on_unload()

    kernel.mount(EchoPlugin("talker"))
    kernel.mount(ListenerPlugin("listener"))
    kernel.think({"topic": "talker", "data": "ping"})
    assert received == [{"echo": "ping"}]


def test_kernel_lifecycle_events_broadcast(kernel):
    events = []
    kernel.bus.subscribe("*", lambda e: events.append(e["topic"]))
    kernel.mount(EchoPlugin("x"))
    pkg_events = [t for t in events if t == "plugin.loaded"]
    assert pkg_events
    kernel.unmount("x")
    assert "plugin.unloaded" in events


# ═══════════════════════════════════════════════════════════
# 4. .CuteMamen 包格式 + 解码器
# ═══════════════════════════════════════════════════════════

def test_pkg_roundtrip_with_memory(tmp_path):
    plugin = EchoPlugin("echo")
    plugin.memory.set("session", "abc")
    plugin.memory.remember("knowledge", {"k": 1})
    manifest = save_pkg(plugin, str(tmp_path / "echo.CuteMamen"),
                        author="tester")
    assert manifest["standard_version"] == "2.0.0"
    assert manifest["format"] == "CuteMamen"
    assert manifest["lifecycle"] == ["on_load", "on_think", "on_unload"]

    loaded, m2 = load_pkg(str(tmp_path / "echo.CuteMamen"),
                          plugin_cls=EchoPlugin)
    assert loaded.memory.get("session") == "abc"
    assert loaded.memory.recall("knowledge") == {"k": 1}
    assert loaded.memory.recall("pkg:origin") == "echo.CuteMamen"


def test_pkg_structure_matches_spec(tmp_path):
    plugin = EchoPlugin("echo")
    save_pkg(plugin, str(tmp_path / "echo.CuteMamen"))
    with tarfile.open(str(tmp_path / "echo.CuteMamen"), "r:gz") as tar:
        names = {m.name for m in tar.getmembers() if not m.isdir()}
    assert {"manifest.json", "weights/weights.npz",
            "memory/working.json", "memory/episodic.json",
            "memory/semantic.json"} <= names


def test_decoder_v1_to_v2_adaptation():
    raw = {
        "name": "old", "standard_version": "1.2.0",
        "model_type": "transformer", "legacy_mode": True,
        "custom_kept": "yes",
        "lifecycle": {"on_init": "native", "on_think": "native"},
    }
    manifest, changes = decode_manifest(raw)
    assert manifest["base_model"] == "transformer"
    assert "legacy_mode" not in manifest
    assert "model_type" not in manifest
    assert manifest["custom_kept"] == "yes"  # 未知字段保留
    assert manifest["lifecycle"]["on_load"] == "native"
    assert manifest["lifecycle"]["on_unload"] == "stub"
    assert any("on_init" in c for c in changes)


def test_decoder_unmappable_field_flagged():
    manifest, changes = decode_manifest(
        {"name": "x", "standard_version": "1.0.0",
         "pipeline_config": {...}})
    assert any("pipeline_config" in c and "no v2 equivalent" in c
               for c in changes)
    # 字段本身仍保留 (前向兼容), 迁移工具 --strict 时报错
    assert "pipeline_config" in manifest


def test_read_manifest_unknown_fields_preserved(tmp_path):
    plugin = EchoPlugin("echo")
    save_pkg(plugin, str(tmp_path / "e.CuteMamen"), future_field="v3")
    m = read_manifest(str(tmp_path / "e.CuteMamen"))
    assert m["future_field"] == "v3"


# ═══════════════════════════════════════════════════════════
# 5. 内存预算淘汰
# ═══════════════════════════════════════════════════════════

def test_memory_budget_eviction_archives_and_registers(tmp_path):
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        kernel = CuteMamenKernel(dim=16, memory_budget_mb=5.0)
        kernel.pkg_dir = "."
        big = EchoPlugin("big")
        big.footprint_mb = lambda: 10.0
        small = EchoPlugin("small")
        small.footprint_mb = lambda: 0.0
        evicted = []
        kernel.bus.subscribe("plugin.evicted",
                             lambda e: evicted.append(e["payload"]))
        kernel.mount(big)
        assert evicted == []  # 单插件不淘汰
        kernel.mount(small)
        assert [e["name"] for e in evicted] == ["big"]
        assert "big" not in kernel.plugins
        assert "big" in kernel.registry  # 存档可恢复
        assert os.path.exists(evicted[0]["pkg"])
    finally:
        os.chdir(cwd)


def test_budget_never_evicts_protected(kernel):
    kernel.memory_budget_mb = 5.0
    a = EchoPlugin("a"); a.footprint_mb = lambda: 10.0
    b = EchoPlugin("b"); b.footprint_mb = lambda: 10.0
    kernel.mount(a)
    kernel.mount(b)  # mount(b) protect=b → 淘汰 a
    assert "a" not in kernel.plugins and "b" in kernel.plugins


# ═══════════════════════════════════════════════════════════
# 6. LoRA / Adapter 兼容桥接
# ═══════════════════════════════════════════════════════════

def test_lora_adapter_math():
    rng = np.random.RandomState(0)
    a = rng.randn(4, 16)
    b = rng.randn(8, 4)
    ad = LoRAAdapter("wq", a=a, b=b, alpha=2.0)
    assert ad.rank == 4 and ad.scale == 0.5
    x = rng.randn(16)
    assert np.allclose(ad.forward(x), ad.delta() @ x)
    base = rng.randn(8, 16)
    merged = apply_lora(base, ad)
    assert np.allclose(merged, base + 0.5 * (b @ a))


def test_lora_plugin_roundtrip_and_forward(kernel):
    rng = np.random.RandomState(1)
    ad = LoRAAdapter("wq", a=rng.randn(4, 16), b=rng.randn(16, 4), alpha=1.0)
    plugin = LoRABridgePlugin("lora-wq", adapter=ad)
    kernel.mount(plugin)
    x = rng.randn(16)
    out = kernel.think({"topic": "lora-wq", "data": x})
    assert np.allclose(out[0]["output"], ad.forward(x))
    assert out[0]["target"] == "wq"
    assert plugin.to_lora() is ad


def test_lora_plugin_save_load(tmp_path):
    rng = np.random.RandomState(2)
    ad = LoRAAdapter("wv", a=rng.randn(2, 16), b=rng.randn(16, 2), alpha=3.0)
    plugin = LoRABridgePlugin("lora-wv", adapter=ad)
    save_pkg(plugin, str(tmp_path / "wv.CuteMamen"))
    loaded, manifest = load_pkg(str(tmp_path / "wv.CuteMamen"))
    assert isinstance(loaded, LoRABridgePlugin)  # base_model 注册表分发
    x = rng.randn(16)
    assert np.allclose(loaded.adapter.forward(x), ad.forward(x))
    assert manifest["lora_target"] == "wv"


def test_lora_from_weight_svd():
    rng = np.random.RandomState(3)
    w = rng.randn(8, 16)
    adapter = lora_from_weight("out", w, rank=4)
    assert adapter.delta().shape == (8, 16)
    # rank-4 SVD 近似: 低秩能量占优
    err = np.linalg.norm(w - adapter.delta())
    assert err < np.linalg.norm(w)


# ═══════════════════════════════════════════════════════════
# 7. FacePlugin: v0.7.0 模态面 pkg 首个特例
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def cube_kernel():
    np.random.seed(11)
    k = CubeGPTKernel(depth=1, dim=16, modalities=["numeric", "text"])
    for i in range(3):
        k.step({"numeric": 0.5 * i + 0.1, "text": "hello world"})
    return k


def test_face_plugin_lifecycle_and_think(cube_kernel):
    plugin = cube_kernel.plugins["text"]
    assert isinstance(plugin, FacePlugin)
    assert plugin.route == "text" and plugin.loaded
    assert plugin.think_count >= 3
    assert plugin.face is cube_kernel.faces["text"]


def test_face_plugin_pkg_roundtrip_state_exact(cube_kernel, tmp_path):
    plugin = cube_kernel.plugins["text"]
    plugin.memory.remember("trained_on", "rust")
    save_pkg(plugin, str(tmp_path / "text.CuteMamen"))
    loaded, manifest = load_pkg(str(tmp_path / "text.CuteMamen"))
    assert isinstance(loaded, FacePlugin)
    assert manifest["base_model"] == "cubegpt.face"
    assert loaded.memory.recall("trained_on") == "rust"
    # 权重与状态逐位还原
    src = plugin.face.cortex._all_units_cache
    dst = loaded.face.cortex._all_units_cache
    assert len(src) == len(dst)
    for u1, u2 in zip(src, dst):
        assert u1.w_in == u2.w_in
        assert np.allclose(u1.state, u2.state)
        assert u1.outgoing.keys() == u2.outgoing.keys()
    assert np.allclose(plugin.face.inbox, loaded.face.inbox)


def test_dfpkg_special_case_loadable_as_plugin(tmp_path):
    """v0.7.0 .dfpkg → 通用 load_pkg 分流到 FacePlugin (首个特例)"""
    np.random.seed(13)
    gpt = CubeGPT(depth=1, dim=16, modalities=["numeric", "text"])
    gpt.step({"numeric": 1.0, "text": "classic"})
    pkg = str(tmp_path / "text.dfpkg")
    gpt.export_face("text", pkg)

    plugin, manifest = load_pkg(pkg)
    assert isinstance(plugin, FacePlugin)
    assert plugin.modality == "text" and plugin.depth == 1
    assert plugin.face is not None
    src = gpt.faces["text"].cortex._all_units_cache
    dst = plugin.face.cortex._all_units_cache
    assert len(src) == len(dst)


# ═══════════════════════════════════════════════════════════
# 8. CubeGPTKernel: 模型精简到只有必要思考
# ═══════════════════════════════════════════════════════════

def test_kernel_step_and_compat_surface(cube_kernel):
    spikes = cube_kernel.step({"numeric": 2.0, "text": "again"})
    assert isinstance(spikes, list)
    assert cube_kernel.get_output_pattern().shape == (16,)
    stats = cube_kernel.get_network_stats()
    assert stats["model"] == "CubeGPTKernel"
    assert stats["cutemamen"]["loaded_plugins"] == ["numeric", "text"]
    assert cube_kernel.kv_stack is cube_kernel.working_memory.kv_stack


def test_kernel_no_thinking_in_kernel_class():
    """内核类本身不含模态面计算单元 (精简验证)"""
    k = CubeGPTKernel(depth=1, dim=16, modalities=[])
    # 无面挂载时: 内核自身单元 = 输出头 16 个 (必要思考的最小读出)
    assert len(k._all_units) == 16
    assert k.faces == {}


def test_kernel_unknown_modality_hot_load(cube_kernel, tmp_path):
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        pkg = "text.dfpkg"
        cube_kernel.export_face("text", pkg)
        cube_kernel.unload_face("text", pkg_path=pkg)
        assert cube_kernel.list_faces() == {"loaded": ["numeric"],
                                            "registered": ["text"]}
        np.random.seed(17)
        cube_kernel.step({"numeric": 1.0, "text": "hot load"})
        assert cube_kernel.list_faces()["loaded"] == ["numeric", "text"]
    finally:
        os.chdir(cwd)


def test_kernel_import_cuteMamen_face_pkg(cube_kernel, tmp_path):
    save_pkg(cube_kernel.plugins["text"],
             str(tmp_path / "text.CuteMamen"))
    np.random.seed(19)
    fresh = CubeGPTKernel(depth=1, dim=16, modalities=["numeric"])
    manifest = fresh.import_face(str(tmp_path / "text.CuteMamen"))
    assert manifest["base_model"] == "cubegpt.face"
    assert fresh.list_faces()["loaded"] == ["numeric", "text"]
    spikes = fresh.step({"numeric": 1.0, "text": "imported"})
    assert isinstance(spikes, list)


def test_kernel_input_validation():
    k = CubeGPTKernel(depth=0, dim=16, modalities=["numeric"])
    with pytest.raises(TypeError):
        k.step(np.zeros(16))
    with pytest.raises(KeyError, match="未知输入模态"):
        k.step({"audio": 1.0})


def test_kernel_budget_evicts_face_and_restores(cube_kernel, tmp_path):
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        cube_kernel.pkg_dir = "."
        cube_kernel.memory_budget_mb = 0.0
        evicted = []
        cube_kernel.bus.subscribe("plugin.evicted",
                                  lambda e: evicted.append(e["payload"]["name"]))
        cube_kernel.step({"numeric": 1.0, "text": "tight"})
        assert evicted  # 0 MB 预算必然触发 LRU 淘汰 (只保单面)
        assert cube_kernel.list_plugins()["registered"]  # 存档进注册表
        # 放宽预算后, 下一步用到被淘汰模态时热加载回来
        cube_kernel.memory_budget_mb = None
        np.random.seed(23)
        cube_kernel.step({"numeric": 1.0, "text": "restore"})
        assert set(cube_kernel.faces) == {"numeric", "text"}
    finally:
        os.chdir(cwd)


def test_to_kernel_zero_copy():
    np.random.seed(29)
    gpt = CubeGPT(depth=1, dim=16, modalities=["numeric", "text"])
    gpt.step({"numeric": 0.7, "text": "before"})
    kernel = gpt.to_kernel()
    # 同一对象: 面 / KV 堆 / 输出头零拷贝
    assert kernel.faces["text"] is gpt.faces["text"]
    assert kernel.kv_stack is gpt.kv_stack
    assert kernel.output_module is gpt.output_module
    assert kernel.total_steps == gpt.total_steps
    # 内核形态继续运行, 且经典形态共享同一记忆
    before = len(gpt.kv_stack.entries)
    kernel.step({"numeric": 1.0, "text": "after"})
    assert len(gpt.kv_stack.entries) >= before


def test_kernel_stdp_toggle_and_reset(cube_kernel):
    cube_kernel.enable_learning(False)
    assert cube_kernel.get_stdp_stats()["learning_enabled"] is False
    cube_kernel.enable_learning(True)
    cube_kernel.reset_state()
    assert cube_kernel.total_steps == 0
    for face in cube_kernel.faces.values():
        assert np.all(face.inbox == 0)


# ═══════════════════════════════════════════════════════════
# 9. migrate-v1-to-v2 迁移工具
# ═══════════════════════════════════════════════════════════

def _make_v1_pkg(path, manifest):
    with tarfile.open(path, "w:gz") as tar:
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


def test_migrate_single_plugin(tmp_path, capsys):
    pkg = str(tmp_path / "old.CuteMamen")
    _make_v1_pkg(pkg, {
        "name": "old", "standard_version": "1.2.0",
        "model_type": "transformer", "legacy_mode": True,
        "lifecycle": {"on_init": "x", "on_think": "y"},
        "custom": 1,
    })
    rc = migrate_main([pkg, "-v"])
    assert rc == 0
    m = read_manifest(pkg)
    assert m["standard_version"] == "2.0.0"
    assert m["base_model"] == "transformer"
    assert "legacy_mode" not in m
    assert m["lifecycle"]["on_load"] == "x"
    assert m["lifecycle"]["on_unload"] == "stub"
    assert m["custom"] == 1  # 未知字段保留
    # 再迁移: 已是 v2, 幂等跳过
    rc2 = migrate_main([pkg])
    assert rc2 == 0


def test_migrate_unmappable_field_exit_codes(tmp_path):
    from src.cutemamen.pkg import read_raw_manifest
    # 非严格: 跳过无法映射字段并警告, 退出码 1 (需人工)
    pkg1 = str(tmp_path / "hard1.CuteMamen")
    _make_v1_pkg(pkg1, {"name": "hard", "standard_version": "1.0.3",
                        "pipeline_config": {"steps": []}})
    assert migrate_main([pkg1]) == 1
    # 严格: 无法自动迁移的字段报错, 退出码 1, 原文件保持 v1
    pkg2 = str(tmp_path / "hard2.CuteMamen")
    _make_v1_pkg(pkg2, {"name": "hard", "standard_version": "1.0.3",
                        "pipeline_config": {"steps": []}})
    assert migrate_main([pkg2, "--strict"]) == 1
    raw = read_raw_manifest(pkg2)
    assert raw["standard_version"] == "1.0.3"  # 未写入


def test_migrate_dry_run_no_write(tmp_path):
    from src.cutemamen.pkg import read_raw_manifest
    pkg = str(tmp_path / "dr.CuteMamen")
    _make_v1_pkg(pkg, {"name": "dr", "standard_version": "1.0.0",
                       "model_type": "t"})
    assert migrate_main([pkg, "--dry-run"]) == 0
    raw = read_raw_manifest(pkg)
    assert raw["standard_version"] == "1.0.0"  # 未写入
    assert raw["model_type"] == "t"


def test_migrate_directory_recursive_and_backup(tmp_path):
    sub = tmp_path / "plugins" / "deep"
    sub.mkdir(parents=True)
    _make_v1_pkg(str(sub / "a.CuteMamen"),
                 {"name": "a", "standard_version": "1.0.0"})
    _make_v1_pkg(str(tmp_path / "plugins" / "b.CuteMamen"),
                 {"name": "b", "standard_version": "1.0.0"})
    rc = migrate_main([str(tmp_path / "plugins"), "-r", "-b"])
    assert rc == 0
    assert read_manifest(str(sub / "a.CuteMamen"))["standard_version"] == "2.0.0"
    assert os.path.exists(str(sub / "a.CuteMamen.bak"))
    # --output 不覆盖原文件
    out = tmp_path / "migrated"
    _make_v1_pkg(str(tmp_path / "plugins" / "c.CuteMamen"),
                 {"name": "c", "standard_version": "1.0.0"})
    migrate_main([str(tmp_path / "plugins" / "c.CuteMamen"),
                  "-o", str(out)])
    assert read_manifest(str(out / "c.CuteMamen"))["standard_version"] == "2.0.0"
    assert read_manifest(
        str(tmp_path / "plugins" / "c.CuteMamen"))["standard_version"] == "1.0.0"


def test_migrate_missing_path_severe_error(tmp_path):
    assert migrate_main([str(tmp_path / "nope.CuteMamen")]) == 2


# ═══════════════════════════════════════════════════════════
# 10. RustCodingPlugin: 真实语料思考插件 (训练材料)
# ═══════════════════════════════════════════════════════════

def test_rust_plugin_training_material(kernel):
    """内置 502 段真实 Rust 语料 → 训练材料 + 5 类原型 (v0.8.4 P2 扩充)"""
    from src.cutemamen import RustCodingPlugin
    plugin = RustCodingPlugin("rust-coding")
    kernel.mount(plugin)
    assert plugin.corpus_size() == 502
    assert sorted(plugin.prototypes) == ["borrow", "lifetime", "move",
                                         "ok", "type"]
    X, y = plugin.training_data()
    assert X.shape == (502, 10) and y.shape == (502,)
    assert max(y) == 4  # 5 类
    assert plugin.memory.get("corpus_size") == 502


def test_rust_plugin_classifies_and_emits(kernel):
    """路由 + on_think 分类 + 事件总线广播"""
    from src.cutemamen import RustCodingPlugin
    plugin = RustCodingPlugin("rust-coding")
    kernel.mount(plugin)
    seen = []
    kernel.bus.subscribe("rust.classified", lambda e: seen.append(e))
    code = ("fn longest(s1: &str, s2: &str) -> &str {\n"
            "    if s1.len() > s2.len() { s1 } else { s2 }\n}")
    out = kernel.think({"topic": "rust", "data": code})
    assert out and out[0]["label"] == "lifetime"
    assert out[0]["confidence"] > 0
    assert seen and seen[0]["topic"] == "rust.classified"
    assert plugin.think_count == 1
    # 支持 {"code": ...} 字典形式; 数据面缺失时静默返回 None
    assert kernel.think({"topic": "rust",
                         "data": {"code": "let r = &String::from(\"x\");"}})[0]["label"]


def test_rust_plugin_pkg_roundtrip(tmp_path, kernel):
    """原型权重存档 → .CuteMamen → 按 base_model 热加载往返"""
    from src.cutemamen import RustCodingPlugin, load_pkg, save_pkg
    plugin = RustCodingPlugin("rust-coding")
    pkg = str(tmp_path / "rust_coding.CuteMamen")
    save_pkg(plugin, pkg)
    loaded, manifest = load_pkg(pkg)
    assert manifest["base_model"] == "rust.coding"
    assert isinstance(loaded, RustCodingPlugin)
    assert loaded.corpus_size() == 502  # 原型权重随包还原 (v0.8.4: 502 段)
    code = "let x: i32 = \"hello\";"
    assert loaded.on_think({"topic": "rust", "data": code}, None)["label"] == "type"


# ═══════════════════════════════════════════════════════════
# 11. .CuteMamen 插件标准落地: ./plugin 独立思考插件文件 (v0.8.5)
# ═══════════════════════════════════════════════════════════

_REPO_PLUGIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "plugin")


def test_discover_plugins_dir_standard(kernel, tmp_path):
    """插件目录标准: 独立包文件 → discover_plugins 注册 → 按路由主题热加载"""
    from src.cutemamen import RustCodingPlugin, save_pkg
    save_pkg(RustCodingPlugin("rust-coding"),
             str(tmp_path / "RustCoding.CuteMamen"))
    # 同目录无关文件不参与发现
    (tmp_path / "notes.txt").write_text("not a plugin")

    found = kernel.discover_plugins(str(tmp_path))
    assert list(found) == ["rust-coding"]
    assert found["rust-coding"]["base_model"] == "rust.coding"
    assert kernel.list_plugins() == {"loaded": [],
                                     "registered": ["rust-coding"]}
    assert kernel.route_index["rust"] == "rust-coding"

    # 未加载状态直接 think: topic=route ("rust" ≠ 插件名) → 随用随载
    code = ("fn longest(s1: &str, s2: &str) -> &str {\n"
            "    if s1.len() > s2.len() { s1 } else { s2 }\n}")
    out = kernel.think({"topic": "rust", "data": code})
    assert out and out[0]["label"] == "lifetime"
    assert "rust-coding" in kernel.plugins
    assert kernel.list_plugins()["registered"] == []


def test_shipped_rust_coding_pkg_routed_by_cubegpt():
    """CubeGPT 路由到仓库交付的独立思考插件文件 plugin/RustCoding.CuteMamen"""
    from src.cutemamen import RustCodingPlugin
    pkg = os.path.join(_REPO_PLUGIN_DIR, "RustCoding.CuteMamen")
    assert os.path.isfile(pkg), "plugin/RustCoding.CuteMamen 必须独立交付"

    np.random.seed(31)
    gpt = CubeGPTKernel(depth=1, dim=16, modalities=["numeric"])
    found = gpt.discover_plugins(_REPO_PLUGIN_DIR)
    assert found["rust-coding"]["route"] == "rust"
    assert "rust-coding" not in gpt.plugins  # 注册未加载 (随用随载)

    out = gpt.think({"topic": "rust", "data": "let x: i32 = \"hello\";"})
    assert out and out[0]["label"] == "type"
    assert isinstance(gpt.plugins["rust-coding"], RustCodingPlugin)
    # 内核必要思考不受插件加载影响
    spikes = gpt.step({"numeric": 1.0})
    assert isinstance(spikes, list)


# ═══════════════════════════════════════════════════════════
# 12. 新阶段 · 分布式架构: 主模型知识迁移 (v0.9.0)
# ═══════════════════════════════════════════════════════════

def _small_train_samples(n_per_class: int = 6):
    """真实语料小子集 (每类 n 段), 供迁移测试快速运行"""
    from src.data.real_dataset import RustCodingTrainingDataset
    dataset = RustCodingTrainingDataset(dim=16)
    train, _ = dataset.generate_dataset(train_ratio=0.98, seed=0)
    picked, seen = [], {}
    for s in train:
        c = s.category
        if seen.get(c, 0) < n_per_class:
            picked.append(s)
            seen[c] = seen.get(c, 0) + 1
    return picked


def test_migrate_from_main_model_priority_and_determinism(kernel):
    """主模型知识迁移后: 读出层优先于原型, 且同 seed 逐位可复现"""
    from src.cutemamen import RustCodingPlugin
    code = "let x: i32 = \"hello\";"
    plugin = RustCodingPlugin("rust-coding")
    kernel.mount(plugin)
    before = kernel.think({"topic": "rust", "data": code})[0]
    assert before["source"] == "corpus-prototypes"  # 未迁移: 原型回退

    plugin.migrate_from_main_model(_small_train_samples(), seed=0)
    after = kernel.think({"topic": "rust", "data": code})[0]
    assert after["source"] == "main-model-readout"
    assert after["label"] in ("type", "move")  # 该样本真实类别: type
    assert plugin.memory.recall("knowledge_source") == "main-model-readout"

    # 同 seed 重新迁移 → 推理结果逐位一致 (确定性, v0.9.0 修复验证)
    plugin2 = RustCodingPlugin("rust-coding")
    plugin2.migrate_from_main_model(_small_train_samples(), seed=0)
    again = plugin2.on_think({"topic": "rust", "data": code}, None)
    assert again["label"] == after["label"]
    assert abs(again["confidence"] - after["confidence"]) < 1e-12


def test_migrated_knowledge_survives_pkg_roundtrip(tmp_path):
    """迁移知识随 .CuteMamen 存档往返: 读出权重逐位还原, 推理一致"""
    from src.cutemamen import RustCodingPlugin, load_pkg, save_pkg
    plugin = RustCodingPlugin("rust-coding")
    plugin.migrate_from_main_model(_small_train_samples(), seed=0)
    pkg = str(tmp_path / "RustCoding.CuteMamen")
    manifest = save_pkg(plugin, pkg)
    assert manifest["knowledge_source"] == "main-model-readout"
    assert manifest["readout"]["n_classes"] == 5

    loaded, m2 = load_pkg(pkg)
    assert m2["knowledge_source"] == "main-model-readout"
    assert loaded.readout is not None
    code = "fn longest(s1: &str, s2: &str) -> &str { s1 }"
    out_src = plugin.on_think({"topic": "rust", "data": code}, None)
    out_dst = loaded.on_think({"topic": "rust", "data": code}, None)
    assert out_dst["source"] == "main-model-readout"
    assert out_dst["label"] == out_src["label"]
    assert abs(out_dst["confidence"] - out_src["confidence"]) < 1e-12
    assert loaded.stats()["knowledge_source"] == "main-model-readout"


def test_shipped_pkg_carries_migrated_knowledge():
    """仓库交付的 plugin/RustCoding.CuteMamen 携带主模型迁移知识"""
    from src.cutemamen import read_manifest
    m = read_manifest(os.path.join(_REPO_PLUGIN_DIR, "RustCoding.CuteMamen"))
    assert m["knowledge_source"] == "main-model-readout"
    assert m["readout"]["n_features"] > 0
    assert m["corpus_size"] == 502
