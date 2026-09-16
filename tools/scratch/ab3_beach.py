"""A/B 第三轮·海滩散步（2026-09-15）：同人物参考，谁还原度最高。

  B  = comfy 9b + PuLID（硬锁，参考图只喂脸）
  A  = legacy 4b + 像素 img2img 0.55（软锁，参考图定构图）
  A+ = legacy 4b + D-LoRA（训练身份，免参考图）
统一 prompt/seed/尺寸。
"""
import sys, time
from pathlib import Path
sys.path.insert(0, r"E:\OmniSpace")
from PIL import Image

PROMPT = ("一位年轻的亚洲女性在海滩上散步，黑色长直发带刘海，穿白色T恤和浅蓝色牛仔裤，"
          "海风吹拂头发，海浪和沙滩背景，写实人像摄影，真实照片质感，"
          "自然肤色，高清细节，柔和自然光，非动漫非卡通")
NEG = "anime, cartoon, illustration, 3d render, painting"
SEED = 20260914
FACE = Path(r"E:\OmniSpace\data\generated\images\ab2_reference_real.png")
OUT = FACE.parent
face = Image.open(FACE).convert("RGB")

from src.services.inference.comfy_paint_engine import (
    get_comfy_paint_engine, pulid_available)
from src.services.inference.comfy_proc import get_comfy_proc
assert pulid_available()
eng = get_comfy_paint_engine()
t0 = time.perf_counter()
r = eng.generate({"prompt": PROMPT, "negative": NEG, "seed": SEED,
                  "steps": 20, "cfg": 7.5, "sampler": "euler_a",
                  "pulid_strength": 1.0,
                  "width": 512, "height": 512}, pulid_image=face)
r["images"][0].save(OUT / "ab3_beach_pulid.png", "PNG")
print(f"[B comfy9b+PuLID 海滩] wall={time.perf_counter()-t0:.1f}s", flush=True)
get_comfy_proc().shutdown()

from src.services.inference.paint_engine import get_paint_engine
pe = get_paint_engine()
assert pe.ensure_loaded("flux2-klein-4b"), pe.get_status().get("last_error")
t0 = time.perf_counter()
r = pe.img2img({"prompt": PROMPT, "negative": NEG, "seed": SEED,
                "steps": 20, "cfg": 7.5, "sampler": "euler_a",
                "strength": 0.55, "width": 512, "height": 512}, face)
r["images"][0].save(OUT / "ab3_beach_ref.png", "PNG")
print(f"[A legacy4b+软锁 海滩] wall={time.perf_counter()-t0:.1f}s", flush=True)

assert pe.attach_lora(
    r"E:\OmniSpace\data\lora_train\xia_mumu\output\xia_mumu_v1.safetensors",
    scale=1.0), pe.get_status().get("last_error")
t0 = time.perf_counter()
r = pe.generate({"prompt": PROMPT, "negative": NEG, "seed": SEED,
                 "steps": 20, "cfg": 7.5, "sampler": "euler_a",
                 "width": 512, "height": 512})
r["images"][0].save(OUT / "ab3_beach_lora.png", "PNG")
print(f"[A+ legacy4b+LoRA 海滩] wall={time.perf_counter()-t0:.1f}s", flush=True)
print("AB3-DONE")
