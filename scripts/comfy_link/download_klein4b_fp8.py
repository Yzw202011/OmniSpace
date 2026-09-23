"""Klein-4B 官方 fp8 单文件下载器（hf-mirror 源，断点续传）。

立项方案 E：官方 fp8 走 comfy 已验证的 fp8 路径，绕开 bf16 数值缺陷。
落位：models/paint/flux2-klein-4b-fp8/（真身）→ 硬链 ComfyUI diffusion_models。
"""
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

URL = "https://hf-mirror.com/black-forest-labs/FLUX.2-klein-4b-fp8/resolve/main/flux-2-klein-4b-fp8.safetensors"
DEST = Path(r"E:\OmniSpace\models\paint\flux2-klein-4b-fp8\flux-2-klein-4b-fp8.safetensors")
EXPECTED = 4_070_000_000
CHUNK = 1 << 20


def main() -> int:
    DEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEST.with_suffix(DEST.suffix + ".part")
    pos = tmp.stat().st_size if tmp.exists() else 0
    req = Request(URL, headers={"Range": f"bytes={pos}-"} if pos else {})
    with urlopen(req, timeout=60) as resp:
        total = pos + int(resp.headers.get("Content-Length", 0))
        print(f"total {total/1e9:.2f}GB, resume from {pos/1e9:.2f}GB", flush=True)
        t0, done0 = time.time(), pos
        with open(tmp, "ab" if pos else "wb") as f:
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                f.write(block)
                pos += len(block)
                if int(time.time() - t0) >= 10:
                    speed = (pos - done0) / max(time.time() - t0, 1) / 1e6
                    print(f"{pos/1e9:.2f}/{total/1e9:.2f}GB {speed:.1f}MB/s", flush=True)
                    t0, done0 = time.time(), pos
    if pos < EXPECTED - 50_000_000:
        print(f"FAIL only {pos/1e9:.2f}GB, rerun to resume", flush=True)
        return 1
    tmp.rename(DEST)
    print(f"OK {DEST}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
