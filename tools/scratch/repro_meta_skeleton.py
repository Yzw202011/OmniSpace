"""meta 空壳 live 复现（2026-09-16）。

复刻 keyframe 救援路径的 4b 装载：CUDA 初始化 → from_pretrained(bf16,
safetensors) →（low_vram 路）enable_sequential_cpu_offload → 逐组件
查 meta。带计时与异常捕获，定位「1 秒返回 meta 骨架」的真因。

用法：runtime\\py312\\python.exe tools\\scratch\\repro_meta_skeleton.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import torch  # noqa: E402

MODEL = Path("models/paint/flux2-klein-4b")

print(f"[0] torch={torch.__version__} cuda={torch.cuda.is_available()}")
if torch.cuda.is_available():
    free_b, total_b = torch.cuda.mem_get_info(0)
    print(f"[0] GPU free={free_b / 2**30:.1f}GB / {total_b / 2**30:.1f}GB")

import diffusers  # noqa: E402

print(f"[0] diffusers={diffusers.__version__}")

flux_cls = getattr(diffusers, "Flux2KleinPipeline", None)
print(f"[0] Flux2KleinPipeline={'OK' if flux_cls else 'MISSING'}")

t0 = time.time()
try:
    pipe = flux_cls.from_pretrained(
        str(MODEL), torch_dtype=torch.bfloat16, use_safetensors=True)
except Exception as exc:  # noqa: BLE001
    print(f"[1] from_pretrained RAISED after {time.time() - t0:.1f}s: "
          f"{type(exc).__name__}: {exc}")
    raise SystemExit(1)
t1 = time.time()
print(f"[1] from_pretrained returned in {t1 - t0:.1f}s")


def meta_report(tag: str) -> bool:
    any_meta = False
    for comp_name in ("transformer", "text_encoder", "vae"):
        comp = getattr(pipe, comp_name, None)
        if comp is None:
            print(f"[{tag}] {comp_name}: <absent>")
            continue
        try:
            p = next(comp.parameters(), None)
        except StopIteration:
            p = None
        is_meta = p is not None and p.is_meta
        any_meta = any_meta or is_meta
        dev = "meta" if is_meta else (str(p.device) if p is not None else "no-param")
        n = sum(1 for _ in comp.parameters())
        print(f"[{tag}] {comp_name}: dev={dev} params={n}")
    return any_meta


if meta_report("2-after-load"):
    print("[2] *** META REPRODUCED at from_pretrained stage ***")
else:
    print("[2] weights materialized (non-meta) at load stage")

# low_vram 路：sequential offload（keyframe 救援实况）
t2 = time.time()
try:
    pipe.enable_sequential_cpu_offload()
    print(f"[3] sequential_cpu_offload OK in {time.time() - t2:.1f}s")
except Exception as exc:  # noqa: BLE001
    print(f"[3] sequential_cpu_offload RAISED in {time.time() - t2:.1f}s: "
          f"{type(exc).__name__}: {exc}")
meta_report("4-after-offload")
print("[done]")
