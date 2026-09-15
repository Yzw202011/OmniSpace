"""A/B 第四轮·3D GC 游戏风结构化描述词（2026-09-15）。
用户描述词原文直入，三配置同 seed。看点：文字（低马尾）vs 参考图（披发）
冲突时各配置听谁的 + 3D 游戏风格执行力。
"""
import sys, time
from pathlib import Path
sys.path.insert(0, r"E:\OmniSpace")
from PIL import Image

PROMPT = ("美术风格：3D GC 游戏风格、高精度 3D 建模、PBR 物理渲染、清晰硬表面质感、"
          "游戏实时光影、干净明暗过渡、写实二次元游戏质感、画面锐利通透\n"
          "时代背景：当代中国都市，盛夏午后\n"
          "角色设定：22岁中国都市年轻女性，身高162cm。体态纤细偏瘦，肩线窄而柔和，"
          "骨相柔和偏鹅蛋脸，脸部线条圆润略带少量清秀稚气。五官清秀端正，平直细眉，"
          "圆杏眼，鼻梁小巧挺直，唇形偏薄。乌黑中长发扎成低马尾，发丝柔顺垂至肩胛骨下方，"
          "额前有轻薄碎发。气质关键词：干净、清秀、朴素、带初入社会的青涩感。"
          "上身穿白色棉质圆领短袖T恤，衣摆自然垂至胯部；下身穿浅蓝色高腰直筒九分牛仔裤，"
          "裤脚微微卷起。脚穿白色帆布鞋，平底，鞋面干净。空手。常态平静表情，眼睛平视镜头。")
NEG = ""
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
                  "width": 512, "height": 768}, pulid_image=face)
r["images"][0].save(OUT / "ab4_gc_pulid.png", "PNG")
print(f"[B comfy9b+PuLID GC风] wall={time.perf_counter()-t0:.1f}s", flush=True)
get_comfy_proc().shutdown()

from backend.services.inference.paint_engine import get_paint_engine
pe = get_paint_engine()
assert pe.ensure_loaded("flux2-klein-4b"), pe.get_status().get("last_error")
t0 = time.perf_counter()
r = pe.img2img({"prompt": PROMPT, "negative": NEG, "seed": SEED,
                "steps": 20, "cfg": 7.5, "sampler": "euler_a",
                "strength": 0.55, "width": 512, "height": 768}, face)
r["images"][0].save(OUT / "ab4_gc_ref.png", "PNG")
print(f"[A legacy4b+软锁 GC风] wall={time.perf_counter()-t0:.1f}s", flush=True)

assert pe.attach_lora(
    r"E:\OmniSpace\data\lora_train\xia_mumu\output\xia_mumu_v1.safetensors",
    scale=1.0), pe.get_status().get("last_error")
t0 = time.perf_counter()
r = pe.generate({"prompt": PROMPT, "negative": NEG, "seed": SEED,
                 "steps": 20, "cfg": 7.5, "sampler": "euler_a",
                 "width": 512, "height": 768})
r["images"][0].save(OUT / "ab4_gc_lora.png", "PNG")
print(f"[A+ legacy4b+LoRA GC风] wall={time.perf_counter()-t0:.1f}s", flush=True)
print("AB4-DONE")
