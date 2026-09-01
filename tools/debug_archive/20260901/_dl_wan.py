"""下载 Wan2.1-I2V-1.3B-Diffusers 到 models/video_gen/wan21-i2v-1.3b。

ModelScope 优先（国内源），官方 org 失败回落镜像 org。
"""
import sys
import traceback

from modelscope import snapshot_download

TARGET = r"e:\OmniSpace\models\video_gen\wan21-i2v-1.3b"
CANDIDATES = (
    "Wan-AI/Wan2.1-I2V-1.3B-Diffusers",
    "AI-ModelScope/Wan2.1-I2V-1.3B-Diffusers",
)

last_exc: Exception | None = None
for repo in CANDIDATES:
    try:
        print(f"[dl] trying {repo} ...", flush=True)
        path = snapshot_download(repo, local_dir=TARGET)
        print(f"[dl] DONE {repo} -> {path}", flush=True)
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        last_exc = exc
        print(f"[dl] {repo} failed: {exc}", flush=True)
        traceback.print_exc()

print(f"[dl] ALL FAILED: {last_exc}", flush=True)
sys.exit(1)
