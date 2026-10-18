"""V1 旗舰试验：Latent Upscaler 两阶段（480p 链式采样 → 神经 latent 上采样
→ 720p 低σ精修）——对比 720p 直出的清晰度/质感差异（2026-10-13 用户令）。

图手术：以 h3_chain 模板 @480p 渲染，在 SamplerCustomAdvanced(124) 与
VAEDecode(130/131) 之间插入 MinimaxH3LatentUpscaler3DRefineHandoff
（learned 上采样+低σ精修单节点集成，原生 joint AV latent 路径）。

后处理与生产链一致：FaceRefine → 降噪+CAS 锐化（_sharpen_pass）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TASK_ID = "h3v1_latent2stage"
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
    ref_imgs = []
    for ref in refs:
        if ref.get("kind") != "character":
            continue
        fp = Path(hce._COMFY_INPUT) / str(ref.get("image") or "")
        if fp.is_file():
            from PIL import Image as _Img
            ref_imgs.append(_Img.open(fp).convert("RGB"))
        if len(ref_imgs) >= 2:
            break

    frames = hce.align_h3_frames(SECONDS)
    seed = str(int(__import__("hashlib").md5(
        f"{ROW_ID}|{SECONDS}".encode()).hexdigest()[:8], 16) % 1_000_000)
    shots = [{"id": "shot_01",
              "prompt": [hce._six_section(r["description"], refs, SECONDS)],
              "length": frames, "seed": seed}]
    plan = {"defaults": {"duration_seconds": SECONDS, "steps": 10},
            "shots": shots}
    plan_json = json.dumps(plan, ensure_ascii=False, indent=1)

    graph = hce._render_graph(
        json.loads(hce._load_template_text()), plan_json,
        f"h3chain_{TASK_ID}", 864, 480,
        [{"image": x["image"]} for x in refs], TASK_ID, 1, turbo=False)

    # ---- V1 图手术：124 输出 → 神经上采样+低σ精修 → 130/131 ----
    graph["V1noise"] = {"class_type": "RandomNoise", "inputs": {
        "noise_seed": 20261013}}
    graph["V1sampler"] = {"class_type": "KSamplerSelect", "inputs": {
        "sampler_name": "res_multistep"}}
    graph["V1sigmas"] = {"class_type": "BasicScheduler", "inputs": {
        "scheduler": "simple", "steps": 10, "denoise": 0.3,
        "model": ["5", 0]}}
    graph["V1refine"] = {
        "class_type": "MinimaxH3LatentUpscaler3DRefineHandoff",
        "inputs": {
            "latent": ["124", 0],
            "noise": ["V1noise", 0],
            "sampler": ["V1sampler", 0],
            "sigmas": ["V1sigmas", 0],
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
    # 130/131 的采样输入改接精修输出
    graph["130"]["inputs"]["samples"] = ["V1refine", 0]
    graph["131"]["inputs"]["samples"] = ["V1refine", 0]
    # 分辨率声明改 720p（精修后 latent 已是 1344×768）
    # （1700/plan 的宽高保留 480p=第一阶段采样分辨率）

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

    # 轮询产物
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

    src = COMFY_OUTPUT_DIR / video_rel
    out_dir = ROOT / "data" / "videos" / ROW_ID[:16]
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{TASK_ID}.mp4"
    import shutil
    shutil.move(str(src), str(dest))
    print("RAW-OUT", dest, flush=True)

    # ---- 后处理对齐生产链：FaceRefine → 降噪+锐化 ----
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
    print("FINAL", post, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
