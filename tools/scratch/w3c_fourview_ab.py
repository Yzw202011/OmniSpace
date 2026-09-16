"""W3-C 步3 GPU 实弹：四视图 onepass 口径（2560×1440，adapter 真实路径）。

走 comic_gen._ComfyGenAdapter（与产品代码同路径，28 步重映射 8 步）；
img2img 变体用已出样张作参考图（ReferenceLatent 条件）。
"""
import sys
import time

sys.path.insert(0, r"E:\OmniSpace")

from PIL import Image  # noqa: E402

from src.api.manga.comic_gen import _ComfyGenAdapter  # noqa: E402

PROMPT = (
    "角色设定图。角色：少年侠客阿澜，十六岁，束发，青衫剑客，"
    "月白长袍腰束墨带。画面：同一角色的四视图，从左到右依次为"
    "正面、侧面、背面、特写，四格等宽竖格，单人。"
    "背景：纯白色，均匀干净。"
    "美术风格：韩国网漫风，干净线稿，清晰上色，表情表现力强。"
)
adapter = _ComfyGenAdapter()
params = {"prompt": PROMPT, "negative": "", "steps": 28, "cfg": 4.5,
          "seed": 20260913, "width": 2560, "height": 1440}

t0 = time.perf_counter()
r = adapter.generate(params)
t_gen = time.perf_counter() - t0
img = r["images"][0]
img.save(r"E:\OmniSpace\data\comfyui\output\w3c_fourview_comfy8s.png", "PNG")
print(f"[onepass comfy 8步(28重映射)] {img.size} wall={t_gen:.1f}s")

ref = Image.open(r"E:\OmniSpace\data\comfyui\output\w3c_ab_comfy_preset.png")
t0 = time.perf_counter()
r2 = adapter.img2img({**params, "seed": 20260914}, ref.convert("RGB"))
t2 = time.perf_counter() - t0
r2["images"][0].save(
    r"E:\OmniSpace\data\comfyui\output\w3c_fourview_comfy_ref.png", "PNG")
print(f"[onepass comfy ref条件] wall={t2:.1f}s")
print("FOURVIEW-SIDE-DONE")
