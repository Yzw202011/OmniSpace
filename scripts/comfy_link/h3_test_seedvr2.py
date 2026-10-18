"""SeedVR2 3B 试验：一步扩散视频修复（FlashVSR 的「更好一点」候选）。

图链：VHS_LoadVideoPath(成片) → SeedVR2LoadDiTModel(fp8 3B, sdpa) +
SeedVR2LoadVAEModel → SeedVR2VideoUpscaler(resolution=1080 短边档,
batch 5, temporal_overlap 2) → VHS_VideoCombine（原音轨锁回）。

对象 = 双采+sig强化 甜点成片 h3v_cmp_2s_fr.mp4（同源对比公平）。
输出 ≈1935×1080（短边 1080，与 2K ESRGAN 2688×1536 / RBVSR 1376×768 三档可比）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TASK_ID = "h3v_seedvr2"
SRC = Path(r"E:\OmniSpace\data\videos\0f67d2d85e024ea2\h3v_cmp_2s_fr.mp4")
_COMFY_BASE = "http://127.0.0.1:8189"


def _api(method: str, path: str, body=None, timeout: float = 10.0):
    if not path.startswith("/"):
        raise ValueError("path 必须以 / 起始")
    url = urllib.parse.urljoin(_COMFY_BASE, path)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in (
            "127.0.0.1", "localhost", "::1"):
        raise ValueError(f"仅允许回环 URL: {url!r}")
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def main() -> int:
    import argparse
    import logging
    ap = argparse.ArgumentParser()
    ap.add_argument("--resolution", type=int, default=1080,
                    help="目标短边像素（768=原尺寸档 / 1536=2K 档）")
    ap.add_argument("--task-suffix", default="")
    args, _ = ap.parse_known_args()
    task_id = TASK_ID + args.task_suffix

    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from src.services.inference.h3_face_refine import _comfy_manager, _wait_comfy_ready
    from src.services.inference.comfy_proc import COMFY_OUTPUT_DIR

    cm = _comfy_manager()
    cm.spawn("comfyui_h3.log")
    if not _wait_comfy_ready(120):
        print("SETUP-FAIL: ComfyUI 未就绪")
        return 1

    wf = {
        "1": {"class_type": "VHS_LoadVideoPath", "inputs": {
            "video": str(SRC), "force_rate": 0, "custom_width": 0,
            "custom_height": 0, "frame_load_cap": 0, "skip_first_frames": 0,
            "select_every_nth": 1, "format": "None"}},
        "2": {"class_type": "SeedVR2LoadDiTModel", "inputs": {
            "model": "seedvr2_ema_3b_fp8_e4m3fn.safetensors",
            "device": "cuda:0", "blocks_to_swap": 0,
            "swap_io_components": False, "offload_device": "none",
            "cache_model": False, "attention_mode": "sdpa"}},
        "3": {"class_type": "SeedVR2LoadVAEModel", "inputs": {
            "model": "ema_vae_fp16.safetensors", "device": "cuda:0",
            "encode_tiled": True, "encode_tile_size": 1024,
            "encode_tile_overlap": 128, "decode_tiled": True,
            "decode_tile_size": 1024, "decode_tile_overlap": 128,
            "tile_debug": "false", "offload_device": "none"}},
        "4": {"class_type": "SeedVR2VideoUpscaler", "inputs": {
            "image": ["1", 0], "dit": ["2", 0], "vae": ["3", 0],
            "seed": 42, "resolution": args.resolution, "max_resolution": 0,
            "batch_size": 5, "uniform_batch_size": True,
            "temporal_overlap": 2, "prepend_frames": 2,
            "color_correction": "lab", "input_noise_scale": 0.0,
            "latent_noise_scale": 0.0, "offload_device": "none",
            "enable_debug": False}},
        "5": {"class_type": "VHS_VideoCombine", "inputs": {
            "images": ["4", 0], "audio": ["1", 2], "frame_rate": 24,
            "loop_count": 0, "filename_prefix": f"seedvr2/{task_id}",
            "format": "video/h264-mp4", "pix_fmt": "yuv420p", "crf": 16,
            "save_metadata": True, "trim_to_audio": False, "pingpong": False,
            "save_output": True}},
    }

    t0 = time.time()
    resp = _api("POST", "/prompt",
                body={"prompt": wf, "client_id": f"omnispace-{task_id}"},
                timeout=30.0)
    if resp.get("error") or resp.get("node_errors"):
        print("SUBMIT-FAIL", json.dumps(
            resp.get("node_errors") or resp["error"], ensure_ascii=False)[:800])
        return 1
    pid = str(resp["prompt_id"])
    print("submitted:", pid, flush=True)

    deadline = time.time() + 240 * 60  # 2K 档 2752x1536 实测 >90min
    while time.time() < deadline:
        time.sleep(15)
        try:
            hist = _api("GET", f"/history/{pid}", timeout=10.0)
            entry = (hist or {}).get(pid)
        except Exception:
            continue
        if entry is None:
            continue
        st = entry.get("status") or {}
        if st.get("status_str") == "error":
            print("EXEC-FAIL", json.dumps(st.get("messages") or [],
                                          ensure_ascii=False)[:800])
            return 1
        for node_out in (entry.get("outputs") or {}).values():
            for item in (node_out.get("gifs") or node_out.get("images") or []):
                fn = str(item.get("filename") or "")
                sub = str(item.get("subfolder") or "")
                if fn.endswith((".mp4", ".webm")):
                    prod = COMFY_OUTPUT_DIR / (Path(sub) / fn if sub else fn)
                    if prod.is_file():
                        out = SRC.with_name(f"{SRC.stem}_seedvr2{args.task_suffix}.mp4")
                        import shutil
                        shutil.move(str(prod), out)
                        print(f"[timing] total {(time.time()-t0)/60:.1f} min")
                        print("FINAL", out)
                        return 0
        if st.get("completed"):
            print("NO-OUTPUT")
            return 1
    print("TIMEOUT")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
