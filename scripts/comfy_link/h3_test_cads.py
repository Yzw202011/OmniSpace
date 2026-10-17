"""V-CADS 试验：CADS 参考条件退火（Visual Reference Annealing）——H3 画质提升（免训练零额外前向）。

图手术：以现行链 @720p×10s 渲染（非 turbo 10 步标准档），在 SigmaShift(5)
与 BasicGuider(121) 之间插入 MiniMaxH3CADSVisualReferenceT8Advanced——每个
去噪步对 Ref2VA 参考图条件 latent 做 CADS 退火加噪（噪声尺度随 σ 在
tau1=0.6~tau2=0.9 窗口渐升渐降），迫使模型在 mid-schedule 阶段更用力地从
参考图取细节。自动关停 H3 原生静态条件加噪防双重破坏。纯 wrapper 数学，
零额外前向，成本=标准档。采样器/调度完全不动（res_multistep 标准档同款）。

对比基线 = h3tune_s10q_fr（同 Rows 同种子 10 步标准档，补同款锐化）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TASK_ID = "h3v_cads"
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
    import hashlib
    seed = int(hashlib.md5(
        f"{ROW_ID}|{SECONDS}".encode()).hexdigest()[:8], 16) % 1_000_000
    shots = [{"id": "shot_01",
              "prompt": [hce._six_section(r["description"], refs, SECONDS)],
              "length": frames, "seed": str(seed)}]
    plan = {"defaults": {"duration_seconds": SECONDS, "steps": 10},
            "shots": shots}
    plan_json = json.dumps(plan, ensure_ascii=False, indent=1)

    # 720p 渲染（非 turbo 标准档 10 步）
    graph = hce._render_graph(
        json.loads(hce._load_template_text()), plan_json,
        f"h3chain_{TASK_ID}", 1344, 768,
        [{"image": x["image"]} for x in refs], TASK_ID, 1, turbo=False)

    # ---- V-CADS 图手术：SigmaShift(5) → CADS patch → BasicGuider(121) ----
    graph["V1cads"] = {
        "class_type": "MiniMaxH3CADSVisualReferenceT8Advanced",
        "inputs": {
            "model": ["5", 0],
            "noise_scale": 0.10,
            "tau1": 0.60,
            "tau2": 0.90,
            "rescale_mix": 1.0,
            "noise_mode": "paper_independent",
            "seed": seed,
        }}
    graph["121"]["inputs"]["model"] = ["V1cads", 0]

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

    deadline = time.monotonic() + 60 * 60
    while time.monotonic() < deadline:
        time.sleep(15)
        hist = engine._api("GET", f"/history/{prompt_id}", timeout=15.0) or {}
        entry = hist.get(prompt_id) or {}
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            print("EXEC-FAIL", json.dumps(
                status.get("messages") or [], ensure_ascii=False)[:600], flush=True)
            return 1
        done = False
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
        if status.get("completed") and not done:
            print("NO-OUTPUT", flush=True)
            return 1
        if done:
            break
    if not done:
        print("TIMEOUT", flush=True)
        return 1

    src = COMFY_OUTPUT_DIR / (f"{sub}/{fn}" if sub else fn)
    out_dir = ROOT / "data" / "videos" / ROW_ID[:16]
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{TASK_ID}.mp4"
    import shutil
    shutil.move(str(src), str(dest))

    # ---- 后处理对齐生产链：FaceRefine → 降噪+CAS 锐化 ----
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
