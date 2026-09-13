"""W3-C inpaint A/B（2026-09-13）：legacy diffusers 4B vs comfy 潜空间修复。

测试床：w3c_fp_raw28_1.png（legacy onepass 四格 PASS 图，2560×1440）。
遮罩第 2 格（侧面视图），prompt 复刻 _repair_view_cell 侧面锚点词。
判据：①未遮罩三格逐像素保留 ②修复格内容合理。
产物 w3c_inpaint_legacy.png / w3c_inpaint_comfy.png + 差异统计。
"""
import sys
import time

sys.path.insert(0, r"E:\OmniSpace")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

SRC = r"E:\OmniSpace\data\comfyui\output\w3c_fp_raw28_1.png"
IDX = 1  # 第 2 格（侧面）
PROMPT = (
    "角色设定图。角色：少年侠客阿澜，十六岁，束发，青衫剑客，"
    "月白长袍腰束墨带。画面：同一角色的侧面视图，单人。构图要求："
    "身体正侧对镜头呈90度，双肩重叠成一条线，只见单侧脸部轮廓与"
    "单只眼睛，从头到脚完整。背景：纯白色，均匀干净。"
    "美术风格：韩国网漫风，干净线稿，清晰上色。"
)
SEED = 20260913

base = Image.open(SRC).convert("RGB")
W, H = base.size
cell_w = W // 4
mask = Image.new("L", (W, H), 0)
ImageDraw.Draw(mask).rectangle([IDX * cell_w, 0, (IDX + 1) * cell_w - 1, H - 1],
                               fill=255)
mask.save(r"E:\OmniSpace\data\comfyui\output\w3c_inpaint_mask.png", "PNG")


def outside_diff(a: Image, b: Image) -> int:
    """未遮罩区逐像素差异（应≈0）。"""
    aa = np.asarray(a, dtype=np.int16)
    bb = np.asarray(b, dtype=np.int16)
    outside = np.ones((H, W), dtype=bool)
    outside[:, IDX * cell_w:(IDX + 1) * cell_w] = False
    d = np.abs(aa - bb).sum(axis=2)
    return int((d[outside] > 8).sum())


# ── comfy 侧 ──────────────────────────────────────────────
from backend.services.inference.comfy_paint_engine import (  # noqa: E402
    get_comfy_paint_engine,
)

eng = get_comfy_paint_engine()
t0 = time.perf_counter()
rc = eng.inpaint({"prompt": PROMPT, "negative": "", "seed": SEED,
                  "steps": 28, "cfg": 4.0}, base, mask)
tc = time.perf_counter() - t0
comfy_out = rc["images"][0].convert("RGB")
comfy_out.save(r"E:\OmniSpace\data\comfyui\output\w3c_inpaint_comfy.png",
               "PNG")
print(f"[comfy inpaint] wall={tc:.1f}s 未遮罩区差异像素="
      f"{outside_diff(base, comfy_out)}")

# ── legacy 侧（comfy 实例退出后跑，避免 16G 叠载）──────
eng.unload()
time.sleep(3)
from backend.services.inference.paint_engine import get_paint_engine  # noqa: E402

leg = get_paint_engine()
assert leg.ensure_loaded("flux2-klein-4b"), "legacy 4B 装载失败"
t0 = time.perf_counter()
rl = leg.inpaint({"prompt": PROMPT, "steps": 28, "cfg": 4.5,
                  "seed": SEED, "mask_margin": 8}, base, mask)
tl = time.perf_counter() - t0
legacy_out = rl["images"][0].convert("RGB")
legacy_out.save(r"E:\OmniSpace\data\comfyui\output\w3c_inpaint_legacy.png",
                "PNG")
print(f"[legacy inpaint] wall={tl:.1f}s 未遮罩区差异像素="
      f"{outside_diff(base, legacy_out)}")
print("INPAINT-AB-DONE")
