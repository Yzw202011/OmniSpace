"""FlashVSR 试验：一步流式扩散视频修复（对比 2K ESRGAN / SR-DOWN）。

图链：VHS_LoadVideoPath(成片) → FlashVSR Model(tiny/bf16) → Plan(quality_locked)
→ Restore(scale=2, color_fix, lock 音频) → VHS_VideoCombine。
输出 2752×1536（与 ESRGAN 2K 2688×1536 同级可比）。

对象 = 双采+sig强化 甜点成片 h3v_cmp_2s_fr.mp4（同源对比公平）。
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

TASK_ID = "h3v_flashvsr"
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
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from src.services.inference.comfy_proc import COMFY_OUTPUT_DIR
    from src.services.inference.h3_face_refine import _comfy_manager, _wait_comfy_ready

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
        "2": {"class_type": "MiniMaxH3FlashVSRModelT8Advanced", "inputs": {
            "model_directory": "FlashVSR-v1.1", "mode": "tiny",
            "precision": "bf16"}},
        "3": {"class_type": "MiniMaxH3FlashVSRExecutionPlanT8Advanced", "inputs": {
            "frames": ["1", 0], "quality_profile": "memory_safe",
            "spatial_strategy": "auto", "memory_policy": "auto",
            "base_attention_budget": 2.0, "kv_retention": 3.0,
            "local_radius": 11, "tile_size": 128, "tile_overlap": 16}},
        "4": {"class_type": "MiniMaxH3FlashVSRRestoreT8Advanced", "inputs": {
            "flashvsr_model": ["2", 0], "plan": ["3", 0], "frames": ["1", 0],
            "audio": ["1", 2], "scale": 2, "seed": 0, "color_fix": True,
            "release_policy": "offload_after"}},
        "5": {"class_type": "VHS_VideoCombine", "inputs": {
            "images": ["4", 0], "audio": ["4", 2], "frame_rate": 24,
            "loop_count": 0, "filename_prefix": f"flashvsr/{TASK_ID}",
            "format": "video/h264-mp4", "pix_fmt": "yuv420p", "crf": 16,
            "save_metadata": True, "trim_to_audio": False, "pingpong": False,
            "save_output": True}},
    }

    t0 = time.time()
    resp = _api("POST", "/prompt",
                body={"prompt": wf, "client_id": f"omnispace-{TASK_ID}"},
                timeout=30.0)
    if resp.get("error") or resp.get("node_errors"):
        print("SUBMIT-FAIL", json.dumps(
            resp.get("node_errors") or resp["error"], ensure_ascii=False)[:800])
        return 1
    pid = str(resp["prompt_id"])
    print("submitted:", pid, flush=True)

    deadline = time.time() + 60 * 60
    while time.time() < deadline:
        time.sleep(10)
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
                        out = SRC.with_name(f"{SRC.stem}_flashvsr.mp4")
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
