"""切换诚实性修复测试（2026-09-23，方案四段制验收单测面）。

背景：comfy 生图栈历史上「名单外模型静默按 klein-9b 出图」（假切换）
——用户以为在用 sdxl/qwen-2512/klein-4b，实际出图恒为 klein-9b-fp8。
修复后三表合一：COMFY_PAINT_MODELS 真源 = 引擎白名单 = 前端清单。

验证：
  ① 真源集合：_KLEIN_4B_BROKEN 期间恰为 klein-9b + z-image-turbo，
     旧栈候选（sdxl/qwen-image-2512）与残局 klein-4b 均不在列；
  ② 名单外点名如实失败 MODEL_NOT_ALLOWED（消息含可用名单）；
  ③ klein-4b 残局守卫（显式点名与 D-LoRA 自动切换双路径）；
  ④ 真源清单项形状（files 内部键不外泄）；
  ⑤ klein-9b 显式点名/缺省回落仍正确（不误伤既有流程）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import pytest

from src.middleware.error_handler import ApiError
from src.services.inference import comfy_paint_engine as cpe


def _eng() -> cpe.ComfyPaintEngine:
    # 纯函数构建不触 self 状态；__new__ 绕过重型初始化（同 z 测试族）
    return cpe.ComfyPaintEngine.__new__(cpe.ComfyPaintEngine)


def _wf(params: dict) -> dict:
    return cpe.ComfyPaintEngine._build_workflow(
        _eng(), params, None, "paint/t")


def test_truth_set_matches_physical_reality() -> None:
    ids = {m["id"] for m in cpe.COMFY_PAINT_MODELS}
    if cpe._KLEIN_4B_BROKEN:
        assert ids == {"flux2-klein-9b", "z-image-turbo"}
        assert "flux2-klein-4b" not in ids
    # 旧栈候选永不在 comfy 真源（sdxl/qwen-2512 走 legacy diffusers 栈）
    assert "sdxl-base-1.0" not in ids
    assert "qwen-image-2512" not in ids
    for m in cpe.COMFY_PAINT_MODELS:
        assert set(m["files"]) == {"unet", "clip", "vae"}
        assert float(m["min_vram_gb"]) > 0


def test_reject_unknown_model_honestly() -> None:
    with pytest.raises(ApiError) as ei:
        cpe._reject_unavailable_model("sdxl-base-1.0")
    assert ei.value.code == "MODEL_NOT_ALLOWED"
    msg = str(ei.value)
    for mid in ("flux2-klein-9b", "z-image-turbo"):
        assert mid in msg, "错误消息必须带可用名单（出路）"
    # 智能路由语义：空串/None 不是点名，不该走到拒绝函数——契约由
    # _run 的 `if _mid and ...` 保证；真源集合不含空串
    assert "" not in cpe._COMFY_PAINT_MODEL_IDS


def test_reject_klein_4b_residue_wording() -> None:
    with pytest.raises(ApiError) as ei:
        cpe._reject_unavailable_model("flux2-klein-4b")
    assert ei.value.code == "MODEL_NOT_ALLOWED"
    msg = ei.value.message
    assert "残局" in msg or "不可用" in msg


def test_d_lora_auto_switch_guarded() -> None:
    # D-LoRA 自动切 4b 路径：残局期间如实失败，不再硬切烂权重炸采样
    if not cpe._KLEIN_4B_BROKEN:
        pytest.skip("klein-4b 转换已修复，守卫按约定退役")
    with pytest.raises(ApiError):
        _wf({"prompt": "x", "lora_name": "xia_mumu_v1",
             "lora_base": "4b"})


def test_models_ready_item_shape() -> None:
    items = cpe.comfy_paint_models_ready()
    assert items, "真源清单不应为空"
    for it in items:
        assert "files" not in it, "内部 files 键不得外泄到清单面"
        assert it["status"] in ("ready", "not_installed")
        assert it["id"] and it["name"]
        assert float(it["min_vram_gb"]) > 0


def test_valid_models_still_build() -> None:
    # 显式点名 9b：不误伤（回归）
    wf = _wf({"prompt": "回归", "model": "flux2-klein-9b"})
    assert wf["unet"]["inputs"]["unet_name"] == "flux-2-klein-9b-fp8.safetensors"
    assert wf["clip"]["inputs"]["clip_name"] == "qwen3_8b.safetensors"
    # 缺省（智能路由/空点名）：回落 klein-9b 默认底座
    wf2 = _wf({"prompt": "缺省"})
    assert wf2["unet"]["inputs"]["unet_name"] == "flux-2-klein-9b-fp8.safetensors"
