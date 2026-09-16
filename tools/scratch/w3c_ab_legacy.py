"""W3-C A/B legacy 侧（2026-09-13）：diffusers klein-4b，同 prompt 同 seed 512²。

与 comfy 侧（w3c_ab_comfy.py）同题对比；产物 w3c_ab_legacy.png。
注意：需先跑 comfy 侧并确认其进程退出（Job Object 已保证），避免显存叠载。
"""
import sys
import time

sys.path.insert(0, r"E:\OmniSpace")

from src.services.inference.paint_engine import get_paint_engine  # noqa: E402

PROMPT = ("A. 全局风格：3D卡通、皮克斯质感、黏土材质渲染、柔和电影光、"
          "干净明亮，细节刻画精致，光影层次丰富")
SEED = 20260913

engine = get_paint_engine()
t0 = time.perf_counter()
ok = engine.ensure_loaded("flux2-klein-4b")
t_load = time.perf_counter() - t0
print(f"[legacy 加载] ok={ok} load={t_load:.1f}s")
if not ok:
    print("LEGACY-LOAD-FAILED:", engine.get_status())
    sys.exit(1)

t0 = time.perf_counter()
result = engine.generate({
    "prompt": PROMPT, "negative": "", "steps": 20, "cfg": 7.5,
    "width": 512, "height": 512, "seed": SEED, "batch_size": 1})
t_gen = time.perf_counter() - t0
result["images"][0].save(
    r"E:\OmniSpace\data\comfyui\output\w3c_ab_legacy.png", "PNG")
print(f"[legacy 20步] gen={t_gen:.1f}s model={result.get('model')} "
      f"seed={result.get('seed')}")
print("LEGACY-SIDE-DONE")
