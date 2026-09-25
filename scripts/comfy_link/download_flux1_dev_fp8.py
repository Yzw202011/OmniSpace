"""FLUX.1-dev fp8 下载器（Kijai 11.9GB 单文件 + 配套 VAE，hf-mirror 断点续传）。

D 线引进 POC：人像写实美感公认第一的底模，走 comfy 原生 UNETLoader
+ CLIPLoader(t5xxl+clip_l) 三件套形态。
"""
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

BASE = "https://hf-mirror.com/Kijai/flux-fp8/resolve/main"
DEST = Path(r"E:\OmniSpace\models\paint\flux1-dev-fp8")
# (远端, 本地名, 期望GB)
FILES = [
    ("flux1-dev-fp8-e4m3fn.safetensors", "flux1-dev-fp8-e4m3fn.safetensors", 11.9),
    ("flux-vae-bf16.safetensors", "flux-vae-bf16.safetensors", 0.17),
]
# TE 三件套（t5xxl fp8 + clip_l）独立仓库
EXTRA = [
    ("https://hf-mirror.com/mcmonkey/google_t5-v1_1-xxl_encoderonly/resolve/main/t5xxl_fp8_e4m3fn.safetensors",
     "t5xxl_fp8_e4m3fn.safetensors", 4.89),
    ("https://hf-mirror.com/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors",
     "clip_l.safetensors", 0.25),
]
CHUNK = 1 << 20


def fetch_url(url: str, local: Path, expect_gb: float) -> bool:
    local.parent.mkdir(parents=True, exist_ok=True)
    tmp = local.with_suffix(local.suffix + ".part")
    pos = tmp.stat().st_size if tmp.exists() else 0
    expect = int(expect_gb * 1e9)
    if local.exists() and local.stat().st_size >= expect - 50_000_000:
        print(f"[skip] {local.name} 完成", flush=True)
        return True
    req = Request(url, headers={"Range": f"bytes={pos}-"} if pos else {})
    with urlopen(req, timeout=60) as resp:
        total = pos + int(resp.headers.get("Content-Length", 0))
        print(f"[{local.name}] {total/1e9:.2f}GB, resume {pos/1e9:.2f}", flush=True)
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
    if pos < expect - 200_000_000:
        print(f"[FAIL] {local.name} {pos/1e9:.2f}GB，重跑续传", flush=True)
        return False
    tmp.rename(local)
    print(f"[OK] {local.name}", flush=True)
    return True


def fetch(remote: str, local_name: str, expect_gb: float) -> bool:
    return fetch_url(f"{BASE}/{remote}", DEST / local_name, expect_gb)


def main() -> int:
    ok = True
    for remote, name, gb in FILES:
        for _ in range(8):
            if fetch(remote, name, gb):
                break
            time.sleep(5)
        else:
            ok = False
    for url, name, gb in EXTRA:
        for _ in range(8):
            if fetch_url(url, DEST / name, gb):
                break
            time.sleep(5)
        else:
            ok = False
    print("ALL DONE" if ok else "INCOMPLETE", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
