"""双采+sigma强化+latent放大 参数扫荡驱动（贪心逐旋钮，2026-10-16 用户令）。

基线 = h3v_2ssig（480p/10步 → 上采样 → 精修 simple/10步/denoise0.3/tail+3/线性）。
每个参数一轮，赢家保留进下一轮；全部产物同种子同后处理（FaceRefine+CAS）。

参数旋钮：
  --denoise          精修重加噪强度（默认 0.3）
  --refine-steps     精修基础步数（默认 10，另加尾段）
  --refine-scheduler 精修调度器（默认 simple）
  --tail-steps       尾段细化步数（默认 3）
  --tail-spacing     尾段间距（默认 video_sigma_linear）
  --stage1-steps     一段采样步数（默认 10）
  --refine-sampler   精修采样器（默认 res_multistep）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ROW_ID = "0f67d2d85e024ea2aeff2bdbaafc8427"
SECONDS = 10.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--denoise", type=float, default=0.3)
    ap.add_argument("--refine-steps", type=int, default=10)
    ap.add_argument("--refine-scheduler", default="simple")
    ap.add_argument("--tail-steps", type=int, default=3)
    ap.add_argument("--tail-spacing", default="video_sigma_linear")
    ap.add_argument("--stage1-steps", type=int, default=10)
    ap.add_argument("--refine-sampler", default="res_multistep")
    args = ap.parse_args()

    import src.services.inference.h3_chain_engine as hce
    from src.data.database import get_db_safe
    from src.services.inference.comfy_proc import COMFY_OUTPUT_DIR

    engine = hce.get_h3_engine()
    engine._ensure_running(lambda f, s: print(f"[boot] {f:.2f} {s}", flush=True))

    db = get_db_safe()
    r = db.query_one(
        "SELECT description, characters, scene, props, asset_ids "
        "FROM storyboard_rows WHERE id=?", (ROW_ID,))
    r = {**r, "id": ROW_ID}
    refs = hce._collect_refs(r, ROW_ID)
    refs = hce._prepare_refs(args.task_id, refs)

    frames = hce.align_h3_frames(SECONDS)
    seed = str(int(__import__("hashlib").md5(
        f"{ROW_ID}|{SECONDS}".encode()).hexdigest()[:8], 16) % 1_000_000)
    shots = [{"id": "shot_01",
              "prompt": [hce._six_section(r["description"], refs, SECONDS)],
              "length": frames, "seed": seed}]
    plan = {"defaults": {"duration_seconds": SECONDS, "steps": args.stage1_steps},
            "shots": shots}
    plan_json = json.dumps(plan, ensure_ascii=False, indent=1)

    graph = hce._render_graph(
        json.loads(hce._load_template_text()), plan_json,
        f"h3chain_{args.task_id}", 864, 480,
        [{"image": x["image"]} for x in refs], args.task_id, 1, turbo=False)

    graph["V1noise"] = {"class_type": "RandomNoise", "inputs": {
        "noise_seed": 20261016}}
    graph["V1sampler"] = {"class_type": "KSamplerSelect", "inputs": {
        "sampler_name": args.refine_sampler}}
    graph["V1sigmas"] = {"class_type": "BasicScheduler", "inputs": {
        "scheduler": args.refine_scheduler, "steps": args.refine_steps,
        "denoise": args.denoise, "model": ["5", 0]}}
    if args.tail_steps > 0:
        graph["V1tail2"] = {
            "class_type": "MiniMaxH3AVTailDetailScheduleT8Advanced",
            "inputs": {
                "sigmas": ["V1sigmas", 0],
                "extra_tail_steps": args.tail_steps,
                "spacing": args.tail_spacing,
                "shift_video": 12.0,
                "shift_audio": 3.0,
                "profile": "custom_strict",
            }}
        refine_sigmas = ["V1tail2", 0]
    else:
        refine_sigmas = ["V1sigmas", 0]
    graph["V1refine"] = {
        "class_type": "MinimaxH3LatentUpscaler3DRefineHandoff",
        "inputs": {
            "latent": ["124", 0],
            "noise": ["V1noise", 0],
            "sampler": ["V1sampler", 0],
            "sigmas": refine_sigmas,
            "model": ["5", 0],
            "positive": ["1703", 0],
            "model_name": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
            "mode": "target dimensions",
            "scale": 2.0,
            "width": 1344,
            "height": 768,
            "megapixels": 1.0,
            "align": 32,
            "keep_proportion": True,
            "lock_audio": True,
            "cfg": 1.0,
            "device": "cuda",
            "precision": "fp16",
            "offload_after_upscale": False,
        }}
    graph["130"]["inputs"]["samples"] = ["V1refine", 0]
    graph["131"]["inputs"]["samples"] = ["V1refine", 0]

    t0 = time.monotonic()
    resp = engine._api("POST", "/prompt",
                       body={"prompt": graph, "client_id": f"omnispace-{args.task_id}"},
                       timeout=30.0)
    if resp.get("error") or resp.get("node_errors"):
        print("SUBMIT-FAIL", json.dumps(
            resp.get("node_errors") or resp.get("error"), ensure_ascii=False)[:600],
            flush=True)
        return 1
    print(f"[cfg] dn={args.denoise} rs={args.refine_steps}/{args.refine_scheduler}"
          f" tail={args.tail_steps}/{args.tail_spacing} s1={args.stage1_steps}"
          f" samp={args.refine_sampler}", flush=True)
    prompt_id = str(resp["prompt_id"])
    print("submitted prompt_id:", prompt_id, flush=True)

    deadline = time.monotonic() + 60 * 60
    video_rel = None
    while time.monotonic() < deadline:
        time.sleep(10)
        hist = engine._api("GET", f"/history/{prompt_id}", timeout=15.0) or {}
        entry = hist.get(prompt_id) or {}
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            print("EXEC-FAIL", json.dumps(
                status.get("messages") or [], ensure_ascii=False)[:600], flush=True)
            return 1
        for node_out in (entry.get("outputs") or {}).values():
            for item in (node_out.get("images") or node_out.get("videos") or []):
                fn = str(item.get("filename") or "")
                sub = str(item.get("subfolder") or "")
                if fn.endswith(".mp4") and "final" in sub:
                    video_rel = f"{sub}/{fn}" if sub else fn
                    break
            if video_rel:
                break
        if video_rel:
            break
        if status.get("completed"):
            print("NO-OUTPUT", flush=True)
            return 1
    if not video_rel:
        print("TIMEOUT", flush=True)
        return 1
    print(f"[timing] raw after {(time.monotonic()-t0)/60:.1f} min", flush=True)

    src = COMFY_OUTPUT_DIR / video_rel
    out_dir = ROOT / "data" / "videos" / ROW_ID[:16]
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{args.task_id}.mp4"
    import shutil
    shutil.move(str(src), str(dest))
    print("RAW-OUT", dest, flush=True)

    from src.services.inference.h3_face_refine import refine_video
    ref_pils = []
    for ref in refs:
        if ref.get("kind") != "character":
            continue
        fp = Path(hce._COMFY_INPUT) / str(ref.get("image") or "")
        if fp.is_file():
            from PIL import Image as _Img
            ref_pils.append(_Img.open(fp).convert("RGB"))
        if len(ref_pils) >= 2:
            break
    post = refine_video(dest, prompt=shots[0]["prompt"][0], ref_images=ref_pils)
    post = hce._sharpen_pass(post, strength=hce._sharpen_strength())
    print(f"[timing] total {(time.monotonic()-t0)/60:.1f} min", flush=True)
    print("FINAL", post, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
