"""Z1 Z-Image-Turbo 工作流族测试（2026-09-15）。

真 import（B9 约定）取 comfy_paint_engine 的 z_image 工作流构建方法，
验证：官方基线参数固化（8步/cfg1/res_multistep/simple/shift3）、
TE 走 lumina2 类型（Qwen3-4B → z_image 分支）、参考图走
TextEncodeZImageOmni（≤3 张，z_pos 接管 pos/neg）、PuLID 拒绝、
klein 默认工作流不受影响。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import pytest
from PIL import Image

from src.services.inference import comfy_paint_engine as cpe


def _eng() -> cpe.ComfyPaintEngine:
    # 纯函数构建不触 self 状态；__new__ 绕过重型初始化
    return cpe.ComfyPaintEngine.__new__(cpe.ComfyPaintEngine)


def _z_wf(params: dict, refs: list[str] | None = None) -> dict:
    return cpe.ComfyPaintEngine._build_workflow_z_image(
        _eng(), params, refs, "paint/t")


def test_z_image_workflow_official_baseline() -> None:
    wf = _z_wf({"prompt": "测试", "width": 1280, "height": 720,
                "seed": 7})
    assert wf["unet"]["class_type"] == "UNETLoader"
    assert wf["unet"]["inputs"]["unet_name"] == "z_image_turbo_bf16.safetensors"
    assert wf["clip"]["inputs"]["clip_name"] == "qwen_3_4b.safetensors"
    assert wf["clip"]["inputs"]["type"] == "lumina2"
    assert wf["vae"]["inputs"]["vae_name"] == "z_image_ae.safetensors"
    assert wf["ms"] == {"class_type": "ModelSamplingAuraFlow",
                        "inputs": {"model": ["unet", 0], "shift": 3}}
    ks = wf["sample"]["inputs"]
    assert ks["steps"] == 8 and ks["cfg"] == 1.0
    assert ks["sampler_name"] == "res_multistep"
    assert ks["scheduler"] == "simple"
    assert ks["seed"] == 7
    assert wf["neg"]["class_type"] == "ConditioningZeroOut"
    assert wf["neg"]["inputs"]["conditioning"] == ["pos", 0]
    assert wf["latent"]["class_type"] == "EmptySD3LatentImage"
    assert wf["latent"]["inputs"]["width"] == 1280
    # 传入 steps/cfg 也不得改写（官方基线固化）
    wf2 = _z_wf({"prompt": "x", "steps": 36, "cfg": 7.5})
    assert wf2["sample"]["inputs"]["steps"] == 8
    assert wf2["sample"]["inputs"]["cfg"] == 1.0


def test_z_image_refs_use_omni_encoder() -> None:
    wf = _z_wf({"prompt": "参考测试", "width": 512, "height": 768},
               ["r1.png", "r2.png"])
    z = wf["z_pos"]
    assert z["class_type"] == "TextEncodeZImageOmni"
    # 参考图先精确缩放到出图尺寸（参考隐空间须与采样隐空间同形）
    assert z["inputs"]["auto_resize_images"] is False
    for i in (1, 2):
        sc = wf[f"scale_img{i}"]
        assert sc["class_type"] == "ImageScale"
        assert sc["inputs"]["width"] == 512
        assert sc["inputs"]["height"] == 768
        assert sc["inputs"]["crop"] == "center"
        assert z["inputs"][f"image{i}"] == [f"scale_img{i}", 0]
    assert z["inputs"]["vae"] == ["vae", 0]
    assert wf["sample"]["inputs"]["positive"] == ["z_pos", 0]
    assert wf["neg"]["inputs"]["conditioning"] == ["z_pos", 0]
    # 无参考：不得出现 z_pos / LoadImage
    plain = _z_wf({"prompt": "x"})
    assert "z_pos" not in plain and "load_img1" not in plain


def test_z_image_refs_capped_at_three() -> None:
    wf = _z_wf({"prompt": "x"}, ["a.png", "b.png", "c.png", "d.png"])
    assert "load_img4" not in wf
    assert wf["z_pos"]["inputs"]["image3"] == ["scale_img3", 0]


def test_z_mode_rejects_pulid() -> None:
    eng = _eng()
    face = Image.new("RGB", (4, 4))
    with pytest.raises(cpe.ApiError) as ei:
        cpe.ComfyPaintEngine._run_locked(
            eng, {"model": "z-image-turbo", "prompt": "x",
                  "pulid_strength": 1.0},
            None, pulid_image=face)
    assert "PuLID" in str(ei.value.message or ei.value)


def test_klein_default_workflow_unaffected() -> None:
    # 不点名 z-image 时必须仍是 klein flux2 工作流族
    eng = cpe.ComfyPaintEngine.__new__(cpe.ComfyPaintEngine)
    wf = cpe.ComfyPaintEngine._build_workflow(
        eng, {"prompt": "x", "width": 512, "height": 512, "seed": 1},
        [], "paint/t")
    assert wf["unet"]["inputs"]["unet_name"] == "flux-2-klein-9b-fp8.safetensors"
    assert "ModelSamplingAuraFlow" not in {v["class_type"]
                                           for v in wf.values()}
