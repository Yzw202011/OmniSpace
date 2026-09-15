"""A/B 第二轮·贴近真实人物（2026-09-15 凌晨）：夏沐沐真人感参考。

三张同 prompt/seed/尺寸：
  B  = comfy klein-9b-fp8 + PuLID(特写,1.0)（产线身份硬锁，keyframe 链同参）
  A  = legacy klein-4b + 特写像素 img2img(0.55)（现产线回落实况=软锁）
  A+ = legacy klein-4b + D-LoRA(xia_mumu_v1,1.0)（attach_lora 未接产线，
       展示"若接线"的快线上限）
参考图: data/comfyui/output/ab2_reference.png（closeup 拷贝）
"""
import sys, time, shutil
from pathlib import Path

sys.path.insert(0, r"E:\OmniSpace")
from PIL import Image

PROMPT = ("一位年轻的亚洲女性，黑色长直发带刘海，穿白色T恤和浅蓝色牛仔裤，"
          "站在樱花树下回眸微笑，粉色花瓣飘落，写实人像摄影，真实照片质感，"
          "自然肤色，高清细节，柔和自然光，非动漫非卡通")
NEG = "anime, cartoon, illustration, 3d render, painting"
SEED = 20260914
SIZE = dict(width=512, height=512)
FACE = Path(r"E:\OmniSpace\data\comic_assets\global\characters\夏沐沐\portrait_views\closeup.png")
OUT = Path(r"E:\OmniSpace\data\generated\images")
LORA = Path(r"E:\OmniSpace\data\lora_train\xia_mumu\output\xia_mumu_v1.safetensors")

face = Image.open(FACE).convert("RGB")
shutil.copy(FACE, OUT / "ab2_reference.png")
print("reference:", face.size, flush=True)

# ── Phase B: comfy 9b + PuLID ────────────────────────────────────────
from backend.services.inference.comfy_paint_engine import (
    get_comfy_paint_engine, pulid_available)
from backend.services.inference.comfy_proc import get_comfy_proc

assert pulid_available(), "PuLID 权重/节点不在位——comfy 路线身份锁不可用"
eng = get_comfy_paint_engine()
params = {"prompt": PROMPT, "negative": NEG, "seed": SEED, "steps": 20,
          "cfg": 7.5, "sampler": "euler_a", "pulid_strength": 1.0, **SIZE}
t0 = time.perf_counter()
r = eng.generate(params, pulid_image=face)
r["images"][0].save(OUT / "ab2_comfy9b_pulid.png", "PNG")
print(f"[B comfy9b+PuLID] wall={time.perf_counter()-t0:.1f}s "
      f"model={r.get('model')}", flush=True)
get_comfy_proc().shutdown()
print("comfy shut down", flush=True)

# ── Phase A: legacy 4b + 像素 img2img 软锁 ───────────────────────────
from backend.services.inference.paint_engine import get_paint_engine
pe = get_paint_engine()
assert pe.ensure_loaded("flux2-klein-4b"), pe.get_status().get("last_error")
params_a = {"prompt": PROMPT, "negative": NEG, "seed": SEED, "steps": 20,
            "cfg": 7.5, "sampler": "euler_a", "strength": 0.55, **SIZE}
t0 = time.perf_counter()
r = pe.img2img(params_a, face)
r["images"][0].save(OUT / "ab2_legacy4b_ref.png", "PNG")
print(f"[A legacy4b+ref软锁] wall={time.perf_counter()-t0:.1f}s", flush=True)

# ── Phase A+: legacy 4b + D-LoRA ─────────────────────────────────────
assert pe.attach_lora(str(LORA), scale=1.0), pe.get_status().get("last_error")
params_ap = {"prompt": PROMPT, "negative": NEG, "seed": SEED, "steps": 20,
             "cfg": 7.5, "sampler": "euler_a", **SIZE}
t0 = time.perf_counter()
r = pe.generate(params_ap)
r["images"][0].save(OUT / "ab2_legacy4b_lora.png", "PNG")
print(f"[A+ legacy4b+LoRA] wall={time.perf_counter()-t0:.1f}s", flush=True)
print("AB2-DONE")
