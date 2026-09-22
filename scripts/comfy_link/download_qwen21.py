"""Qwen-Image-2.1 POC 下载器（hf-mirror 源，断点续传，仅 16G 卡量化三件套）。

产物落位遵循 comfy_model_map.json 真身约定：
  models/paint/qwen-image-2.1/
    ├── qwen_image_2.1_int8_convrot.safetensors   (diffusion_models 档)
    ├── text_encoder/qwen3vl_8b_w4a8.safetensors  (text_encoders 档)
    └── vae/qwen_image_2.1_vae_bf16.safetensors   (vae 档)
"""
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

BASE = "https://hf-mirror.com/Comfy-Org/Qwen-Image-2.1/resolve/main"
DEST = Path(r"E:\OmniSpace\models\paint\qwen-image-2.1")
# (远端路径, 本地相对路径, 期望字节大小 —— 2026-09-22 亲核自 HF API tree)
FILES = [
    ("vae/qwen_image_2.1_vae_bf16.safetensors", "vae/qwen_image_2.1_vae_bf16.safetensors", 680_000_000),
    ("text_encoders/qwen3vl_8b_w4a8.safetensors", "text_encoder/qwen3vl_8b_w4a8.safetensors", 6_310_000_000),
    ("diffusion_models/qwen_image_2.1_int8_convrot.safetensors", "qwen_image_2.1_int8_convrot.safetensors", 7_260_000_000),
]
CHUNK = 1 << 20


def fmt_gb(n: float) -> str:
    return f"{n / 1e9:.2f}GB"


def download(remote: str, local: Path, expected: int) -> bool:
    url = f"{BASE}/{remote}"
    tmp = local.with_suffix(local.suffix + ".part")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    pos = tmp.stat().st_size if tmp.exists() else 0
    if local.exists() and local.stat().st_size == expected:
        print(f"[skip] {local.name} 已完成", flush=True)
        return True
    req = Request(url, headers={"Range": f"bytes={pos}-"} if pos else {})
    with urlopen(req, timeout=60) as resp:
        total = pos + int(resp.headers.get("Content-Length", 0))
        if abs(total - expected) > 50_000_000:
            print(f"[warn] 总大小 {fmt_gb(total)} 与登记 {fmt_gb(expected)} 偏差大，仍继续", flush=True)
        mode = "ab" if pos else "wb"
        t0, done0 = time.time(), pos
        with open(tmp, mode) as f:
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                f.write(block)
                pos += len(block)
                if int(time.time() - t0) >= 5:
                    speed = (pos - done0) / max(time.time() - t0, 1) / 1e6
                    print(f"[{local.name}] {fmt_gb(pos)}/{fmt_gb(total)} {speed:.1f}MB/s", flush=True)
                    t0, done0 = time.time(), pos
    if pos < expected - 50_000_000:
        print(f"[FAIL] {local.name} 只到 {fmt_gb(pos)}/{fmt_gb(expected)}，可重跑续传", flush=True)
        return False
    tmp.rename(local)
    print(f"[OK] {local.name} {fmt_gb(pos)}", flush=True)
    return True


def main() -> int:
    ok = True
    for remote, rel, expected in FILES:
        for _ in range(5):
            if download(remote, DEST / rel, expected):
                break
            time.sleep(10)
        else:
            ok = False
    print("ALL DONE" if ok else "INCOMPLETE —— 重跑本脚本续传", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
