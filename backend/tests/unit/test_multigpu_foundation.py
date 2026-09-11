"""批1 多卡地基：mock 双卡单测 + 单卡回归哨兵（2026-09-05）。

三条防线（实施计划 docs/新模块与多显卡实施计划-2026-09-05.md §1.7）：
1. 单卡快速路径哨兵——可用设备 ≤1 时资源域分配恒主卡，功能锁/显存
   账本语义与历史逐比特一致（本批安全网铁律）；
2. mock 双卡行为——同域互斥、异域并行、坏配置回落、paint/video_gen
   同卡收敛、按卡记账与按卡重置；
3. 引擎绑卡接线哨兵——vLLM/ComfyUI 子进程必须显式设
   CUDA_VISIBLE_DEVICES、显存闸门不得锚死 0 号卡（源码断言防接线被删）。

⚠️ 实机双卡验证缺口：本机单卡（RTX 5070 Ti），多卡路径仅 mock 背书；
   实弹验证待双卡/专业卡机器到位（实施计划 §4 风险1，永久待办）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.engines import gpu_domains
from backend.engines.vram_manager import VramManager
from backend.middleware.error_handler import ApiError
from backend.middleware.feature_lock import FeatureLockManager

_ROOT = Path(__file__).resolve().parents[3]

# 双卡分配映射（dialog/training→卡0，paint/video_gen→卡1）
_DOMAIN_MAP = {"dialog": "0", "paint": "1", "video_gen": "1",
               "training": "0"}


# ── 工具 ────────────────────────────────────────────────────────

def _dual(monkeypatch: pytest.MonkeyPatch,
          cfg: dict[str, int] | None = None) -> None:
    """注入 mock 双卡环境（0/1 两卡 + 指定分配表）。"""
    monkeypatch.setattr(gpu_domains, "_device_indexes", lambda: [0, 1])
    monkeypatch.setattr(gpu_domains, "feature_devices_config",
                        lambda: dict(cfg or {}))
    monkeypatch.setattr(gpu_domains, "primary_device", lambda: 0)


def _fl(monkeypatch: pytest.MonkeyPatch,
        mapping: dict[str, str]) -> FeatureLockManager:
    """全新 FeatureLockManager 实例 + 指定域映射（不碰全局单例）。"""
    from backend.middleware import feature_lock as fl_mod
    monkeypatch.setattr(fl_mod, "_domain_of",
                        lambda f: mapping.get(f, "0"))
    return FeatureLockManager()


# ── 1. 单卡快速路径（逐比特旧行为铁律）──────────────────────────

def test_single_card_always_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """单卡机器：即使配置写了别的卡号，也必须全部走主卡 0。"""
    monkeypatch.setattr(gpu_domains, "_device_indexes", lambda: [0])
    monkeypatch.setattr(gpu_domains, "feature_devices_config",
                        lambda: {"dialog": 1, "paint": 1, "video_gen": 1,
                                 "training": 1})
    for feat in ("dialog", "paint", "video_gen", "training"):
        assert gpu_domains.resolve_feature_device(feat) == 0, (
            f"单卡机器 {feat} 必须走主卡（逐比特旧行为铁律）")
    # 未知类别（embedding/voice 等小模型）同样主卡
    assert gpu_domains.resolve_feature_device("embedding") == 0


def test_real_domain_resolution_single_card(monkeypatch) -> None:
    """真实链路（本机单卡、远程关闭钉死）：域解析恒主卡；功能锁单卡
    互斥语义与历史一致（真实 _domain_of，不打 mock）。"""
    import backend.services.inference.backends.remote_backend as rb
    monkeypatch.setattr(rb, "_read_settings_kv", lambda: {})
    monkeypatch.setattr(rb, "_cfg_cache", None)
    # 同步隔离槽位绑定 KV（实弹验收会在库里留下真绑定，必须钉死空场）
    import backend.services.cloud_provider_service as cps
    monkeypatch.setattr(cps, "_read_kv", lambda key, default=None: default)
    monkeypatch.setattr(cps, "_write_kv", lambda key, value: None)
    monkeypatch.setattr(cps, "_endpoint_cache", None)
    assert gpu_domains.resolve_feature_device("dialog") == 0
    assert gpu_domains.resolve_feature_domain("paint") == "0"
    m = FeatureLockManager()
    assert asyncio.run(m.acquire("dialog")) is True
    assert asyncio.run(m.acquire("paint")) is False
    assert m.get_block_reason("paint") is not None
    assert m.get_block_reason("dialog") is None
    asyncio.run(m.release("dialog"))
    assert asyncio.run(m.acquire("paint")) is True
    asyncio.run(m.release("paint"))


# ── 2. mock 双卡行为 ─────────────────────────────────────────────

def test_dual_card_assignment(monkeypatch: pytest.MonkeyPatch) -> None:
    _dual(monkeypatch, {"dialog": 0, "paint": 1, "video_gen": 1,
                        "training": 0})
    assert gpu_domains.resolve_feature_device("dialog") == 0
    assert gpu_domains.resolve_feature_device("paint") == 1
    assert gpu_domains.resolve_feature_device("video_gen") == 1
    assert gpu_domains.resolve_feature_device("training") == 0
    assert gpu_domains.resolve_feature_domain("paint") == "1"


def test_dual_card_missing_entry_falls_back_primary(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """多卡但无分配配置：全部回落主卡（保守=现状）。"""
    _dual(monkeypatch, {})
    for feat in ("dialog", "paint", "video_gen", "training"):
        assert gpu_domains.resolve_feature_device(feat) == 0


def test_bad_index_falls_back_to_primary(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """配置指向不存在的卡：回落主卡（绝不允许因配置错误起不来）。"""
    _dual(monkeypatch, {"paint": 5, "dialog": 0})
    assert gpu_domains.resolve_feature_device("paint") == 0
    assert gpu_domains.resolve_feature_device("dialog") == 0


def test_paint_video_gen_same_card_enforced(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """paint/video_gen 异卡配置被自动收敛到 paint 的卡（共用 ComfyUI
    单实例约束）。"""
    _dual(monkeypatch, {"paint": 1, "video_gen": 0})
    assert gpu_domains.resolve_feature_device("paint") == 1
    assert gpu_domains.resolve_feature_device("video_gen") == 1


def test_lock_single_domain_reentry_semantics(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """单域（=历史单卡）语义：同功能可重入、计数归零才放锁。"""
    m = _fl(monkeypatch, {})
    assert asyncio.run(m.acquire("dialog", task_id="t1")) is True
    assert asyncio.run(m.acquire("dialog")) is True
    asyncio.run(m.release("dialog"))
    assert m.active_feature == "dialog"  # 还剩一次重入，锁不释放
    asyncio.run(m.release("dialog"))
    assert m.active_feature is None
    st = m.status()
    assert st["active_feature"] is None
    assert st["holders"] == []


def test_lock_dual_domain_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    """双卡核心语义：异域并行、同域互斥、兼容快照取主卡域持有者。"""
    m = _fl(monkeypatch, _DOMAIN_MAP)
    assert asyncio.run(m.acquire("dialog", task_id="chat")) is True   # 域0
    assert asyncio.run(m.acquire("paint", task_id="draw")) is True    # 域1 并行
    assert asyncio.run(m.acquire("video_gen")) is False               # 域1 被 paint 占
    assert asyncio.run(m.acquire("training")) is False                # 域0 被 dialog 占
    assert m.get_block_reason("video_gen") is not None
    assert m.get_block_reason("dialog") is None                       # 同功能不阻断
    assert m.is_feature_active("paint") is True
    st = m.status()
    assert st["active_feature"] == "dialog"          # 主卡域优先（兼容快照）
    assert st["task_id"] == "chat"
    assert {h["feature"] for h in st["holders"]} == {"dialog", "paint"}
    assert {h["domain"] for h in st["holders"]} == {"0", "1"}
    asyncio.run(m.release("dialog"))
    asyncio.run(m.release("paint"))
    assert m.active_feature is None


def test_lock_domain_param_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式域参数（批3 远程引擎的 REMOTE_DOMAIN 预留）：与本地域互不
    阻断，远程对话与本地绘画天然并行。"""
    m = _fl(monkeypatch, {})
    assert asyncio.run(m.acquire("paint")) is True                    # 本地域
    assert asyncio.run(m.acquire("dialog", domain="remote")) is True  # 伪域并行
    assert asyncio.run(m.acquire("dialog")) is False                  # 本地域被 paint 占
    asyncio.run(m.release("dialog", domain="remote"))
    asyncio.run(m.release("paint"))
    assert m.active_feature is None


def test_sync_fallback_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """acquire_sync/release_sync（lora/style 训练降级路径）：与异步
    路径同语义——同域互斥、可重入、计数归零才放锁。"""
    m = _fl(monkeypatch, {})
    assert m.acquire_sync("training", task_id="t1") is True
    assert m.active_feature == "training"
    assert m.acquire_sync("training") is True      # 重入
    assert m.acquire_sync("dialog") is False       # 同域阻断
    m.release_sync("training")
    assert m.active_feature == "training"
    m.release_sync("training")
    assert m.active_feature is None


def test_invalid_feature_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _fl(monkeypatch, {})
    with pytest.raises(ApiError):
        asyncio.run(m.acquire("bogus"))
    assert m.acquire_sync("bogus") is False


# ── 显存账本按卡记账（纯记账模式，不依赖真 CUDA）────────────────

def test_vram_per_device_ledger() -> None:
    vm = VramManager()
    # 强制纯记账模式（测试不依赖真 CUDA 状态）
    vm._cuda_available = False
    vm._vram_totals = {0: 16000.0, 1: 24000.0}
    vm._vram_total_mb = 16000.0
    vm._device_allocated_mb = {}

    vm.track_alloc("m0", 1000.0, device=0)
    vm.track_alloc("m1", 2000.0, device=1)

    u0 = vm.get_device_usage(0)
    u1 = vm.get_device_usage(1)
    assert u0["total_mb"] == 16000.0 and u0["used_mb"] == 1000.0
    assert u1["total_mb"] == 24000.0 and u1["used_mb"] == 2000.0
    assert vm.get_available_mb(1) == pytest.approx(22000.0)
    # 默认（无参）= 主卡口径，且携带卡号与全卡分解
    u = vm.get_usage()
    assert u["device"] == 0
    assert {d["index"] for d in u["devices"]} == {0, 1}

    # 按卡重置只清指定卡
    vm.reset_bookkeeping(device=0)
    assert vm.get_device_usage(0)["used_mb"] == 0.0
    assert vm.get_device_usage(1)["used_mb"] == 2000.0
    vm.track_free("m1")
    assert vm.get_device_usage(1)["used_mb"] == 0.0


# ── 3. 接线哨兵（源码断言，防绑卡接线被删/回退）─────────────────

def test_vllm_bind_sentinel() -> None:
    src = (_ROOT / "backend" / "engines" / "vllm_service.py").read_text(
        encoding="utf-8")
    assert 'env["CUDA_VISIBLE_DEVICES"]' in src, "vLLM 子进程绑卡接线被删"
    assert 'resolve_feature_device("dialog")' in src, "vLLM 未按资源域绑卡"


def test_comfy_bind_sentinel() -> None:
    src = (_ROOT / "backend" / "services" / "inference"
           / "comfy_proc.py").read_text(encoding="utf-8")
    assert 'env["CUDA_VISIBLE_DEVICES"]' in src, "ComfyUI 绑卡接线被删"
    assert 'resolve_feature_device("paint")' in src, "ComfyUI 未按资源域绑卡"


def test_vram_gates_not_hardcoded_device_zero() -> None:
    """显存闸门/账本不得锚死 0 号卡（批1 前的病灶）。"""
    src = (_ROOT / "backend" / "engines" / "vllm_service.py").read_text(
        encoding="utf-8")
    assert "mem_get_info(0)" not in src, "vLLM 准入闸门仍锚死 0 号卡"
    # 批2（2026-09-10）：读数统一走 gpu_budget.read_physical_bytes
    # （torch-only 通道），仍以 _dev_idx 传域卡号——两种形态均认
    assert ("mem_get_info(_dev_idx)" in src
            or "read_physical_bytes(" in src), \
        "vLLM 准入闸门读数未按域卡号"
    src2 = (_ROOT / "backend" / "engines" / "vram_manager.py").read_text(
        encoding="utf-8")
    assert "memory_allocated(0)" not in src2, "显存账本仍锚死 0 号卡"
    assert "mem_get_info(0)" not in src2
    src3 = (_ROOT / "backend" / "services" / "inference" / "backends"
            / "vllm_backend.py").read_text(encoding="utf-8")
    assert "mem_get_info(0)" not in src3, "vLLM 让档闸门仍锚死 0 号卡"


def test_release_for_module_device_guard_sentinel() -> None:
    src = (_ROOT / "backend" / "services" / "model_manager"
           / "__init__.py").read_text(encoding="utf-8")
    assert "target_device" in src, "release_for_module 未按卡释放"
    assert "_vllm_same_card" in src, "vLLM 按需终止缺异卡守卫"
    assert "_comfy_same_card" in src, "ComfyUI 按需终止缺异卡守卫"


# ── 审计 09-10 P1-20：同 key 重复登记按替换语义净增减 ────────────────

def test_vram_track_alloc_same_key_replaces_net() -> None:
    """同 key 二次 track_alloc 先扣旧值再记新值——总额不虚增、释放归零。

    旧病：覆盖条目但总额照加，track_free 只扣新值，旧 size 永久泄漏。
    """
    vm = VramManager()
    vm._cuda_available = False
    vm._vram_totals = {0: 16000.0}
    vm._vram_total_mb = 16000.0
    vm._device_allocated_mb = {}

    vm.track_alloc("model_a", 5000.0, device=0)
    assert vm.get_device_usage(0)["used_mb"] == pytest.approx(5000.0)
    vm.track_alloc("model_a", 3000.0, device=0)  # 模型重载场景
    assert vm.get_device_usage(0)["used_mb"] == pytest.approx(3000.0)
    vm.track_free("model_a")
    assert vm.get_device_usage(0)["used_mb"] == pytest.approx(0.0)
# 本项目仅供学习使用，商业授权请+Q 3559331368
