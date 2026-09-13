"""批1a 绘画档位 A/B 实弹（2026-09-12）：36步/cfg4 vs 4步/cfg1 vs 4步+consistency LoRA。

镜像 backend/services/inference/comfy_paint_engine.py 的 _build_workflow
节点图（无参考图/无 PuLID），直接提交 8189，同 seed 同 prompt 三组计时。
产物落 data/comfyui/output/，供用户目验。只读探测 + 生成提交，不动任何代码。
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8189"
SEED = 20260912
PROMPT = ("一位年轻女性站在秋天的银杏树下，阳光透过金黄的树叶洒在她肩上，"
          "她穿着米色风衣，围巾随风轻扬，电影感构图，浅景深，胶片质感，"
          "面部细节清晰，8k 高清")
NEG = "模糊，畸形手，多余手指，低质量，水印"
SIZE = (1280, 720)


def _nodes(steps: int, cfg: float, lora: str | None):
    wf = {
        "unet": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "flux-2-klein-9b-fp8.safetensors",
            "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen3_8b.safetensors",
            "type": "flux2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {
            "vae_name": "flux2-vae.safetensors"}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": SEED}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {
            "sampler_name": "euler"}},
        "sigmas": {"class_type": "Flux2Scheduler", "inputs": {
            "steps": steps, "width": SIZE[0], "height": SIZE[1]}},
        "latent": {"class_type": "EmptyFlux2LatentImage", "inputs": {
            "width": SIZE[0], "height": SIZE[1], "batch_size": 1}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["clip", 0], "text": PROMPT}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["clip", 0], "text": NEG}},
        "guider": {"class_type": "CFGGuider", "inputs": {
            "model": ["unet", 0], "positive": ["pos", 0],
            "negative": ["neg", 0], "cfg": cfg}},
        "sample": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0],
            "sampler": ["sampler", 0], "sigmas": ["sigmas", 0],
            "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {
            "samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {
            "images": ["decode", 0], "filename_prefix": "ab_preset"}},
    }
    if lora:
        wf["lora"] = {"class_type": "LoraLoader", "inputs": {
            "model": ["unet", 0], "clip": ["clip", 0],
            "lora_name": lora, "strength_model": 1.0, "strength_clip": 0.0}}
        wf["guider"]["inputs"]["model"] = ["lora", 0]
        wf["pos"]["inputs"]["clip"] = ["lora", 1]
        wf["neg"]["inputs"]["clip"] = ["lora", 1]
    return {"prompt": wf, "client_id": "paint_preset_ab"}


def run(tag: str, steps: int, cfg: float, lora: str | None) -> float:
    body = json.dumps(_nodes(steps, cfg, lora)).encode()
    req = urllib.request.Request(f"{BASE}/prompt", data=body, method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as resp:
        pid = json.loads(resp.read())["prompt_id"]
    while True:
        time.sleep(2.0)
        try:
            with urllib.request.urlopen(f"{BASE}/history/{pid}",
                                        timeout=10) as resp:
                hist = json.loads(resp.read())
        except urllib.error.HTTPError:
            continue
        entry = hist.get(pid)
        if not entry:
            continue
        st = (entry.get("status") or {}).get("status_str")
        if st == "error":
            _msgs = json.dumps(
                (entry.get("status") or {}).get("messages", []),
                ensure_ascii=False)[:400]
            print(f"[{tag}] ERROR: {_msgs}")
            return -1.0
        if st == "success":
            dt = time.perf_counter() - t0
            outs = []
            for node_out in (entry.get("outputs") or {}).values():
                for item in node_out.get("images") or []:
                    outs.append(item["filename"])
            print(f"[{tag}] steps={steps} cfg={cfg} lora={lora or '-'} "
                  f"wall={dt:.1f}s files={outs}")
            return dt


if __name__ == "__main__":
    run("A-基线", 36, 4.0, None)
    run("B-fast裸", 4, 1.0, None)
    run("C-fast+consistency", 4, 1.0,
        "flux2-klein-9b-consistency-v2.safetensors")
    print("DONE")
