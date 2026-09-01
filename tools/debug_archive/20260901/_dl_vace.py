"""下载 Wan2.1-VACE-1.3B-Diffusers（官方 diffusers 布局）到 video_gen 目录。"""
import sys
import traceback

from modelscope import snapshot_download

TARGET = r"e:\OmniSpace\models\video_gen\wan21-vace-1.3b"
REPO = "Wan-AI/Wan2.1-VACE-1.3B-Diffusers"

try:
    print(f"[dl] {REPO} -> {TARGET} ...", flush=True)
    path = snapshot_download(REPO, local_dir=TARGET)
    print(f"[dl] DONE -> {path}", flush=True)
    sys.exit(0)
except Exception as exc:  # noqa: BLE001
    traceback.print_exc()
    print(f"[dl] FAILED: {exc}", flush=True)
    sys.exit(1)
