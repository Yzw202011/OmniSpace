"""第二轮续：用真人感特写重跑 B(comfy9b+PuLID) 与 A(legacy4b+软锁)。"""
import sys, time
from pathlib import Path
sys.path.insert(0, r"E:\OmniSpace")
from PIL import Image

PROMPT = ("一位年轻的亚洲女性，黑色长直发带刘海，穿白色T恤和浅蓝色牛仔裤，"
          "站在樱花树下回眸微笑，粉色花瓣飘落，写实人像摄影，真实照片质感，"
          "自然肤色，高清细节，柔和自然光，非动漫非卡通")
NEG = "anime, cartoon, illustration, 3d render, painting"
SEED = 20260914
FACE = Path(r"E:\OmniSpace\data\generated\images\ab2_reference_real.png")
OUT = FACE.parent
face = Image.open(FACE).convert("RGB")

from backend.services.inference.comfy_paint_engine import (
    get_comfy_paint_engine, pulid_available)
from backend.services.inference.comfy_proc import get_comfy_proc
assert pulid_available()
eng = get_comfy_paint_engine()
t0 = time.perf_counter()
r = eng.generate({"prompt": PROMPT, "negative": NEG, "seed": SEED,
                  "steps": 20, "cfg": 7.5, "sampler": "euler_a",
                  "pulid_strength": 1.0,
                  "width": 512, "height": 512}, pulid_image=face)
r["images"][0].save(OUT / "ab2_comfy9b_pulid_real.png", "PNG")
print(f"[B2 comfy9b+PuLID·真人特写] wall={time.perf_counter()-t0:.1f}s", flush=True)
get_comfy_proc().shutdown()

from backend.services.inference.paint_engine import get_paint_engine
pe = get_paint_engine()
assert pe.ensure_loaded("flux2-klein-4b"), pe.get_status().get("last_error")
t0 = time.perf_counter()
r = pe.img2img({"prompt": PROMPT, "negative": NEG, "seed": SEED,
                "steps": 20, "cfg": 7.5, "sampler": "euler_a",
                "strength": 0.55, "width": 512, "height": 512}, face)
r["images"][0].save(OUT / "ab2_legacy4b_ref_real.png", "PNG")
print(f"[A2 legacy4b+软锁·真人特写] wall={time.perf_counter()-t0:.1f}s", flush=True)
print("AB2-R2-DONE")
