"""V-GenRefine 试验：生成式精修（分辨率不变）——成片 VAE 编回 latent 低强度重精修。

图链：VHS_LoadVideoPath(成片) → VAEEncode(video vae) + VAEEncodeAudio(audio vae)
→ MiniMaxH3AVLatentBuilderT8Advanced 打包 → RefineHandoff（denoise 0.3 +
尾段+3，lock_audio=True 音频零噪+原样透传，mode target dimensions=源尺寸）
→ 链式 130/131 解码 → 正常产出（音轨=原声）。

链式循环照常保留（1703 正则条件依赖它）；124 号采样器无人消费自动跳过。
风险预告：VAE 往返损耗 / 精修致运动漂移 / Contex-Loop 契约拒绝外部 latent——
任一翻车即按用户令返回。
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
    ap.add_argument("--task-id", default="h3v_genrefine")
    ap.add_argument("--src", required=True)
    ap.add_argument("--denoise", type=float, default=0.3)
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
    shots = [{"id": "shot_01",
              "prompt": [hce._six_section(r["description"], refs, SECONDS)],
              "length": frames, "seed": "100000"}]
    plan = {"defaults": {"duration_seconds": SECONDS, "steps": 10},
            "shots": shots}
    plan_json = json.dumps(plan, ensure_ascii=False, indent=1)

    # 链式图 @源尺寸（循环只供条件；124 无消费者自动跳过）
    graph = hce._render_graph(
        json.loads(hce._load_template_text()), plan_json,
        f"h3chain_{args.task_id}", 1376, 768,
        [{"image": x["image"]} for x in refs], args.task_id, 1, turbo=False)

    # ---- 编码链：成片 → 视频/音频 latent → 打包 ----
    graph["V1vhs"] = {"class_type": "VHS_LoadVideoPath", "inputs": {
        "video": str(Path(args.src).resolve()), "force_rate": 0,
        "custom_width": 0, "custom_height": 0, "frame_load_cap": 0,
        "skip_first_frames": 0, "select_every_nth": 1, "format": "None"}}
    graph["V1venc"] = {"class_type": "VAEEncode", "inputs": {
        "pixels": ["V1vhs", 0], "vae": ["3", 0]}}
    graph["V1aenc"] = {"class_type": "VAEEncodeAudio", "inputs": {
        "audio": ["V1vhs", 2], "vae": ["4", 0]}}
    graph["V1pack"] = {"class_type": "MiniMaxH3AVLatentBuilderT8Advanced", "inputs": {
        "video_latent": ["V1venc", 0], "audio_latent": ["V1aenc", 0]}}

    # ---- 精修段（同两阶段甜点：simple/10 步/dn0.3/尾段+3）----
    graph["V1noise"] = {"class_type": "RandomNoise", "inputs": {
        "noise_seed": 20261017}}
    graph["V1sampler"] = {"class_type": "KSamplerSelect", "inputs": {
        "sampler_name": "res_multistep"}}
    graph["V1sigmas"] = {"class_type": "BasicScheduler", "inputs": {
        "scheduler": "simple", "steps": 10, "denoise": args.denoise,
        "model": ["5", 0]}}
    graph["V1tail2"] = {
        "class_type": "MiniMaxH3AVTailDetailScheduleT8Advanced",
        "inputs": {
            "sigmas": ["V1sigmas", 0],
            "extra_tail_steps": 3,
            "spacing": "video_sigma_linear",
            "shift_video": 12.0,
            "shift_audio": 3.0,
            "profile": "custom_strict",
        }}
    graph["V1refine"] = {
        "class_type": "MinimaxH3LatentUpscaler3DRefineHandoff",
        "inputs": {
            "latent": ["V1pack", 0],
            "noise": ["V1noise", 0],
            "sampler": ["V1sampler", 0],
            "sigmas": ["V1tail2", 0],
            "model": ["5", 0],
            "positive": ["1703", 0],
            "model_name": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
            "mode": "target dimensions",
            "scale": 1.0,
            "width": 1376,
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

    t0 = time.time()
    resp = engine._api("POST", "/prompt",
                       body={"prompt": graph, "client_id": f"omnispace-{args.task_id}"},
                       timeout=30.0)
    if resp.get("error") or resp.get("node_errors"):
        print("SUBMIT-FAIL", json.dumps(
            resp.get("node_errors") or resp.get("error"), ensure_ascii=False)[:800],
            flush=True)
        return 1
    prompt_id = str(resp["prompt_id"])
    print("submitted prompt_id:", prompt_id, flush=True)

    deadline = time.monotonic() + 45 * 60
    video_rel = None
    while time.monotonic() < deadline:
        time.sleep(10)
        hist = engine._api("GET", f"/history/{prompt_id}", timeout=15.0) or {}
        entry = hist.get(prompt_id) or {}
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            print("EXEC-FAIL", json.dumps(
                status.get("messages") or [], ensure_ascii=False)[:800], flush=True)
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
    print(f"[timing] total {(time.monotonic()-t0)/60:.1f} min", flush=True)
    print("FINAL", dest, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
