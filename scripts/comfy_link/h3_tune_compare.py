"""H3 挡位战役对比拼图：多支成片按同一组时间比例抽帧并排成网格。

用法：
  runtime/py312/python.exe scripts/comfy_link/h3_tune_compare.py \
      --out data/h3_tune/compare_phase1.png \
      --video "6步turbo|data/videos/0f67d2d85e024ea2/9cb33e1b47d24be1b07bd73af8e7f122_fr_fu.mp4" \
      --video "10步|data/videos/0f67d2d85e024ea2/h3tune_s10q_fr_fu.mp4" ...

抽帧位置取时长比例（默认 8% / 44% / 84%），对齐"远景/双人中景/双人近景"
三个关键镜位（以 9cb33e1b 基线实测定位），成片帧数微差不受影响。
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

FFMPEG = Path("runtime/ffmpeg/bin/ffmpeg.exe")
FRACTIONS = (0.08, 0.44, 0.84)
CELL_W = 640  # 单元格宽，高按源比例


def probe_duration(mp4: Path) -> float:
    out = subprocess.run(
        ["runtime/ffmpeg/bin/ffprobe.exe", "-v", "error", "-show_entries",
         "format=duration", "-of", "csv=p=0", str(mp4)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def grab(mp4: Path, t: float, dst: Path) -> None:
    subprocess.run(
        [str(FFMPEG), "-v", "error", "-ss", f"{t:.3f}", "-i", str(mp4),
         "-frames:v", "1", "-y", str(dst)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", action="append", required=True,
                    help="标签|路径")
    ap.add_argument("--out", default="data/h3_tune/compare.png")
    ap.add_argument("--shots", default="0.08,0.44,0.84",
                    help="逗号分隔的时长比例，默认对齐远景/双人中景/双人近景")
    args = ap.parse_args()
    fracs = [float(c) for c in args.shots.split(",") if c.strip()] or list(FRACTIONS)

    root = Path(args.out).parent
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for spec in args.video:
        label, _, path = spec.partition("|")
        mp4 = Path(path)
        dur = probe_duration(mp4)
        cells = []
        for i, f in enumerate(fracs):
            dst = root / f"_c_{mp4.stem}_{i}.png"
            grab(mp4, dur * f, dst)
            cells.append(Image.open(dst))
        rows.append((label, cells))

    cw = CELL_W
    ch = int(rows[0][1][0].height * cw / rows[0][1][0].width)
    pad, label_h = 6, 34
    W = pad + len(fracs) * (cw + pad)
    H = pad + len(rows) * (ch + label_h + pad)
    sheet = Image.new("RGB", (W, H), (18, 18, 24))
    draw = ImageDraw.Draw(sheet)
    for r, (label, cells) in enumerate(rows):
        y = pad + r * (ch + label_h + pad)
        draw.text((pad + 4, y + 6), label, fill=(240, 240, 240))
        for c, img in enumerate(cells):
            x = pad + c * (cw + pad)
            sheet.paste(img.resize((cw, ch)), (x, y + label_h))
    sheet.save(args.out)
    print("SAVED", args.out, f"{W}x{H}")
    for p in root.glob("_c_*.png"):
        p.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
