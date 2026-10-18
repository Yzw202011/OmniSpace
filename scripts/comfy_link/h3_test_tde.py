"""TDE 试验：时序保护细节增强（分辨率不变，非超分纯画质提升）。

图链：VHS_LoadVideoPath → MiniMaxH3TemporalDetailEnhanceT8Advanced
（upscale_factor=1.0 分辨率保持；运动门控锐化：动区收敛防闪烁）
→ VHS_VideoCombine（原音轨锁回，h264 crf16）。

对象 = 双采+sig强化 甜点成片 h3v_cmp_2s_fr.mp4（1344×768，今日对打赛产物）。
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

TASK_ID = "h3v_tde"
SRC = Path(r"E:\OmniSpace\data\videos\0f67d2d85e024ea2\h3v_cmp_2s_fr.mp4")
_COMFY_BASE = "http://127.0.0.1:8189"


def _api(method: str, path: str, body=None, timeout: float = 10.0):
    # SSRF 边界：回环字面量基址 + 拼接后解析校验（同 h3_face_refine 形状）
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
        "2": {"class_type": "MiniMaxH3TemporalDetailEnhanceT8Advanced", "inputs": {
            "frames": ["1", 0],
            "upscale_factor": 1.0,
            "strength": 0.35,
            "blur_radius": 2,
            "blur_sigma": 1.2,
            "motion_threshold": 0.04,
            "temporal_guard": 0.85,
            "frame_chunk_size": 8,
            "maximum_output_megapixels": 2.1,
        }},
        "3": {"class_type": "VHS_VideoCombine", "inputs": {
            "images": ["2", 0], "audio": ["1", 2], "frame_rate": 24,
            "loop_count": 0, "filename_prefix": f"tde/{TASK_ID}",
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
            resp.get("node_errors") or resp["error"], ensure_ascii=False)[:600])
        return 1
    pid = str(resp["prompt_id"])
    print("submitted:", pid, flush=True)

    deadline = time.time() + 1800
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
                                          ensure_ascii=False)[:600])
            return 1
        for node_out in (entry.get("outputs") or {}).values():
            for item in (node_out.get("gifs") or node_out.get("images") or []):
                fn = str(item.get("filename") or "")
                sub = str(item.get("subfolder") or "")
                if fn.endswith((".mp4", ".webm")):
                    prod = COMFY_OUTPUT_DIR / (Path(sub) / fn if sub else fn)
                    if prod.is_file():
                        out = SRC.with_name(f"{SRC.stem}_tde.mp4")
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
