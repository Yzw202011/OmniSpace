"""H3 画质技术线矩阵驱动：CADS 退火 × 尾段细化 × RF 重启（可任意组合）。

相对生产链（非 turbo 10 步标准档）的可选图手术层：
  --cads-scale FLOAT   CADS 参考条件退火噪声尺度（5→CADS→121；0/缺省=关）
  --tail-steps INT     尾段细化步数（123→V1tail→124.sigmas；0/缺省=关）
  --restart-sigma FLOAT  RF 重启重加噪 σ（三路接管 121/124.sampler/124.sigmas；0/缺省=关；
                         model 必须接原始 UNETLoader(1) 旁路 SigmaShift 防双重位移；
                         与 CADS 互斥——重启自带 shift patch 且要求独占采样栈）

对比基线 = h3tune_s10q_fr + 同款锐化（生产链同款：10 步+FaceRefine+CAS）。
种子 = md5(行ID|秒数) 与生产引擎一致（刻意 MD5，非安全场景）。
"""
from __future__ import annotations

import argparse
import hashlib
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
    ap.add_argument("--cads-scale", type=float, default=0.0)
    ap.add_argument("--tau1", type=float, default=0.60)
    ap.add_argument("--tau2", type=float, default=0.90)
    ap.add_argument("--tail-steps", type=int, default=0)
    ap.add_argument("--restart-sigma", type=float, default=0.0)
    ap.add_argument("--restart-steps", type=int, default=3)
    args = ap.parse_args()
    if not args.task_id.replace("_", "").isalnum():
        print("BAD-TASK-ID", flush=True)
        return 2

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
    seed = int(hashlib.md5(
        f"{ROW_ID}|{SECONDS}".encode()).hexdigest()[:8], 16) % 1_000_000
    shots = [{"id": "shot_01",
              "prompt": [hce._six_section(r["description"], refs, SECONDS)],
              "length": frames, "seed": str(seed)}]
    plan = {"defaults": {"duration_seconds": SECONDS, "steps": 10},
            "shots": shots}
    plan_json = json.dumps(plan, ensure_ascii=False, indent=1)

    graph = hce._render_graph(
        json.loads(hce._load_template_text()), plan_json,
        f"h3chain_{args.task_id}", 1344, 768,
        [{"image": x["image"]} for x in refs], args.task_id, 1, turbo=False)

    if args.restart_sigma > 0:
        # RF 重启三路接管（独占采样栈，含自带 shift；CADS 不可叠加）
        graph["V1restart"] = {
            "class_type": "MiniMaxH3RectifiedFlowRestartSamplerT8Advanced",
            "inputs": {
                "model": ["1", 0],
                "av_latent": ["1703", 3],
                "steps": 10,
                "shift_video": 12.0,
                "shift_audio": 3.0,
                "restart_video_sigma": args.restart_sigma,
                "restart_steps": args.restart_steps,
                "restart_seed": seed,
            }}
        graph["121"]["inputs"]["model"] = ["V1restart", 0]
        graph["124"]["inputs"]["sampler"] = ["V1restart", 1]
        graph["124"]["inputs"]["sigmas"] = ["V1restart", 2]
    else:
        if args.cads_scale > 0:
            graph["V1cads"] = {
                "class_type": "MiniMaxH3CADSVisualReferenceT8Advanced",
                "inputs": {
                    "model": ["5", 0],
                    "noise_scale": args.cads_scale,
                    "tau1": args.tau1,
                    "tau2": args.tau2,
                    "rescale_mix": 1.0,
                    "noise_mode": "paper_independent",
                    "seed": seed,
                }}
            graph["121"]["inputs"]["model"] = ["V1cads", 0]
        if args.tail_steps > 0:
            graph["V1tail"] = {
                "class_type": "MiniMaxH3AVTailDetailScheduleT8Advanced",
                "inputs": {
                    "sigmas": ["123", 0],
                    "extra_tail_steps": args.tail_steps,
                    "spacing": "video_sigma_linear",
                    "shift_video": 12.0,
                    "shift_audio": 3.0,
                    "profile": "custom_strict",
                }}
            graph["124"]["inputs"]["sigmas"] = ["V1tail", 0]

    resp = engine._api("POST", "/prompt",
                       body={"prompt": graph, "client_id": f"omnispace-{args.task_id}"},
                       timeout=30.0)
    if resp.get("error") or resp.get("node_errors"):
        print("SUBMIT-FAIL", json.dumps(
            resp.get("node_errors") or resp.get("error"), ensure_ascii=False)[:600],
            flush=True)
        return 1
    prompt_id = str(resp["prompt_id"])
    print(f"[cfg] cads={args.cads_scale} tail={args.tail_steps} "
          f"restart={args.restart_sigma}/{args.restart_steps}", flush=True)
    print("submitted prompt_id:", prompt_id, flush=True)

    deadline = time.monotonic() + 60 * 60
    done = False
    while time.monotonic() < deadline:
        time.sleep(15)
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
                    print("RAW-OUT", f"{sub}/{fn}" if sub else fn, flush=True)
                    done = True
                    break
            if done:
                break
        if done:
            break
        if status.get("completed") and not done:
            print("NO-OUTPUT", flush=True)
            return 1
    if not done:
        print("TIMEOUT", flush=True)
        return 1

    src = COMFY_OUTPUT_DIR / (f"{sub}/{fn}" if sub else fn)
    out_dir = ROOT / "data" / "videos" / ROW_ID[:16]
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{args.task_id}.mp4"
    import shutil
    shutil.move(str(src), str(dest))

    # 后处理对齐生产链：FaceRefine → 降噪+CAS 锐化
    from src.services.inference.h3_face_refine import refine_video
    from src.services.inference.h3_chain_engine import _sharpen_pass
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
    print("FINAL", post, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
