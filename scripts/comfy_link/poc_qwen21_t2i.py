"""Qwen-Image-2.1 t2i POC 实弹脚本（2026-09-23）。

对着本机 8190 隔离实例 POST /prompt（官方模板子图结构的 API 化），
轮询出图并回读 PNG 尺寸；配合外部 nvidia-smi 采样记录显存峰值。
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8190"
PROMPT = ("治愈系插画海报：一只戴圆框眼镜的橘猫坐在木书堆顶端看书，"
          "窗外是雨夜，暖黄台灯，蒸汽从茶杯升起；海报顶部大字标题"
          "「猫的书房」，右下角小字「OmniSpace 实测」")
NEG = "低清，畸形爪子，乱码文字"

WF = {
    "unet": {"class_type": "UNETLoader", "inputs": {
        "unet_name": "qwen_image_2.1_int8_convrot.safetensors",
        "weight_dtype": "default"}},
    "clip": {"class_type": "CLIPLoader", "inputs": {
        "clip_name": "qwen3vl_8b_w4a8.safetensors",
        "type": "qwen_image", "device": "default"}},
    "vae": {"class_type": "VAELoader", "inputs": {
        "vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
    "encode": {"class_type": "TextEncodeQwenImage21", "inputs": {
        "clip": ["clip", 0], "prompt": PROMPT,
        "negative_prompt": NEG, "resolution": 2048}},
    "ksample": {"class_type": "KSampler", "inputs": {
        "model": ["unet", 0], "seed": 42, "steps": 25, "cfg": 1.0,
        "sampler_name": "euler", "scheduler": "simple",
        "positive": ["encode", 0], "negative": ["encode", 1],
        "latent_image": ["encode", 2], "denoise": 1.0}},
    "decode": {"class_type": "VAEDecode", "inputs": {
        "samples": ["ksample", 0], "vae": ["vae", 0]}},
    "save": {"class_type": "SaveImage", "inputs": {
        "images": ["decode", 0], "filename_prefix": "qwen21_poc"}},
}


def main() -> None:
    t0 = time.time()
    req = urllib.request.Request(
        BASE + "/prompt", data=json.dumps(
            {"prompt": WF, "client_id": "poc"}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        pid = json.load(r)["prompt_id"]
    print("prompt_id:", pid, flush=True)
    while True:
        time.sleep(5)
        with urllib.request.urlopen(BASE + "/history/" + pid, timeout=10) as r:
            hist = json.load(r)
        if pid not in hist:
            print(f"[{time.time()-t0:6.0f}s] queued/running...", flush=True)
            continue
        outputs = hist[pid].get("outputs", {})
        status = hist[pid].get("status", {})
        if status.get("status_str") == "error":
            print("EXEC ERROR:", json.dumps(
                status.get("messages", []))[-600:], flush=True)
            return
        for node_out in outputs.values():
            for img in node_out.get("images", []):
                if img.get("type") == "output":
                    print(f"DONE in {time.time()-t0:.0f}s -> "
                          f"{img['filename']} sub={img.get('subfolder')}",
                          flush=True)
                    return
        print(f"[{time.time()-t0:6.0f}s] finished, no image?", flush=True)
        return


if __name__ == "__main__":
    main()
