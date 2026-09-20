"""空闲显存归零（批1，2026-09-20）测试。

覆盖三路：
1. deflate_cuda_pool restore=False 停靠不回卡（空闲归零核心语义）；
   默认参数行为不变（回卡）+ 4GB 门槛对默认调用方保留。
2. SET-010 假开关接线：_clear_cache_on_unload_enabled 读
   system_settings 同源同表（缺键/异常按 True 安全默认）；
   unload_model 按开关跳过/执行 empty_cache。
3. bge 惰性回卡：停靠 CPU 后首次 embed/_embed_batch 搬回 CUDA；
   CUDA 不可用时留 CPU 慢速可用；park/restore 与 encode 经
   _device_lock 串行化。

torch 以 sys.modules 桩替（真 CUDA 不可入单测——WDDM 读数假绿）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import types

import pytest

from src.data.vector_db import VectorDB
from src.services import model_manager as mm
from src.services.model_manager import ModelManager

# ── 桩件 ──────────────────────────────────────────────────────

class _FakeCuda:
    def __init__(self, avail: bool = True,
                 reserved_seq: list[int] | None = None) -> None:
        self._avail = avail
        self._seq = list(reserved_seq or [])
        self.empty_cache_calls = 0

    def is_available(self) -> bool:
        return self._avail

    def memory_reserved(self, device: int = 0) -> int:
        return self._seq.pop(0) if self._seq else 0

    def empty_cache(self) -> None:
        self.empty_cache_calls += 1


class _FakeTorch:
    def __init__(self, cuda: _FakeCuda) -> None:
        self.cuda = cuda


class _FakeVdb:
    """deflate 协作桩：记录 park/restore 调用序。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def park_embed_model(self) -> bool:
        self.calls.append("park")
        return True

    def restore_embed_model(self) -> None:
        self.calls.append("restore")


class _FakeVec:
    def tolist(self) -> list[float]:
        return [0.1, 0.2]


class _FakeEmbedModel:
    def __init__(self, device: str = "cpu") -> None:
        self.device = device
        self.encode_calls: list[object] = []

    def to(self, dev: str) -> None:
        self.device = dev

    def encode(self, text, normalize_embeddings: bool = True):
        self.encode_calls.append(text)
        return _FakeVec()


class _FakeDB:
    def __init__(self, row: dict | None) -> None:
        self._row = row

    def query_one(self, sql: str, params=()) -> dict | None:
        return self._row


@pytest.fixture()
def fake_torch(monkeypatch):
    cuda = _FakeCuda(avail=True)
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch(cuda))
    return cuda


@pytest.fixture()
def fake_vdb(monkeypatch):
    vdb = _FakeVdb()
    monkeypatch.setattr("src.data.vector_db.get_vector_db", lambda: vdb)
    return vdb


def _gb(n: float) -> int:
    return int(n * 1024 ** 3)


# ── 1. deflate_cuda_pool：restore 参数 ────────────────────────

def test_deflate_restore_false_keeps_bge_parked(monkeypatch, fake_torch,
                                                fake_vdb):
    """空闲归零：restore=False 时停靠后不回卡，池清空释放量如实返回。"""
    fake_torch._seq = [_gb(5.0), _gb(0.2)]  # 压缩前 / 压缩后
    released = mm.deflate_cuda_pool(0.0, restore=False)
    assert fake_vdb.calls == ["park"]          # 只停不回
    assert fake_torch.empty_cache_calls == 1
    assert released == pytest.approx(4.8, abs=0.01)


def test_deflate_default_still_restores(monkeypatch, fake_torch, fake_vdb):
    """默认参数行为不变：停 → 清 → 回卡（既有调用方语义保持）。"""
    fake_torch._seq = [_gb(5.0), _gb(1.3)]
    mm.deflate_cuda_pool(0.0)
    assert fake_vdb.calls == ["park", "restore"]


def test_deflate_threshold_preserved_for_default_callers(monkeypatch,
                                                         fake_torch,
                                                         fake_vdb):
    """4GB 门槛对默认调用方保留：池已干净则跳过停靠（省 2-4s）。"""
    fake_torch._seq = [_gb(1.0)]
    released = mm.deflate_cuda_pool()
    assert fake_vdb.calls == []
    assert released == 0.0
    assert fake_torch.empty_cache_calls == 0


def test_deflate_no_cuda_noop(monkeypatch, fake_vdb):
    """无 CUDA（核显机）：直接 0.0，不碰停靠协作。"""
    cuda = _FakeCuda(avail=False)
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch(cuda))
    assert mm.deflate_cuda_pool(0.0, restore=False) == 0.0
    assert fake_vdb.calls == []


# ── 2. SET-010 开关接线 ───────────────────────────────────────

def _patch_db(monkeypatch, row: dict | None) -> None:
    monkeypatch.setattr("src.data.database.get_db_safe",
                        lambda: _FakeDB(row))


def test_knob_false_reads_from_system_settings(monkeypatch):
    row = {"value": json.dumps({"clear_cache_on_unload": False})}
    _patch_db(monkeypatch, row)
    assert mm._clear_cache_on_unload_enabled() is False


def test_knob_true_and_safe_defaults(monkeypatch):
    row = {"value": json.dumps({"clear_cache_on_unload": True})}
    _patch_db(monkeypatch, row)
    assert mm._clear_cache_on_unload_enabled() is True
    _patch_db(monkeypatch, None)                     # 无记录
    assert mm._clear_cache_on_unload_enabled() is True
    monkeypatch.setattr("src.data.database.get_db_safe", lambda: None)
    assert mm._clear_cache_on_unload_enabled() is True  # DB 不可用


def test_knob_corrupt_value_falls_back_true(monkeypatch):
    _patch_db(monkeypatch, {"value": "{not-json"})
    assert mm._clear_cache_on_unload_enabled() is True


def _bare_manager(monkeypatch, entry: dict) -> tuple[ModelManager, _FakeCuda]:
    """裸 ModelManager（不走重 __init__）：只喂 unload_model 依赖。

    2026-09-20 卫生修复：全部经 monkeypatch（自动还原）——原裸赋值
    `_get_engine = lambda` 残留在共享单例上，毒害后续 lifecycle 测试
    （ensure_loaded 意外走"无引擎类别"旁路返回 True）。
    """
    mgr = ModelManager.__new__(ModelManager)
    monkeypatch.setattr(mgr, "_loaded_lock", threading.Lock(), raising=False)
    monkeypatch.setattr(mgr, "_loaded", {"m1": entry}, raising=False)
    monkeypatch.setattr(mgr, "_user_load_pins", {}, raising=False)
    monkeypatch.setattr(mgr, "_vram_lock", threading.Lock(), raising=False)
    monkeypatch.setattr(mgr, "_reserved_vram_gb", 0.0, raising=False)
    monkeypatch.setattr(mgr, "_get_engine", lambda category: None,
                        raising=False)
    # 引擎侧单例跟踪器桩替（避免真 NVML/记账副作用）
    monkeypatch.setitem(
        sys.modules, "src.engines.vram_manager",
        types.SimpleNamespace(get_vram_manager=lambda: types.SimpleNamespace(
            track_free=lambda mid: None)))
    monkeypatch.setitem(
        sys.modules, "src.engines.memory_manager",
        types.SimpleNamespace(get_memory_manager=lambda: (
            types.SimpleNamespace(unregister=lambda key: None))))
    cuda = _FakeCuda(avail=True)
    monkeypatch.setattr(mm, "_torch", _FakeTorch(cuda))
    return mgr, cuda


_ENTRY = {"category": "paint", "engine": "ensure_loaded", "vram_gb": 4.0}


def test_unload_skips_empty_cache_when_knob_off(monkeypatch):
    mgr, cuda = _bare_manager(monkeypatch, dict(_ENTRY))
    monkeypatch.setattr(mm, "_clear_cache_on_unload_enabled",
                        lambda: False)
    assert mgr.unload_model("m1") is True
    assert cuda.empty_cache_calls == 0                  # 开关关：不清池


def test_unload_clears_cache_when_knob_on(monkeypatch):
    mgr, cuda = _bare_manager(monkeypatch, dict(_ENTRY))
    monkeypatch.setattr(mm, "_clear_cache_on_unload_enabled",
                        lambda: True)
    assert mgr.unload_model("m1") is True
    assert cuda.empty_cache_calls == 1                  # 开关开：清池


# ── 3. bge 惰性回卡（空闲归零后续） ───────────────────────────

def _bare_vdb(model: _FakeEmbedModel) -> VectorDB:
    vdb = VectorDB.__new__(VectorDB)
    vdb._device_lock = threading.Lock()
    vdb._embed_model = model
    vdb._get_embed_model = lambda: model               # noqa: E731
    return vdb


def test_embed_lazy_restores_to_cuda(monkeypatch):
    """停靠 CPU 后首次检索：先惰性回卡再 encode，语义向量返回。"""
    cuda = _FakeCuda(avail=True)
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch(cuda))
    model = _FakeEmbedModel(device="cpu")
    vdb = _bare_vdb(model)
    vec = vdb.embed("你好")
    assert model.device == "cuda"                       # 已回卡
    assert model.encode_calls == ["你好"]
    assert vec == [0.1, 0.2]


def test_embed_batch_lazy_restores(monkeypatch):
    cuda = _FakeCuda(avail=True)
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch(cuda))
    model = _FakeEmbedModel(device="cpu")
    vdb = _bare_vdb(model)
    vecs = vdb._embed_batch(["a", "b"])
    assert model.device == "cuda"
    assert len(vecs) == 2


def test_embed_stays_cpu_without_cuda(monkeypatch):
    """核显机（无 CUDA）：留 CPU 慢速 encode，不报错不降级哈希。"""
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch(_FakeCuda(False)))
    model = _FakeEmbedModel(device="cpu")
    vdb = _bare_vdb(model)
    vec = vdb.embed("hello")
    assert model.device == "cpu"
    assert vec == [0.1, 0.2]


def test_embed_on_cuda_skips_move(monkeypatch):
    """已在卡上：惰性回卡为 no-op（无多余搬移开销）。"""
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch(_FakeCuda(True)))
    model = _FakeEmbedModel(device="cuda:0")
    vdb = _bare_vdb(model)
    vdb.embed("x")
    assert model.device == "cuda:0"


def test_park_and_restore_move_device(monkeypatch):
    """停靠/回卡搬设备（与惰性回卡三态闭环）。"""
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch(_FakeCuda(True)))
    model = _FakeEmbedModel(device="cuda:0")
    vdb = _bare_vdb(model)
    assert vdb.park_embed_model() is True
    assert model.device == "cpu"
    vdb.restore_embed_model()
    assert model.device == "cuda"


# ── 4. 并发压测：设备锁真线程碰撞（补 2026-09-20 批1 🔶 边界） ──

class _SlowFakeEmbedModel(_FakeEmbedModel):
    """加宽碰撞窗口：to()/encode 各睡 0.5ms，让线程真撞上。

    encode 额外记录当刻设备——设备锁正确时（CUDA 可用）应为 cuda，
    若出现 cpu 即锁失效（搬移撞上在飞 encode）。
    """

    def __init__(self, device: str = "cpu") -> None:
        super().__init__(device)
        self.encode_devices: list[str] = []

    def to(self, dev: str) -> None:
        time.sleep(0.0005)
        super().to(dev)

    def encode(self, text, normalize_embeddings: bool = True):
        time.sleep(0.0005)
        self.encode_devices.append(str(self.device))
        return super().encode(text, normalize_embeddings)


def _run_threads(targets: list) -> list:
    """并发起跑（Barrier 对齐）+ 死锁护栏（join 超时即失败）。"""
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(targets))

    def _wrap(fn):
        def run():
            barrier.wait(timeout=10)
            for _ in range(fn.iters):
                try:
                    fn.body()
                except BaseException as exc:  # noqa: BLE001 - 线程内异常回收
                    errors.append(exc)
                    return
        return run

    threads = [threading.Thread(target=_wrap(t)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    alive = [t for t in threads if t.is_alive()]
    assert not alive, f"线程未收尾（疑似死锁）: {len(alive)} 个存活"
    return errors


def _stress(vdb: VectorDB, model: _SlowFakeEmbedModel,
            park_mode: str) -> None:
    """压测骨架：4 编码线程 × 300 次 vs 2 搬移线程 × 200 周期。

    不变式（设备锁正确性）：
    1. 线程内零异常（含死锁护栏）；
    2. embed 永不降级哈希（哈希降级不抛异常，须按返回值断言——
       语义静默丢失是本锁要根治的病）；
    3. 每次encode 发生在 cuda 上（CUDA 可用时锁内惰性回卡原子生效）。
    """
    class _T:
        def __init__(self, body, iters):
            self.body, self.iters = body, iters

    bad_vecs: list = []

    def _embed_once(tag: str) -> None:
        vec = vdb.embed(tag)
        if vec != [0.1, 0.2]:
            bad_vecs.append(vec)

    embedders = [_T(lambda i=i: _embed_once(f"q{i}"), 300) for i in range(4)]
    if park_mode == "restore":
        movers = [_T(lambda: (vdb.park_embed_model(),
                              vdb.restore_embed_model()), 200)
                  for _ in range(2)]
    else:  # idle_zero：deflate restore=False 语义（只停不回）
        movers = [_T(lambda: vdb.park_embed_model(), 200) for _ in range(2)]

    errors = _run_threads(embedders + movers)
    assert not errors, f"线程内异常 ×{len(errors)}: {errors[:3]}"
    assert model.encode_calls, "应有 encode 发生（压测空转=假绿）"
    assert not bad_vecs, (
        f"embed 降级哈希 ×{len(bad_vecs)}（真嵌入向量被静默替换）")
    cpu_encodes = [d for d in model.encode_devices
                   if not d.startswith("cuda")]
    assert not cpu_encodes, (
        f"锁失效：{len(cpu_encodes)}/{len(model.encode_devices)} 次 encode "
        f"发生在 cpu（搬移撞上在飞编码）")


def test_stress_park_restore_cycle_vs_embed(monkeypatch):
    """场景A：deflate 停→回循环 对 4 路并发检索（restore=True 语义）。"""
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch(_FakeCuda(True)))
    model = _SlowFakeEmbedModel(device="cuda:0")
    vdb = _bare_vdb(model)
    _stress(vdb, model, park_mode="restore")


def test_stress_idle_zero_park_vs_embed(monkeypatch):
    """场景B：空闲归零（只停不回）对 4 路并发检索——每次检索惰性回卡。"""
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch(_FakeCuda(True)))
    model = _SlowFakeEmbedModel(device="cpu")
    vdb = _bare_vdb(model)
    _stress(vdb, model, park_mode="idle_zero")
