"""夏沐沐角色 LoRA 验证脚本（LoRA 训练路线④，2026-09-10）。

klein-4b + xia_mumu_v1 LoRA → 生成测试图（触发词 "xia mumu"），
与 LoRA 训练效果验证。产物落 ComfyUI output，文件名 uno 前缀已占用，
此处用 lora_test 前缀。
"""

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


def _post(path: str, payload: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{COMFY}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{COMFY}{path}", timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    prompt = (
        "xia mumu, anime illustration of the chinese teenage girl xia mumu, "
        "standing in front of a villa gate holding a black notebook, "
        "3D CG anime realistic fusion style, afternoon sunlight, full body")

    wf = {
        "1": {  # klein-4b DiT（BFL 单文件）
            "class_type": "UNETLoader",
            "inputs": {"unet_name": "flux-2-klein-4b.safetensors",
                       "weight_dtype": "default"},
        },
        "2": {  # Qwen3-4B 文本编码器（klein type）
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": "qwen_3_4b.safetensors",
                       "type": "flux2", "device": "default"},
        },
        "3": {"class_type": "VAELoader",
              "inputs": {"vae_name": "flux2_klein_ae.safetensors"}},
        "8": {  # 角色 LoRA（训练产物）
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"model": ["1", 0],
                       "lora_name": "xia_mumu_v1.safetensors",
                       "strength_model": 1.0},
        },
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["2", 0], "text": prompt}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["2", 0], "text": ""}},
        "12": {"class_type": "EmptyLatentImage",
               "inputs": {"width": 896, "height": 896, "batch_size": 1}},
        "20": {"class_type": "ModelSamplingAuraFlow",
               "inputs": {"model": ["8", 0], "shift": 3.0}},
        "30": {"class_type": "KSampler",
               "inputs": {"model": ["20", 0], "positive": ["6", 0],
                          "negative": ["7", 0], "latent_image": ["12", 0],
                          "seed": 42, "steps": 20, "cfg": 4.0,
                          "sampler_name": "euler", "scheduler": "simple",
                          "denoise": 1.0}},
        "40": {"class_type": "VAEDecode",
               "inputs": {"samples": ["30", 0], "vae": ["3", 0]}},
        "50": {"class_type": "SaveImage",
               "inputs": {"filename_prefix": "lora_test", "images": ["40", 0]}},
    }

    report: dict[str, Any] = {"bench": "xia_mumu_lora_test",
                              "ts": time.strftime("%F %T")}
    print("[lora-test] 提交（klein-4b 装载 + 20 步）…", flush=True)
    t0 = time.time()
    with ResourceSampler(interval_s=1.0) as sampler:
        resp = _post("/prompt", {"prompt": wf, "client_id": "lora_test"})
        pid = resp.get("prompt_id")
        if not pid:
            print(f"[abort] {resp}")
            return 2
        deadline = t0 + 1200.0
        entry: dict[str, Any] = {}
        stall = 0
        while time.time() < deadline:
            time.sleep(5)
            try:
                entry = _get(f"/history/{pid}").get(pid, {})
                st = entry.get("status", {})
                if st.get("completed") or st.get("status_str") == "error":
                    break
                stall = 0
            except Exception:  # noqa: BLE001 - 推理阻塞期轮询超时属预期
                stall += 1
                if stall > 60:  # 5 分钟完全无响应则放弃等待
                    break
        wall_s = time.time() - t0
    status = entry.get("status", {}).get("status_str", "timeout")
    out = ""
    for node_out in (entry.get("outputs") or {}).values():
        for img in node_out.get("images", []):
            if img.get("type") == "output":
                out = img.get("filename", "")
    report["lora_test"] = {"status": status, "wall_s": round(wall_s, 1),
                           "output": out, "prompt_id": pid}
    report["resources"] = sampler.summary()
    print(f"[lora-test] {status} {wall_s:.0f}s → {out}", flush=True)
    path = save_report("xia_mumu_lora_test", report)
    print(f"[done] 报告 → {path}")
    return 0 if out else 1


if __name__ == "__main__":
    raise SystemExit(main())
# 本项目仅供学习使用，商业授权请+Q 3559331368
