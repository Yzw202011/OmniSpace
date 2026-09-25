"""Z-Image-Base（40 步全量）人像美感 POC（2026-09-25，D 线第三候选）。

同提示词同 seed 20250923，对照 Z-Image-Turbo 8 步蒸馏版（r2 基线）。
工作流复用 z builder 契约：UNETLoader + CLIPLoader(type=lumina2,
Qwen3-4B TE) + ModelSamplingAuraFlow(shift=3) + 8 步改 40 步全量。
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8190"
PROMPT = ("美术风格：3D GC 游戏风格、高精度 3D 建模、PBR 物理渲染、清晰硬表面"
          "质感、游戏实时光影、干净明暗过渡、写实二次元游戏质感、画面锐利通透。"
          "时代背景：当代中国都市，盛夏午后。角色设定：22岁中国都市年轻女性，"
          "身高162cm。体态纤细偏瘦，肩线窄而柔和，骨相柔和偏鹅蛋脸，脸部线条"
          "圆润略带少量清秀稚气。五官清秀端正，平直细眉，圆杏眼，鼻梁小巧挺直"
          "，唇形偏薄。乌黑中长发扎成低马尾，发丝柔顺垂至肩胛骨下方，额前有轻"
          "薄碎发。气质关键词：干净、清秀、朴素、带初入社会的青涩感。上身穿白"
          "色棉质圆领短袖T恤，衣摆自然垂至胯部；下身穿浅蓝色高腰直筒九分牛仔"
          "裤，裤脚微微卷起。脚穿白色帆布鞋，平底，鞋面干净。空手。常态平静表"
          "情，眼睛平视镜头。")
NEG = "丑陋，畸形五官，皮肤粗糙，塑料感，模糊，低质量"


def gen(w: int, h: int, steps: int) -> None:
    wf = {
        "unet": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "z_image_base-nvfp8-mixed.safetensors",
            "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen_3_4b.safetensors",
            "type": "lumina2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {
            "vae_name": "z_image_ae.safetensors"}},
        "ms": {"class_type": "ModelSamplingAuraFlow", "inputs": {
            "model": ["unet", 0], "shift": 3}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["clip", 0], "text": PROMPT}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["clip", 0], "text": NEG}},
        "latent": {"class_type": "EmptySD3LatentImage", "inputs": {
            "width": w, "height": h, "batch_size": 1}},
        "sample": {"class_type": "KSampler", "inputs": {
            "model": ["ms", 0], "positive": ["pos", 0],
            "negative": ["neg", 0], "latent_image": ["latent", 0],
            "seed": 20250923, "steps": steps, "cfg": 4.0,
            "sampler_name": "res_multistep", "scheduler": "simple",
            "denoise": 1.0}},
        "decode": {"class_type": "VAEDecode", "inputs": {
            "samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {
            "images": ["decode", 0],
            "filename_prefix": f"zbase_{w}x{h}"}},
    }
    t0 = time.time()
    req = urllib.request.Request(
        BASE + "/prompt", data=json.dumps(
            {"prompt": wf, "client_id": "zbase"}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            pid = json.load(r)["prompt_id"]
    except urllib.error.HTTPError as e:
        print(f"[{w}x{h}] 400:", e.read().decode()[:300], flush=True)
        return
    print(f"[{w}x{h} steps={steps}] pid={pid}", flush=True)
    while True:
        time.sleep(5)
        with urllib.request.urlopen(BASE + "/history/" + pid, timeout=10) as r:
            hist = json.load(r)
        if pid not in hist:
            print(f"  {time.time()-t0:.0f}s running...", flush=True)
            continue
        st = hist[pid].get("status", {})
        if st.get("status_str") == "error":
            print("  EXEC ERROR:", json.dumps(
                st.get("messages", []))[-300:], flush=True)
            return
        for node_out in hist[pid].get("outputs", {}).values():
            for img in node_out.get("images", []):
                if img.get("type") == "output":
                    print(f"  DONE {time.time()-t0:.0f}s -> "
                          f"{img.get('subfolder','')}/{img['filename']}",
                          flush=True)
                    return
        break


if __name__ == "__main__":
    gen(1024, 1024, 40)
