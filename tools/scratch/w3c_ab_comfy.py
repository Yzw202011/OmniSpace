"""W3-C A/B comfy 侧（2026-09-13）：分镜行预览 512²，同 seed 同描述词。

跑两档：fast（4步/cfg1，目标档）与 preset 默认（当前 quality=36步/cfg4）。
产物落 data/comfyui/output/w3c_ab_*.png，供与 legacy（backend 真端点）目验对比。
"""
import sys
import time

sys.path.insert(0, r"E:\OmniSpace")

from src.api.manga.common import comfy_paint_generate  # noqa: E402

PROMPT = ("A. 全局风格：3D卡通、皮克斯质感、黏土材质渲染、柔和电影光、"
          "干净明亮，细节刻画精致，光影层次丰富")
SEED = 20260913

base = {"prompt": PROMPT, "negative": "", "width": 512, "height": 512,
        "seed": SEED}

t0 = time.perf_counter()
r_fast = comfy_paint_generate({**base, "steps": 4, "cfg": 1.0})
t_fast = time.perf_counter() - t0
r_fast["images"][0].save(
    r"E:\OmniSpace\data\comfyui\output\w3c_ab_comfy_fast.png", "PNG")
print(f"[comfy-fast 4步] wall={t_fast:.1f}s engine={r_fast['engine']} "
      f"model={r_fast['model']}")

t0 = time.perf_counter()
r_q = comfy_paint_generate(dict(base))  # 不传 steps/cfg → preset 档
t_q = time.perf_counter() - t0
r_q["images"][0].save(
    r"E:\OmniSpace\data\comfyui\output\w3c_ab_comfy_preset.png", "PNG")
print(f"[comfy-preset 档] wall={t_q:.1f}s engine={r_q['engine']}")
print("COMFY-SIDE-DONE")
