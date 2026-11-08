"""看片辅助：抽帧 + 客观指标（锐度/运动抖动）+ 分组对比图（测试工具）。

指标口径（诚实声明）：
- sharpness = 灰度中心 60% 裁剪的拉普拉斯方差（纹理锐度代理，越高越锐）
- motion = 相邻抽帧(0.6s)灰度平均绝对差（运动量代理）
- jitter = motion 序列的标准差/均值（越大越"一顿一顿"）
均为代理指标，最终裁决结合人工看图。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

BASE = Path(r"E:/OmniSpace/data/videos/h3_ab")
FRAMES = BASE / "frames"
FFMPEG = r"E:/OmniSpace/runtime/ffmpeg/bin/ffmpeg.exe"

GROUP_480 = ["A0", "A1", "A2", "A3", "A4", "B1", "B2", "C1", "C1n", "C2"]
GROUP_720 = ["U0", "U1", "U2", "U3", "D1"]
T_STAMPS = [0.5, 1.1, 1.7, 2.3, 2.9, 3.5, 4.1, 4.7]
SHEET_TS = [1.1, 2.9, 4.7]
CELL = (480, 270)
FONT = ImageFont.truetype(r"C:/Windows/Fonts/arial.ttf", 16)


def extract_frames(vid: str) -> list[Path]:
    out = []
    for i, t in enumerate(T_STAMPS):
        fp = FRAMES / f"{vid}_t{i}.png"
        if not fp.exists():
            subprocess.run(
                [FFMPEG, "-y", "-v", "error", "-ss", str(t), "-i", str(BASE / f"{vid}.mp4"),
                 "-frames:v", "1", str(fp)], check=True)
        out.append(fp)
    return out


def lap_var(img: Image.Image) -> float:
    g = np.asarray(img.convert("L"), dtype=np.float32)
    h, w = g.shape
    g = g[int(h*0.2):int(h*0.8), int(w*0.2):int(w*0.8)]
    k = np.zeros_like(g)
    k[1:-1, 1:-1] = (-4*g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1]
                     + g[1:-1, :-2] + g[1:-1, 2:])
    return float(k.var())


def metrics(vid: str, paths: list[Path]) -> dict:
    laps, diffs = [], []
    prev = None
    for fp in paths:
        im = Image.open(fp)
        laps.append(lap_var(im))
        g = np.asarray(im.convert("L").resize((256, 144)), dtype=np.float32)
        if prev is not None:
            diffs.append(float(np.abs(g - prev).mean()))
        prev = g
    m = float(np.mean(diffs)) if diffs else 0.0
    j = float(np.std(diffs) / m) if m > 0 else 0.0
    return {"sharp": round(float(np.mean(laps)), 1), "motion": round(m, 2),
            "jitter": round(j, 3)}


def label_cell(im: Image.Image, text: str) -> Image.Image:
    cell = Image.new("RGB", CELL, (12, 12, 12))
    ratio = max(CELL[0] / im.width, CELL[1] / im.height)
    im2 = im.resize((int(im.width * ratio), int(im.height * ratio)))
    cell.paste(im2.crop((0, 0, CELL[0], CELL[1])))
    d = ImageDraw.Draw(cell)
    d.rectangle([0, 0, CELL[0], 20], fill=(0, 0, 0))
    d.text((4, 3), text, font=FONT, fill=(255, 255, 90))
    return cell


def build_sheet(group: list[str], names: dict[str, str], ti: int, out: str) -> None:
    cols, rows = 2, (len(group) + 1) // 2
    sheet = Image.new("RGB", (CELL[0] * cols, CELL[1] * rows), (0, 0, 0))
    for i, vid in enumerate(group):
        fp = FRAMES / f"{vid}_t{ti}.png"
        cell = label_cell(Image.open(fp), f"{vid}  {names[vid]}")
        sheet.paste(cell, ((i % cols) * CELL[0], (i // cols) * CELL[1]))
    sheet.save(BASE / out)


def main() -> None:
    FRAMES.mkdir(exist_ok=True)
    names = {
        "A0": "nonturbo-s10", "A1": "turbo6+ref", "A2": "turbo4", "A3": "turbo8",
        "A4": "nonturbo-s16", "B1": "lx2v-ref2v8", "B2": "ckpt850-4",
        "C1": "SingW4A8+t6", "C1n": "SingW4A8-nt10", "C2": "SingGGUF+t6",
        "U0": "nt10-2s", "U1": "turbo2s6+2", "U2": "turbo2s8+2",
        "U3": "SingW4A8-t2s", "D1": "960p-x2",
    }
    out: dict[str, dict] = {}
    for vid in GROUP_480 + GROUP_720:
        paths = extract_frames(vid)
        out[vid] = metrics(vid, paths)
        print(vid, out[vid], flush=True)
    (BASE / "metrics.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    for ti, t in enumerate(SHEET_TS):
        build_sheet(GROUP_480, names, ti, f"sheet480_t{t}.png")
        build_sheet(GROUP_720, names, ti, f"sheet720_t{t}.png")
    print("sheets done", flush=True)


if __name__ == "__main__":
    main()
