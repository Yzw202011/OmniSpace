"""V3 试验：720p 快采 → latent 神经放大到 1080p → 精修（尾段细化）——双两阶段对比。

图手术：与 h3v_2ssig 同构，仅两处不同：
  1. 第一阶段 720p(1344×768) 10 步（非 480p）
  2. 精修目标 1920×1088（1080p 档，32 对齐；keep_proportion 内部适配 7:4）

对比 = h3v_2ssig_fr（480p→720p 版）+ s10q_fr_sharp（720p 直出）。
风险预警：1080p latent 精修显存 ≈ 720p 的 2.1 倍，16G 卡可能 OOM（试了才知道）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TASK_ID = "h3v_2s1080"
ROW_ID = "0f67d2d85e024ea2aeff2bdbaafc8427"
SECONDS = 10.0


def main() -> int:
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
    refs = hce._prepare_refs(TASK_ID, refs)

    frames = hce.align_h3_frames(SECONDS)
    seed = str(int(__import__("hashlib").md5(
        f"{ROW_ID}|{SECONDS}".encode()).hexdigest()[:8], 16) % 1_000_000)
    shots = [{"id": "shot_01",
              "prompt": [hce._six_section(r["description"], refs, SECONDS)],
              "length": frames, "seed": seed}]
    plan = {"defaults": {"duration_seconds": SECONDS, "steps": 10},
            "shots": shots}
    plan_json = json.dumps(plan, ensure_ascii=False, indent=1)

    # 第一阶段：720p 采样
    graph = hce._render_graph(
        json.loads(hce._load_template_text()), plan_json,
        f"h3chain_{TASK_ID}", 1344, 768,
        [{"image": x["image"]} for x in refs], TASK_ID, 1, turbo=False)

    # ---- 两阶段图手术：124 → 上采样(1080p)+精修(尾段+3) → 130/131 ----
    graph["V1noise"] = {"class_type": "RandomNoise", "inputs": {
        "noise_seed": 20261016}}
    graph["V1sampler"] = {"class_type": "KSamplerSelect", "inputs": {
        "sampler_name": "res_multistep"}}
    graph["V1sigmas"] = {"class_type": "BasicScheduler", "inputs": {
        "scheduler": "simple", "steps": 10, "denoise": 0.3,
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
            "latent": ["124", 0],
            "noise": ["V1noise", 0],
            "sampler": ["V1sampler", 0],
            "sigmas": ["V1tail2", 0],
            "model": ["5", 0],
            "positive": ["1703", 0],
            "model_name": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
            "mode": "target dimensions",
            "scale": 2.0,
            "width": 1920,
            "height": 1088,
            "megapixels": 2.0,
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
                       body={"prompt": graph, "client_id": f"omnispace-{TASK_ID}"},
                       timeout=30.0)
    if resp.get("error") or resp.get("node_errors"):
        print("SUBMIT-FAIL", json.dumps(
            resp.get("node_errors") or resp.get("error"), ensure_ascii=False)[:600],
            flush=True)
        return 1
    prompt_id = str(resp["prompt_id"])
    print("submitted prompt_id:", prompt_id, flush=True)

    deadline = time.monotonic() + 90 * 60
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
    dest = out_dir / f"{TASK_ID}.mp4"
    import shutil
    shutil.move(str(src), str(dest))
    print("RAW-OUT", dest, flush=True)

    # ---- 后处理对齐生产链：FaceRefine → 降噪+锐化 ----
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
    print(f"[timing] total {(time.monotonic()-t0)/60:.1f} min", flush=True)
    print("FINAL", post, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
