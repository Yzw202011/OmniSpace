"""HiDream-O1-Image Dev fp8_scaled 下载器（hf-mirror，断点续传）。

D 线第二候选：8B UiT 像素统一架构，MIT 可商用，Artificial Analysis
开源榜曾第一；checkpoint 单文件内嵌 TE/VAE sentinel（无分件）。
"""
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

URL = ("https://hf-mirror.com/Comfy-Org/HiDream-O1-Image/resolve/main/"
       "checkpoints/hidream_o1_image_dev_fp8_scaled.safetensors")
DEST = Path(r"E:\OmniSpace\models\paint\hidream-o1\hidream_o1_image_dev_fp8_scaled.safetensors")
EXPECTED = 8_070_000_000
CHUNK = 1 << 20


def main() -> int:
    DEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEST.with_suffix(DEST.suffix + ".part")
    pos = tmp.stat().st_size if tmp.exists() else 0
    req = Request(URL, headers={"Range": f"bytes={pos}-"} if pos else {})
    with urlopen(req, timeout=60) as resp:
        total = pos + int(resp.headers.get("Content-Length", 0))
        print(f"total {total/1e9:.2f}GB resume {pos/1e9:.2f}", flush=True)
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
                    print(f"  {pos/1e9:.2f}/{total/1e9:.2f}GB {speed:.1f}MB/s", flush=True)
                    t0, done0 = time.time(), pos
    if pos < EXPECTED - 200_000_000:
        print(f"[FAIL] {pos/1e9:.2f}GB 重跑续传", flush=True)
        return 1
    tmp.rename(DEST)
    print(f"[OK] {DEST}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
