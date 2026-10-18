"""DLSS-NR 试验：NVIDIA DLSS 2x 超分+降噪（RTX 50 系原生加速）——FlashVSR 的 NVIDIA 正统替代。

图链：LoadVideo(成片) → MiniMaxH3DLSSNRRuntimeAuditT8Advanced（运行时/驱动审计）
→ MiniMaxH3DLSSNRVideoFileT8Advanced（mode=sr_nr, scale 2.0, 流式有界处理）
→ saved_path 产物回捞。外部程序 video2dlssnr.exe 调 nvngx DLSS DLL。

前置布局（models/DLSS-NR/1.3/）：t8-runtime-manifest.json + video2dlssnr_release.zip
+ bin/{exe, 三 DLL}。驱动要求 ≥616.56（本机 616.92 ✓）。
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

TASK_ID = "h3v_dlssnr_cine"
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
    import os
    import shutil
    knob = {k: os.environ.get("DLSSNR_" + k, "") for k in
            ("global_tone", "local_tone", "intensity", "color", "skin", "style")}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from src.services.inference.comfy_proc import COMFY_INPUT_DIR
    from src.services.inference.h3_face_refine import _comfy_manager, _wait_comfy_ready

    cm = _comfy_manager()
    cm.spawn("comfyui_h3.log")
    if not _wait_comfy_ready(120):
        print("SETUP-FAIL: ComfyUI 未就绪")
        return 1

    # 核心 LoadVideo 只认 input 目录内文件：拷源片进去
    COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    local_name = f"dlssnr_{TASK_ID}.mp4"
    shutil.copy2(SRC, COMFY_INPUT_DIR / local_name)

    wf = {
        "1": {"class_type": "LoadVideo", "inputs": {"file": local_name}},
        "2": {"class_type": "MiniMaxH3DLSSNRRuntimeAuditT8Advanced", "inputs": {
            "runtime_version": "1.3", "probe_mode": "feature_probe_1_frame",
            "cuda_device_index": 0, "dxgi_adapter_index": 0}},
        "3": {"class_type": "MiniMaxH3DLSSNRVideoFileT8Advanced", "inputs": {
            "dlss_nr_runtime": ["2", 0], "source_video": ["1", 0],
            "mode": "sr_nr", "scale": "2.0", "motion_engine": "auto",
            "sr_preset": "default", "nr_preset": "0 Default",
            "nr_style": knob["style"] or "2 Cinematic",
            "nr_intensity": float(knob["intensity"] or 1.2),
            "nr_local_structure": 1.0,
            "nr_local_tone": float(knob["local_tone"] or 1.0),
            "nr_skin": float(knob["skin"] or 1.0),
            "nr_global_tone": float(knob["global_tone"] or -1.0),
            "nr_detail": 0.9, "nr_color": float(knob["color"] or 0.8),
            "nr_ui_correction": False,
            "nr_auto_mask": False, "quality_profile": "custom",
            "cuda_device_index": 0, "probe_mode": "feature_probe_1_frame",
            "dxgi_adapter_index": 0,
            "filename_prefix": f"dlssnr/{TASK_ID}", "crf": 16.0}},
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
            sp = node_out.get("saved_path")
            if isinstance(sp, str) and sp:
                dest = Path(sp)
                print(f"[timing] total {(time.time()-t0)/60:.1f} min")
                print("FINAL", dest)
                return 0
        if st.get("completed"):
            print("NO-OUTPUT（无 saved_path）")
            return 1
    print("TIMEOUT")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
