"""DLSS-NR 色调甜点扫荡：暖肤（反雪白）+ 提亮，四变体批跑。

基座 = 上轮胜出暖肤组合：Cinematic/Natural 风格 + intensity 1.2 + skin 1.0
+ color 0.8 + detail 0.9。变量 = 提亮策略（global_tone / local_tone）。
每版 ~93s；产物自动从 output/dlssnr/ 回捞并测肤色+全帧亮度。
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

SRC = Path(r"E:\OmniSpace\data\videos\0f67d2d85e024ea2\h3v_cmp_2s_fr.mp4")
OUT_DIR = Path(r"E:\OmniSpace\data\comfyui\output\dlssnr")
VID_DIR = Path(r"E:\OmniSpace\data\videos\0f67d2d85e024ea2")
_COMFY_BASE = "http://127.0.0.1:8189"

VARIANTS = [
    # (suffix, style, intensity, color, detail, global_tone, local_tone)
    ("b1", "2 Cinematic", 1.2, 0.8, 0.9, 0.8, 1.0),
    ("b2", "2 Cinematic", 1.2, 0.8, 0.9, 1.4, 1.0),
    ("b3", "2 Cinematic", 1.2, 0.8, 0.9, 1.0, 1.3),
    ("b4", "1 Natural", 1.0, 1.0, 1.0, 1.0, 1.0),
]


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


def run_variant(suffix: str, style: str, intensity: float, color: float,
                detail: float, global_tone: float, local_tone: float) -> Path:
    wf = {
        "1": {"class_type": "LoadVideo", "inputs": {"file": "dlssnr_tone_src.mp4"}},
        "2": {"class_type": "MiniMaxH3DLSSNRRuntimeAuditT8Advanced", "inputs": {
            "runtime_version": "1.3", "probe_mode": "feature_probe_1_frame",
            "cuda_device_index": 0, "dxgi_adapter_index": 0}},
        "3": {"class_type": "MiniMaxH3DLSSNRVideoFileT8Advanced", "inputs": {
            "dlss_nr_runtime": ["2", 0], "source_video": ["1", 0],
            "mode": "sr_nr", "scale": "2.0", "motion_engine": "auto",
            "sr_preset": "default", "nr_preset": "0 Default",
            "nr_style": style, "nr_intensity": intensity,
            "nr_local_structure": 1.0, "nr_local_tone": local_tone,
            "nr_skin": 1.0, "nr_global_tone": global_tone,
            "nr_detail": detail, "nr_color": color,
            "nr_ui_correction": False, "nr_auto_mask": False,
            "quality_profile": "custom",
            "cuda_device_index": 0, "probe_mode": "feature_probe_1_frame",
            "dxgi_adapter_index": 0,
            "filename_prefix": f"dlssnr/tone_{suffix}", "crf": 16.0}},
    }
    _api("POST", "/prompt",
         body={"prompt": wf, "client_id": f"omnispace-tone-{suffix}"},
         timeout=30.0)
    expect = OUT_DIR / f"h3v_dlssnr_{suffix}_00001_.mp4"
    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(8)
        if expect.is_file() and expect.stat().st_size > 100000:
            time.sleep(2)
            dest = VID_DIR / f"h3v_cmp_2s_fr_dlssnr_{suffix}.mp4"
            import shutil
            shutil.move(str(expect), str(dest))
            return dest
    raise TimeoutError(f"variant {suffix} 600s 未出片")


def main() -> int:
    import logging
    import shutil
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from src.services.inference.comfy_proc import COMFY_INPUT_DIR
    from src.services.inference.h3_face_refine import _comfy_manager, _wait_comfy_ready

    cm = _comfy_manager()
    cm.spawn("comfyui_h3.log")
    if not _wait_comfy_ready(120):
        print("SETUP-FAIL")
        return 1
    COMFY_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SRC, COMFY_INPUT_DIR / "dlssnr_tone_src.mp4")

    t0 = time.time()
    for args in VARIANTS:
        suffix = args[0]
        print(f"[run] {suffix}: style={args[1]} intensity={args[2]} "
              f"color={args[3]} global={args[5]} local={args[6]}", flush=True)
        dest = run_variant(*args)
        print(f"[done] {suffix} -> {dest.name} "
              f"({(time.time()-t0)/60:.1f}min 累计)", flush=True)
    print(f"ALL DONE {(time.time()-t0)/60:.1f} min", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
