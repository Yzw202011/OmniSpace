"""W3-C 四视图格数首过率复验（2026-09-13）：8步×5 + 12步×3，自动数格。

数格启发式：按列扫描「跨全高的黑色分隔线」簇数 n → 格数 = n+1。
完整判定 = 恰好 4 格。产物 w3c_fp_{steps}_{i}.png。
"""
import sys
import time

sys.path.insert(0, r"E:\OmniSpace")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from backend.api.manga.comic_gen import _ComfyGenAdapter  # noqa: E402

PROMPT = (
    "角色设定图。角色：少年侠客阿澜，十六岁，束发，青衫剑客，"
    "月白长袍腰束墨带。画面：同一角色的四视图，从左到右依次为"
    "正面、侧面、背面、特写，四格等宽竖格，单人。"
    "背景：纯白色，均匀干净。"
    "美术风格：韩国网漫风，干净线稿，清晰上色，表情表现力强。"
)


def count_cells(path: str) -> int:
    img = np.asarray(Image.open(path).convert("L"))
    h, w = img.shape
    dark_ratio = (img < 90).mean(axis=0)  # 每列黑像素占比
    is_sep = dark_ratio > 0.85
    seps = 0
    prev = False
    for x in range(w // 10, w * 9 // 10):  # 忽略左右边缘画框
        if is_sep[x] and not prev:
            seps += 1
        prev = is_sep[x]
    return seps + 1


adapter = _ComfyGenAdapter()
results = {"8": [], "12": []}
for steps, runs in ((8, 5), (12, 3)):
    for i in range(1, runs + 1):
        params = {"prompt": PROMPT, "negative": "", "steps": steps,
                  "cfg": 1.0, "seed": 1000 + i, "width": 2560,
                  "height": 1440}
        t0 = time.perf_counter()
        r = adapter.generate(params)
        dt = time.perf_counter() - t0
        out = (rf"E:\OmniSpace\data\comfyui\output"
               rf"\w3c_fp_{steps}_{i}.png")
        r["images"][0].save(out, "PNG")
        cells = count_cells(out)
        results[str(steps)].append(cells)
        print(f"[{steps}步 run{i}] cells={cells} "
              f"{'PASS' if cells == 4 else 'MISS'} wall={dt:.1f}s")

for k, v in results.items():
    ok = sum(1 for c in v if c == 4)
    print(f"== {k}步首过率: {ok}/{len(v)} ==")
print("FP-DONE")
