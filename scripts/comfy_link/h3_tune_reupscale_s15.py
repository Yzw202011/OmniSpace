"""s15 成片补跑 2K 逐帧放大（挡位战役专用）。

h3_frame_upscale._TIMEOUT_S=900s 按 5s 段标定，11.5s 长行 2K 放大在
s15 那趟撞顶超时。本驱动在进程内放宽到 2400s 重试（生产文件不动）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC = ROOT / "data" / "videos" / "0f67d2d85e024ea2" / "h3tune_s15q_fr.mp4"


def main() -> int:
    import src.services.inference.h3_frame_upscale as fu

    fu._TIMEOUT_S = 2400.0  # 试验进程内放宽，生产常量不动
    # 冷启动竞态修复：放大模块就绪探测仅 10s，独立进程须先经引擎
    # 托管链等 ComfyUI API 真就绪（与主链采样前的等待同源）
    from src.services.inference.h3_engine import get_h3_engine
    engine = get_h3_engine()
    engine._ensure_running(lambda f, s: print(f"[boot] {f:.2f} {s}",
                                              flush=True))
    t0 = time.monotonic()
    out = fu.upscale_video(
        SRC, progress_cb=lambda f, s: print(f"[reup] {f:.2f} {s}",
                                            flush=True))
    print("DONE", out, f"{(time.monotonic() - t0) / 60:.1f}min", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
