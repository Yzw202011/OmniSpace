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
    # 2026-09-25 D 线收编 z-image-base → 五件套
    ids = {m["id"] for m in cpe.COMFY_PAINT_MODELS}
    if cpe._KLEIN_4B_BROKEN:
        assert ids == {"flux2-klein-9b", "z-image-turbo",
                       "qwen-image-2.1"}
    else:
        assert ids == {"flux2-klein-9b", "flux2-klein-4b",
                       "z-image-turbo", "z-image-base",
                       "qwen-image-2.1"}
    # 旧栈候选永不在 comfy 真源（sdxl/qwen-2512 走 legacy diffusers 栈）
    assert "sdxl-base-1.0" not in ids
    assert "qwen-image-2512" not in ids
    # FLUX.1-dev / HiDream-O1 实测不过关不集成（2026-09-25 D 线）
    assert "flux1-dev" not in ids
    assert "hidream-o1" not in ids
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
    # 残局守卫文案（仅在 4b 摘除期间有意义；修复回真后跳过）
    if not cpe._KLEIN_4B_BROKEN:
        pytest.skip("klein-4b 已修复回真，守卫文案测试退役")
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
    # klein-4b 官方 fp8 回真（2026-09-23 晚）：显式点名真切 fp8 三件套
    if not cpe._KLEIN_4B_BROKEN:
        wf4 = _wf({"prompt": "四B", "model": "flux2-klein-4b"})
        assert wf4["unet"]["inputs"]["unet_name"] == (
            "flux-2-klein-4b-fp8.safetensors")
        assert wf4["clip"]["inputs"]["clip_name"] == "qwen_3_4b.safetensors"


def test_qwen21_workflow_official_baseline() -> None:
    # Qwen-Image-2.1 构建基线（2026-09-23 集成，POC 实弹同构）：
    # 专用三输出编码器 + EmptyLatentImage 控宽高比 + 25 步/cfg1 官线
    wf = cpe.ComfyPaintEngine._build_workflow_qwen21(
        _eng(), {"prompt": "测试", "negative": "低清", "width": 832,
                 "height": 480, "seed": 7}, None, "paint/t")
    assert wf["unet"]["inputs"]["unet_name"] == (
        "qwen_image_2.1_int8_convrot.safetensors")
    assert wf["clip"]["inputs"]["clip_name"] == "qwen3vl_8b_w4a8.safetensors"
    assert wf["clip"]["inputs"]["type"] == "qwen_image"
    assert wf["vae"]["inputs"]["vae_name"] == "qwen_image_2.1_vae_bf16.safetensors"
    enc = wf["encode"]
    assert enc["class_type"] == "TextEncodeQwenImage21"
    assert enc["inputs"]["prompt"] == "测试"
    assert enc["inputs"]["negative_prompt"] == "低清"
    assert enc["inputs"]["resolution"] == 832  # max(832,480) 取 32 倍数
    assert wf["latent"]["class_type"] == "EmptyLatentImage"
    assert wf["latent"]["inputs"]["width"] == 832
    assert wf["latent"]["inputs"]["height"] == 480
    ks = wf["sample"]["inputs"]
    assert ks["steps"] == 25 and ks["cfg"] == 1.0
    assert ks["sampler_name"] == "euler" and ks["scheduler"] == "simple"
    assert ks["seed"] == 7
    assert ks["positive"] == ["encode", 0] and ks["negative"] == ["encode", 1]
    assert ks["latent_image"] == ["latent", 0]
    assert wf["decode"]["class_type"] == "VAEDecode"
    assert wf["save"]["class_type"] == "SaveImage"


def test_qwen21_workflow_refs_and_lora() -> None:
    wf = cpe.ComfyPaintEngine._build_workflow_qwen21(
        _eng(), {"prompt": "参考", "lora_name": "hero_v1",
                 "lora_scale": 0.8, "width": 1024, "height": 1024},
        ["r1.png", "r2.png", "r3.png", "r4.png", "r5.png",
         "r6.png", "r7.png", "r8.png", "r9.png"], "paint/t")
    # LoRA 模型侧挂载、TE 不包（strength_clip=0）
    assert wf["lora"]["inputs"]["strength_model"] == 0.8
    assert wf["lora"]["inputs"]["strength_clip"] == 0.0
    assert wf["sample"]["inputs"]["model"] == ["lora", 0]
    assert wf["encode"]["inputs"]["clip"] == ["lora", 1]
    # 参考图 ≤_MAX_WF_REFS 截断 + image1..N 条件注入
    for i in range(1, 9):
        assert wf[f"load_img{i}"]["class_type"] == "LoadImage"
        assert wf["encode"]["inputs"][f"image{i}"] == [f"load_img{i}", 0]
    assert "image9" not in wf["encode"]["inputs"]


def test_face_beauty_injection_disabled_for_klein() -> None:
    """klein 人脸干预下线（2026-09-26 用户拍板）：词块不再注入。"""
    person = "22岁中国都市年轻女性，白色T恤，低马尾"
    for mid in ("flux2-klein-9b", "flux2-klein-4b"):
        assert cpe._inject_face_beauty(person, mid) == person
    # z-base 偶像词块保留（无精修 Pass 的补偿通道）
    out = cpe._inject_face_beauty(person, "z-image-base")
    assert "偶像级绝美少女" in out and out.startswith(person)
    # 幂等
    assert cpe._inject_face_beauty(out, "z-image-base") == out
    # 场景图不注入
    assert cpe._inject_face_beauty(
        "a cozy wooden bookshop interior", "z-image-base") == (
        "a cozy wooden bookshop interior")


def test_face_beauty_builder_integration() -> None:
    # klein 构建器不再注入（干净原生出图）
    wf = _wf({"prompt": "22岁中国都市年轻女性，白色T恤"})
    assert "东亚年轻面容" not in wf["pos"]["inputs"]["text"]
    # z/qwen21 构建器不受扰
    wz = cpe.ComfyPaintEngine._build_workflow_z_image(
        _eng(), {"prompt": "22岁女性"}, None, "paint/t")
    assert "东亚年轻面容" not in wz["pos"]["inputs"]["text"]
    wq = cpe.ComfyPaintEngine._build_workflow_qwen21(
        _eng(), {"prompt": "22岁女性", "width": 832, "height": 480},
        None, "paint/t")
    assert "东亚年轻面容" not in wq["encode"]["inputs"]["prompt"]


def test_zbase_workflow_variant() -> None:
    """z-image-base 变体（2026-09-25 D 线收编）：40 步/负向词有效。"""
    wf = cpe.ComfyPaintEngine._build_workflow_z_image(
        _eng(), {"prompt": "测试", "negative": "低清",
                 "model": "z-image-base"}, None, "paint/t")
    assert wf["unet"]["inputs"]["unet_name"] == (
        "z_image_base-nvfp8-mixed.safetensors")
    # 非蒸馏版负向词有效（真实负条件）
    assert wf["neg"]["class_type"] == "CLIPTextEncode"
    ks = wf["sample"]["inputs"]
    assert ks["steps"] == 40 and ks["cfg"] == 4.0
    # turbo 蒸馏版基线不退（8 步/cfg1/ZeroOut）
    wt = cpe.ComfyPaintEngine._build_workflow_z_image(
        _eng(), {"prompt": "测试", "model": "z-image-turbo"}, None,
        "paint/t")
    assert wt["unet"]["inputs"]["unet_name"] == "z_image_turbo_bf16.safetensors"
    assert wt["neg"]["class_type"] == "ConditioningZeroOut"
    assert wt["sample"]["inputs"]["steps"] == 8
    assert wt["sample"]["inputs"]["cfg"] == 1.0


def test_face_beauty_routing_by_model() -> None:
    """美学词块按模型路由（2026-09-26 klein 下线后仅 z-base）。"""
    person = "22岁中国都市年轻女性，白色T恤"
    # z-base → 偶像级词块
    out = cpe._inject_face_beauty(person, "z-image-base")
    assert "偶像级绝美少女" in out and out.startswith(person)
    # klein 系 → 不注入（下线）
    for mid in ("flux2-klein-9b", "flux2-klein-4b"):
        assert cpe._inject_face_beauty(person, mid) == person
    # z-turbo/qwen21 不注入（自带先验）
    for mid in ("z-image-turbo", "qwen-image-2.1"):
        assert cpe._inject_face_beauty(person, mid) == person
    # 幂等
    once = cpe._inject_face_beauty(person, "z-image-base")
    assert cpe._inject_face_beauty(once, "z-image-base") == once


def test_zbase_builder_injects_idol_block() -> None:
    wf = cpe.ComfyPaintEngine._build_workflow_z_image(
        _eng(), {"prompt": "22岁中国都市年轻女性",
                 "model": "z-image-base"}, None, "paint/t")
    assert "偶像级绝美少女" in wf["pos"]["inputs"]["text"]
    # klein builder 原样（干预下线）
    wk = _wf({"prompt": "22岁中国都市年轻女性"})
    assert "偶像级绝美少女" not in wk["pos"]["inputs"]["text"]



def test_default_negative_injection() -> None:
    """通用负面词默认注入（2026-09-26 拍板：klein 系+z-base 用户零填写）。"""
    # ① 三模型空负面词 → 全套默认（欧美脸组打头）
    for mid in ("flux2-klein-9b", "flux2-klein-4b", "z-image-base"):
        out = cpe._effective_negative("", mid)
        assert out.startswith("欧美人脸，西方人脸")
        assert "畸形五官" in out and "水印" in out
    # ② 用户手填 → 用户词在前+通用块合并；含欧美词不重复欧美组
    out = cpe._effective_negative("不要猫", "flux2-klein-9b")
    assert out.startswith("不要猫，")
    assert "欧美人脸" in out  # 欧美脸组恒保留
    out2 = cpe._effective_negative("欧美脸不要", "flux2-klein-9b")
    assert out2.count("欧美人脸") == 0 and "畸形五官" in out2
    # ③ z-turbo/qwen21 不注入（架构无效/弱化）
    for mid in ("z-image-turbo", "qwen-image-2.1"):
        assert cpe._effective_negative("", mid) == ""
        assert cpe._effective_negative("x", mid) == "x"


def test_default_negative_builder_integration() -> None:
    # klein builder：neg 节点吃到默认块
    wf = _wf({"prompt": "人物"})
    assert "欧美人脸" in wf["neg"]["inputs"]["text"]
    # z-base builder：base 变体 neg 节点吃到；turbo 保持 ZeroOut
    wb = cpe.ComfyPaintEngine._build_workflow_z_image(
        _eng(), {"prompt": "人物", "model": "z-image-base"}, None, "paint/t")
    assert wb["neg"]["class_type"] == "CLIPTextEncode"
    assert "欧美人脸" in wb["neg"]["inputs"]["text"]
    wt = cpe.ComfyPaintEngine._build_workflow_z_image(
        _eng(), {"prompt": "人物", "model": "z-image-turbo"}, None, "paint/t")
    assert wt["neg"]["class_type"] == "ConditioningZeroOut"
