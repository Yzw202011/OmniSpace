"""UNO 主体一致生成冒烟（B2-2 D 路线备选，2026-09-10）。

验证 ByteDance UNO（FLUX.1-dev + dit_lora）在动漫 3D CG 风格下的
身份保持能力：夏沐沐 portrait + 3D CG 全身 + 场景图 → 生成新构图。

直提 ComfyUI /prompt（UNOModelLoader + UNOGenerate 双节点）。
判定：产物真实落盘 + 目检脸/服装/风格三轴。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bench_common import ResourceSampler, save_report  # noqa: E402

COMFY = "http://127.0.0.1:8189"


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{COMFY}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _get(path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{COMFY}{path}", timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    from PIL import Image

    def prep(src: str, name: str, max_dim: int = 512) -> str:
        img = Image.open(src).convert("RGB")
        img.thumbnail((max_dim, max_dim))
        out = Path("E:/OmniSpace/data/comfyui/input") / name
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out, format="PNG")
        return name

    base = "E:/OmniSpace/data/comic_assets/align200465dd41"
    char = prep(f"{base}/characters/夏沐沐/portrait.png", "uno_char.png")
    full = prep(f"{base}/characters/夏沐沐/fullbody.png", "uno_full.png")
    scene = prep(f"{base}/scenes/别墅区街角大门/image.png", "uno_scene.png")

    prompt = (
        "The girl from Picture 1 standing in front of the gate from "
        "Picture 2, holding the black notebook, 3D CG anime realistic "
        "fusion style, high quality game render, afternoon sunlight")

    wf = {
        "1": {  # UNO 模型装载（fp8 unet + vae + t5 + clip + lora）
            "class_type": "UNOModelLoader",
            "inputs": {
                "flux_model": "flux1-dev-fp8-e4m3fn.safetensors",
                "ae_model": "ae.safetensors",
                "t5_model": "t5xxl_fp8_e4m3fn.safetensors",
                "clip_model": "clip_l.safetensors",
                "use_fp8": True,
                "offload": True,
                "lora_model": "uno_dit_lora.safetensors",
            },
        },
        "10": {"class_type": "LoadImage", "inputs": {"image": char}},
        "11": {"class_type": "LoadImage", "inputs": {"image": full}},
        "12": {"class_type": "LoadImage", "inputs": {"image": scene}},
        "20": {  # UNO 生成（25 步默认）
            "class_type": "UNOGenerate",
            "inputs": {
                "uno_model": ["1", 0],
                "prompt": prompt,
                "width": 896, "height": 896,
                "guidance": 4.0, "num_steps": 25, "seed": 42,
                "pe": "d",
                "reference_image_1": ["10", 0],
                "reference_image_2": ["11", 0],
                "reference_image_3": ["12", 0],
            },
        },
        "30": {  # SaveImage（UNOGenerate 直接回 IMAGE tensor）
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "uno_smoke", "images": ["20", 0]},
        },
    }

    report: dict[str, Any] = {"bench": "uno_smoke",
                              "ts": time.strftime("%F %T")}
    print("[uno] 提交（首跑含模型装载 ~数分钟）…", flush=True)
    t0 = time.time()
    with ResourceSampler(interval_s=1.0) as sampler:
        resp = _post("/prompt", {"prompt": wf, "client_id": "uno_smoke"})
        pid = resp.get("prompt_id")
        if not pid:
            print(f"[abort] 提交失败: {resp}")
            return 2
        deadline = t0 + 1200.0
        entry: dict[str, Any] = {}
        while time.time() < deadline:
            time.sleep(3)
            entry = _get(f"/history/{pid}").get(pid, {})
            st = entry.get("status", {})
            if st.get("completed") or st.get("status_str") == "error":
                break
        wall_s = time.time() - t0
    status = entry.get("status", {}).get("status_str", "timeout")
    out = ""
    for node_out in (entry.get("outputs") or {}).values():
        for img in node_out.get("images", []):
            if img.get("type") == "output":
                out = img.get("filename", "")
    report["uno"] = {"status": status, "wall_s": round(wall_s, 1),
                     "output": out, "prompt_id": pid}
    report["resources"] = sampler.summary()
    print(f"[uno] {status} {wall_s:.0f}s → {out}", flush=True)
    path = save_report("uno_smoke", report)
    print(f"[done] 报告 → {path}")
    return 0 if out else 1


if __name__ == "__main__":
    raise SystemExit(main())
