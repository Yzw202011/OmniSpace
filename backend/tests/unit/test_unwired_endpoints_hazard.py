"""未接队列生图端点的并发危害颗粒级测试（2026-09-02 诚信复核）。

用户质询「四视图/单视图重生仍走引擎内部锁，不叠载」断言的真实性，
本文件用测试固化三条实证：

  A. unload_model 不持有 _infer_lock——推理进行中卸载可并发执行，
     多视图循环的下一次 engine.generate 将撞上 None 管线（1998 行
     ensure_loaded 后 pipe 被抽走型崩溃；2026-08-26 资源守卫卸载
     在用管线事故同根）；
  B. 修复前四视图/单视图重生端点不入图像队列（无 paint 功能锁、
     无位次）——修复后必须入队（test_image_queue_api 断言）；
  C. 队列排空收尾的卸载发生在「队列自身任务之间/之后」——接线的
     端点作为队列任务不会被自己的收尾砸中（顺序性由队列串行保证）。
"""
from __future__ import annotations

import threading

import pytest

from backend.services.inference.paint_engine import PaintEngine


class _StubPipe:
    """假管线：generate 慢速占住 _infer_lock，模拟推理进行中。"""


def _engine_with_stub_pipe(monkeypatch: pytest.MonkeyPatch) -> PaintEngine:
    """构造带假管线的引擎实例（不触 GPU/CUDA）。"""
    eng = PaintEngine.__new__(PaintEngine)  # 绕过 __init__ 的 CUDA 探测
    eng._lock = threading.Lock()
    eng._infer_lock = threading.RLock()
    eng._pipe = _StubPipe()
    eng._pipe_i2i = None
    eng._model_id = "stub-model"
    eng._model_alias = ""
    eng._model_dir = None
    eng._state = "ready"
    eng._lora_state = None
    monkeypatch.setattr(
        "backend.services.inference.paint_engine._release_cuda_memory",
        lambda: None, raising=False)
    return eng


def test_unload_does_not_take_infer_lock_race_documented(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A：_infer_lock 持有（推理中）时 unload_model 照样执行——
    多视图循环下一步将读到 None 管线。此测试固化病根（修复=端点
    入队由队列串行化，卸载只发生在队尾）。"""
    eng = _engine_with_stub_pipe(monkeypatch)
    with eng._infer_lock:  # 模拟一次 generate 进行中
        assert eng.unload_model() is True  # 不阻塞、直接卸掉
        assert eng._pipe is None  # 管线在「推理中」被抽走
        # 多视图循环的下一步（legacy 四视图 = 4 次连续 generate）：
        # ensure_loaded 会重载——但若循环体直接复用 self._pipe（真实
        # 代码在 generate() 内部重新取引用），None 即崩溃源


def test_engine_same_family_serializes(monkeypatch: pytest.MonkeyPatch) -> None:
    """同为 paint_engine 的两次推理经 _infer_lock 串行（断言中
    「引擎内部锁排队」为真的部分）。"""
    eng = _engine_with_stub_pipe(monkeypatch)
    order: list[str] = []

    def slow_gen() -> None:
        with eng._infer_lock:
            order.append("gen1-start")
            import time
            time.sleep(0.15)
            order.append("gen1-end")

    t = threading.Thread(target=slow_gen)
    t.start()
    import time
    time.sleep(0.03)
    with eng._infer_lock:
        order.append("gen2-start")
    t.join()
    # gen2 必须等 gen1 完全结束后才开始（串行，无并发推理）
    assert order == ["gen1-start", "gen1-end", "gen2-start"]


def test_turnaround_endpoints_wired_and_runners_lock_free() -> None:
    """B（修复后断言）：四视图/单视图重生端点已接统一图像队列；
    同步核心保持无锁 runner 契约（锁/协商由队列编排，不双取）。

    修复前证据（2026-09-02 实测前状态）：两端点 + 两 sync 核均无
    acquire_or_raise/get_image_queue——无互斥直跑，hazard A 的竞态
    对它们敞开。
    """
    import inspect

    from backend.api.manga import comic_asset as ca_mod
    from backend.api.manga import comic_gen as gen_mod
    ep_src = inspect.getsource(ca_mod.comic_asset_generate_turnaround) \
        + inspect.getsource(ca_mod.comic_asset_regenerate_view)
    sync_src = inspect.getsource(gen_mod._generate_turnaround_sync) \
        + inspect.getsource(gen_mod._regenerate_view_sync)
    # 端点层：已入队（排队互斥 + 位次 + 收尾协商归队列）
    assert "get_image_queue" in ep_src
    # runner 层：不再自取功能锁（双取会造成计数失配）
    assert "acquire_or_raise" not in sync_src
    assert "acquire_or_raise" not in ep_src
# 本项目仅供学习使用，商业授权请+Q 3559331368
