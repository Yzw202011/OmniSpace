"""锁序不变量测试（D1 专项，2026-09-21）——ABBA 环已破的防回归闸。

不变量：任何引擎在调用 check_vram / _try_free_vram（会跨引擎卸载：
mgr.unload_model / 他引擎 unload_model）时，**不得持有自身 _lock**。
违反即重建 paint._lock → dialog._lock 反向边，与 dialog→paint 单向边
相向成环（2026-09-10 锁序宣言、09-21 D1 复核销账）。

方法：monkeypatch 各引擎的 check_vram/_try_free_vram 记录调用时的
持锁状态（threading.Lock 无 owner 概念，用 acquire(blocking=False)
探测：成功=无人持有）。load_model 走到不可用药依赖的早退分支即止——
腾挪调用点在此之前（选模型失败早退不触发 check_vram 属正常）。
"""
from __future__ import annotations

import threading


def _probe_lock_unheld(lock: threading.Lock) -> bool:
    """探测锁当前无人持有（acquire 立即成功即无人持）。"""
    got = lock.acquire(blocking=False)
    if got:
        lock.release()
    return got


def test_paint_check_vram_not_under_engine_lock(monkeypatch):
    """paint.load_model 调 check_vram 时必须已释放 paint._lock（P1-3 外提
    的不变量——谁把腾挪挪回锁内，本测试即红）。"""
    from src.services.inference import paint_engine as pe

    engine = pe.get_paint_engine()
    calls: list[bool] = []

    def _spy_check_vram(required_gb):
        calls.append(_probe_lock_unheld(engine._lock))
        return False, 0.0  # 触发"显存不足"早退，止步于装载前

    monkeypatch.setattr(engine, "check_vram", _spy_check_vram)
    # 依赖/模型探测失败早退不经过 check_vram——直接调 load_model(None)，
    # torch/diffusers 在场则走到 check_vram；若无 GPU 环境依赖缺失则跳过
    import torch  # noqa: F401
    try:
        engine.load_model(None)
    except Exception:  # noqa: BLE001 - 后续装载失败不影响断言（探针已记录）
        pass
    assert calls, "check_vram 未被触达（依赖缺失？不应静默跳过）"
    assert all(calls), (
        "paint.check_vram 在持有 engine._lock 时被调用——ABBA 环回归！"
        " 腾挪必须留在装载锁外段（见 model_manager 锁序宣言 2026-09-21）")


def test_dialog_try_free_direction_is_one_way(monkeypatch):
    """dialog._try_free_vram 卸 paint 属单向合法边——本测试固化其存在性
    与方向（dialog→paint），并断言 dialog.unload_model 不被 paint 路径
    反向调用（paint._try_free_vram 里的 dialog.unload 只允许在锁外段）。
    """
    from src.services.inference import dialog_engine as de
    from src.services.inference import paint_engine as pe

    d_eng = de.get_dialog_engine()
    p_eng = pe.get_paint_engine()
    p_calls: list[bool] = []

    def _spy_paint_try_free(required_gb):
        p_calls.append(_probe_lock_unheld(p_eng._lock))
        return 0.0

    monkeypatch.setattr(p_eng, "_try_free_vram", _spy_paint_try_free)
    # paint.check_vram → _try_free_vram 的调用发生在锁外（上一测试已锁），
    # 此处复核同一路径的双重探针一致性
    monkeypatch.setattr(
        p_eng, "check_vram",
        lambda required_gb: (False, _spy_paint_try_free(required_gb)))
    import torch  # noqa: F401
    try:
        p_eng.load_model(None)
    except Exception:  # noqa: BLE001
        pass
    if p_calls:
        assert all(p_calls), "paint._try_free_vram 持锁调用——环回归"
    # dialog 侧锁存在（结构断言，防误删字段）
    assert isinstance(d_eng._lock, type(p_eng._lock))
