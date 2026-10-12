"""H3 挡位战役·后处理变体驱动：对已生成的 _fr 母片重跑放大并调 blend。

blend_factor 语义（h3_frame_upscale）：ESRGAN 4x 结果与 lanczos 回缩版
的混合比——低=保留更多超分锐度，高=更平滑。生产定档 0.55（e7dfc37）。
本驱动只动试验进程，不碰生产常量。

用法：
  runtime/py312/python.exe scripts/comfy_link/h3_tune_post.py \
      --src data/videos/0f67d2d85e024ea2/h3tune_s20q_fr.mp4 --blend 0.45
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="_fr 母片路径")
    ap.add_argument("--blend", type=float, required=True)
    ap.add_argument("--tag", default="", help="产物名后缀标记")
    args = ap.parse_args()

    import src.services.inference.h3_frame_upscale as fu

    fu._TIMEOUT_S = 2400.0  # 长行预算放宽（生产 900s 按 5s 段标定）
    from src.services.inference.h3_engine import get_h3_engine
    get_h3_engine()._ensure_running(
        lambda f, s: print(f"[boot] {f:.2f} {s}", flush=True))

    t0 = time.monotonic()
    out = fu.upscale_video(
        Path(args.src), blend_factor=args.blend,
        progress_cb=lambda f, s: print(f"[post] {f:.2f} {s}", flush=True))
    tag = args.tag or f"b{args.blend}"
    dest = out.with_name(f"{out.stem}_{tag}{out.suffix}")
    out.rename(dest)
    print("DONE", dest, f"{(time.monotonic() - t0) / 60:.1f}min", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
