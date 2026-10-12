"""H3 挡位战役驱动（2026-10-11 用户令：6/10/15/20 步扫描，10~20 支找优）。

试验专用，不进生产链：本进程内 monkeypatch config（manga.h3_steps /
manga.h3_turbo），直调 run_h3_chain_task 复用完整链（六段式计划→链式采样
→FaceRefine→逐帧放大→静音轨）。种子=md5(行ID|秒数) 引擎内确定性生成，
同参数天然同种子。计时与产物落账 data/h3_tune/results.jsonl。

用法（runtime 解释器，cwd 任意）：
  runtime/py312/python.exe scripts/comfy_link/h3_tune_steps.py --steps 20 --turbo 0
  runtime/py312/python.exe scripts/comfy_link/h3_tune_steps.py --steps 6  --turbo 1
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 与参照任务 9cb33e1b（6 步 turbo 基线，18.8min）同行同秒同档——
# 同种子、同 277 帧、同 864x480，产出可与基线逐帧对齐比较
ROW_ID = "0f67d2d85e024ea2aeff2bdbaafc8427"
SECONDS = 11.5  # align_h3_frames → 277 帧（17k+5 网格，与基线一致）
QUALITY = "480p"  # 864x480


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, required=True)
    ap.add_argument("--turbo", type=int, default=0, help="1=turbo 蒸馏链")
    ap.add_argument("--task-id", default="")
    ap.add_argument("--seconds", type=float, default=SECONDS)
    ap.add_argument("--quality", default=QUALITY, help="480p/720p")
    args = ap.parse_args()

    import src.services.inference.h3_chain_engine as hce

    real_get_config = hce.get_config

    def tuned_config() -> dict:
        cfg = real_get_config()
        manga = dict(cfg.get("manga") or {})
        manga["h3_steps"] = args.steps
        manga["h3_turbo"] = bool(args.turbo)
        return {**cfg, "manga": manga}

    hce.get_config = tuned_config

    task_id = args.task_id or (
        f"h3tune_s{args.steps}" + ("t" if args.turbo else "q"))
    t0 = time.monotonic()
    result = hce.run_h3_chain_task(
        task_id, [ROW_ID], args.seconds, args.quality, aspect="16:9",
        progress_cb=lambda f, s: print(
            f"[{task_id}] {f:.2f} {s}", flush=True))
    wall_min = round((time.monotonic() - t0) / 60.0, 2)
    rec = {"task_id": task_id, "steps": args.steps, "turbo": bool(args.turbo),
           "seconds": args.seconds, "quality": args.quality,
           "wall_minutes": wall_min, **result}
    out = ROOT / "data" / "h3_tune" / "results.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("DONE " + json.dumps(rec, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
