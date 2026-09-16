"""P1-2 哨兵（审计 09-10 锁语义五连）：卸载必须等待在途推理收尾。

旧病：unload_model 只取 `_lock`，推理在 `_infer_lock` 下运行——模块
切换/漫剧让渡触发卸载时，采样中管线引用被抽走（中途崩/黑图）。
修法：unload 先取 `_infer_lock`（锁序全局约定：先 _infer_lock 后 _lock）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import threading

from src.services.inference import paint_engine as pe_mod
from src.services.inference.paint_engine import PaintEngine


def _bare_engine(monkeypatch) -> PaintEngine:
    """绕过 __init__（不触发 diffusers/依赖），只装配锁语义所需的字段。"""
    eng = PaintEngine.__new__(PaintEngine)
    eng._lock = threading.Lock()
    eng._infer_lock = threading.RLock()
    eng._pipe = object()          # 非空 = 有模型在载
    eng._pipe_i2i = None
    eng._model_id = "sdxl-test"   # 非 qwen-image，避开流式卸载分支
    eng._model_alias = ""
    eng._model_dir = None
    eng._state = "ready"
    monkeypatch.setattr(eng, "_reset_lora_state", lambda: None)
    return eng


def test_unload_waits_for_inflight_inference(monkeypatch) -> None:
    monkeypatch.setattr(pe_mod, "_release_cuda_memory", lambda: None)
    monkeypatch.setattr(pe_mod, "_cuda_free_gb", lambda: 0.0)

    eng = _bare_engine(monkeypatch)
    result: dict = {}

    with eng._infer_lock:  # 模拟在途推理持推理锁
        t = threading.Thread(target=lambda: result.update(
            unloaded=eng.unload_model()))
        t.start()
        t.join(0.5)
        assert t.is_alive(), (
            "unload_model 未等待在途推理即卸载（P1-2 修复回潮）")
    t.join(5)
    assert not t.is_alive(), "推理收尾后卸载仍未完成（死锁？）"
    assert result.get("unloaded") is True
    assert eng._state == "unloaded"


def test_unload_without_inference_is_immediate(monkeypatch) -> None:
    """无在途推理时卸载不应被额外阻塞（回归保护）。"""
    monkeypatch.setattr(pe_mod, "_release_cuda_memory", lambda: None)
    monkeypatch.setattr(pe_mod, "_cuda_free_gb", lambda: 0.0)

    eng = _bare_engine(monkeypatch)
    assert eng.unload_model() is True
    assert eng._pipe is None
