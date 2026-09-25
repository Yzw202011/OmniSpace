"""Z-Image-Base（非蒸馏全量版）下载器——nvfp8-mixed 6.29GB 社区量化版。

D 线第三候选实弹：阿里通义 6B 写实人像，Base 版 40 步全量采样（对比
Turbo 8 步蒸馏），同 TE qwen_3_4b / 同 VAE——我们的 z builder 现成。
"""
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

URL = ("https://hf-mirror.com/RamonGuthrie/z_image_base-nvfp8-mixed/resolve/main/"
       "z_image_base-nvfp8-mixed.safetensors")
DEST = Path(r"E:\OmniSpace\models\paint\z-image-base\z_image_base-nvfp8-mixed.safetensors")
EXPECTED = 6_290_000_000
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
