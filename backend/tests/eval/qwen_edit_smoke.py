"""Qwen-Edit-2511 GGUF 冒烟脚本（B2-1，2026-09-09）。

直提 ComfyUI /prompt（绕过产品链），验证：
  ①单图编辑：输入图 + 指令 → 产物落盘
  ②三图 EditPlus：角色+场景+道具 → 合成产物（三一致性技术可行性）

流程：起 ComfyUI（comfy_proc 正规托管）→ 提交工作流 → 轮询 /history →
取产物路径 + 计时 + 显存峰值（ResourceSampler）→ 报告落 logs/eval/。

前置纪律：GPU 活动门；ComfyUI 冷启动 ~40s + 模型装载数分钟。
用法：runtime/py310/python.exe backend/tests/eval/qwen_edit_smoke.py
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
CLIENT_ID = "qwen-edit-smoke"


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{COMFY}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _get(path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{COMFY}{path}", timeout=30) as r:
        return json.loads(r.read())


def _b64_png(path: str, max_dim: int = 512) -> str:
    """输入图缩到 max_dim 转 base64（控制 prompt 体积）。"""
    import base64
    import io

    from PIL import Image
    img = Image.open(path).convert("RGB")
    img.thumbnail((max_dim, max_dim))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _wait_prompt(prompt_id: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            hist = _get(f"/history/{prompt_id}")
            entry = hist.get(prompt_id)
            if entry and entry.get("status", {}).get("completed"):
                return entry
            if entry and entry.get("status", {}).get("status_str") == "error":
                return entry
        except Exception:  # noqa: BLE001 - 轮询
            pass
        time.sleep(3.0)
    return {}


def _prep_input_image(src: str, name: str, max_dim: int = 512) -> str:
    """缩图落 ComfyUI input 目录，返回文件名（LoadImage 只认文件名）。"""
    from PIL import Image
    img = Image.open(src).convert("RGB")
    img.thumbnail((max_dim, max_dim))
    out = Path("E:/OmniSpace/data/comfyui/input") / name
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="PNG")
    return name


def _build_single_edit_workflow(image_name: str,
                                instruction: str) -> dict[str, Any]:
    """单图编辑：Qwen-Edit GGUF + text encoder GGUF + mmproj。"""
    return {
        "3": {  # UnetLoaderGGUF
            "class_type": "UnetLoaderGGUF",
            "inputs": {"unet_name": "qwen-image-edit-2511-Q4_K_M.gguf"},
        },
        "4": {  # CLIPLoaderGGUF（qwen2vl 架构带 mmproj）
            "class_type": "CLIPLoaderGGUF",
            "inputs": {
                "clip_name": "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf",
                "type": "qwen_image",
                "device": "default",
            },
        },
        "10": {  # LoadImage（input 目录文件名）
            "class_type": "LoadImage",
            "inputs": {"image": image_name},
        },
        "11": {  # ImageScale（限制总像素防 OOM）
            "class_type": "ImageScaleToTotalPixels",
            "inputs": {"image": ["10", 0], "upscale_method": "lanczos",
                       "megapixels": 0.75, "resolution_steps": 1},
        },
        "6": {  # TextEncode（编辑指令——节点签名：prompt/vae/image1~3）
            "class_type": "TextEncodeQwenImageEditPlus",
            "inputs": {
                "clip": ["4", 0],
                "prompt": instruction,
                "vae": ["13", 0],
                "image1": ["11", 0],
            },
        },
        "12": {  # EmptyLatentImage（编辑模型需要）
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": 896, "height": 896, "batch_size": 1},
        },
        "13": {  # VAELoader（qwen 自带 vae 目录名）
            "class_type": "VAELoader",
            "inputs": {"vae_name": "qwen_image_vae.safetensors"},
        },
        "20": {  # ModelSamplingAuraFlow（qwen 系 shift）
            "class_type": "ModelSamplingAuraFlow",
            "inputs": {"model": ["3", 0], "shift": 3.0},
        },
        "8": {  # Lightning LoRA（4步蒸馏）
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": ["20", 0],
                "lora_name":
                    "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors",
                "strength_model": 1.0,
            },
        },
        "7": {  # 负面条件（空——EditPlus 单输出，负面用 ZeroOut）
            "class_type": "ConditioningZeroOut",
            "inputs": {"conditioning": ["6", 0]},
        },
        "30": {  # KSampler（Lightning 4 步 + CFG 1.0）
            "class_type": "KSampler",
            "inputs": {
                "model": ["8", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["12", 0],
                "seed": 42, "steps": 4, "cfg": 1.0,
                "sampler_name": "euler", "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "40": {  # VAEDecode + SaveImage
            "class_type": "VAEDecode",
            "inputs": {"samples": ["30", 0], "vae": ["13", 0]},
        },
        "50": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "qedit_smoke", "images": ["40", 0]},
        },
    }


def _submit(workflow: dict[str, Any]) -> str:
    resp = _post("/prompt", {
        "prompt": workflow, "client_id": CLIENT_ID})
    pid = resp.get("prompt_id")
    if not pid:
        raise RuntimeError(f"提交失败: {resp}")
    return pid


def _extract_output(entry: dict[str, Any]) -> str:
    for node_out in (entry.get("outputs") or {}).values():
        for img in node_out.get("images", []):
            if img.get("type") == "output":
                return img.get("filename", "")
    return ""


def _build_triple_edit_workflow(char_name: str, scene_name: str,
                                prop_name: str,
                                instruction: str) -> dict[str, Any]:
    """三图合成：EditPlus 三图输入（角色+场景+道具）。"""
    wf = _build_single_edit_workflow(char_name, instruction)
    # 追加场景/道具图到 image2/image3 + 额外两个 LoadImage+Scale 节点
    wf["14"] = {  # 场景图
        "class_type": "LoadImage", "inputs": {"image": scene_name},
    }
    wf["15"] = {
        "class_type": "ImageScaleToTotalPixels",
        "inputs": {"image": ["14", 0], "upscale_method": "lanczos",
                   "megapixels": 0.75, "resolution_steps": 1},
    }
    wf["16"] = {  # 道具图
        "class_type": "LoadImage", "inputs": {"image": prop_name},
    }
    wf["17"] = {
        "class_type": "ImageScaleToTotalPixels",
        "inputs": {"image": ["16", 0], "upscale_method": "lanczos",
                   "megapixels": 0.5, "resolution_steps": 1},
    }
    # 把三图挂到 EditPlus 节点
    wf["6"]["inputs"]["image2"] = ["15", 0]
    wf["6"]["inputs"]["image3"] = ["17", 0]
    return wf


def main() -> int:
    import sys as _sys
    mode = _sys.argv[1] if len(_sys.argv) > 1 else "single"
    base = "E:/OmniSpace/data/comic_assets/align200465dd41"

    if mode == "triple":
        print("[smoke] ② 三图合成（角色+场景+道具）", flush=True)
        char = _prep_input_image(
            f"{base}/characters/夏沐沐/portrait.png", "t_char.png")
        scene = _prep_input_image(
            f"{base}/scenes/别墅区街角大门/image.png", "t_scene.png")
        prop = _prep_input_image(
            f"{base}/props/黑色笔记本/image.png", "t_prop.png")
        wf = _build_triple_edit_workflow(
            char, scene, prop,
            "The girl from Picture 1 is standing in front of the gate from "
            "Picture 2, holding the notebook from Picture 3. She is looking "
            "at the notebook with curiosity. Keep her face, hair and outfit "
            "exactly the same as Picture 1. Afternoon sunlight, cinematic "
            "composition.")
        report: dict[str, Any] = {"bench": "qwen_edit_smoke_triple",
                                  "ts": time.strftime("%F %T"),
                                  "quant": "Q4_K_M", "mode": "triple"}
        t0 = time.time()
        with ResourceSampler(interval_s=1.0) as sampler:
            pid = _submit(wf)
            entry = _wait_prompt(pid, 1200.0)
            wall_s = time.time() - t0
        out = _extract_output(entry)
        status = entry.get("status", {}).get("status_str", "timeout")
        report["triple_edit"] = {
            "status": status, "wall_s": round(wall_s, 1),
            "output": out, "prompt_id": pid,
        }
        report["resources"] = sampler.summary()
        print(f"[smoke] 三图: {status} {wall_s:.0f}s → {out}", flush=True)
        path = save_report("qwen_edit_smoke_triple", report)
        print(f"[done] 报告 → {path}")
        return 0 if out else 1

    # 默认单图
    print("[smoke] ① 单图编辑", flush=True)
    # 用现成资产图做输入（夏沐沐 portrait）
    asset = ("E:/OmniSpace/data/comic_assets/align200465dd41/"
             "characters/夏沐沐/portrait.png")
    img_name = _prep_input_image(asset, "qedit_smoke_input.png")
    wf = _build_single_edit_workflow(
        img_name, "Keep the character's face and pose exactly the same, "
                  "change the hair color to sky blue, studio lighting")
    report_single: dict[str, Any] = {"bench": "qwen_edit_smoke",
                              "ts": time.strftime("%F %T"),
                              "quant": "Q4_K_M"}
    t0 = time.time()
    with ResourceSampler(interval_s=1.0) as sampler:
        pid = _submit(wf)
        entry = _wait_prompt(pid, 900.0)
        wall_s = time.time() - t0
    out = _extract_output(entry)
    status = entry.get("status", {}).get("status_str", "timeout")
    report_single["single_edit"] = {
        "status": status, "wall_s": round(wall_s, 1),
        "output": out, "prompt_id": pid,
    }
    report_single["resources"] = sampler.summary()
    print(f"[smoke] 单图: {status} {wall_s:.0f}s → {out}", flush=True)
    path = save_report("qwen_edit_smoke", report_single)
    print(f"[done] 报告 → {path}")
    return 0 if out else 1


if __name__ == "__main__":
    raise SystemExit(main())
