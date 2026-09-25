"""FLUX.1-dev fp8 人像美感 POC（2026-09-25，D 线引进验证）。

同提示词同 seed 20250923 出图，与 klein/qwen21 基线（r2_*.png）对比
人脸美感。工作流 = comfy 原生 flux1 形态：UNETLoader + DualCLIPLoader
(t5xxl fp8 + clip_l, type=flux) + FluxGuidance + KSampler（dev 版
20 步 cfg 3.5 社区基线起步）。
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8190"
PROMPT = ("Art style: 3D game CG render, high-precision 3D modeling, PBR physical shading, crisp hard-surface textures, real-time game lighting, clean light-shadow falloff, realistic anime-game aesthetic, sharp transparent image. Setting: contemporary Chinese metropolis, midsummer afternoon. Character: 22-year-old Chinese urban young woman, 162cm tall. Slim slender figure, narrow gentle shoulders, soft oval face with rounded lines and a touch of youthful innocence. Delicate pretty features: straight thin eyebrows, round almond eyes, small straight nose, thin lips. Jet-black medium-long hair tied in a low ponytail, silky strands falling below shoulder blades, wispy thin bangs. Aura: clean, fresh, unadorned, fresh-to-workforce innocence. Wearing white cotton crew-neck short-sleeve T-shirt with hem naturally reaching hips; light-blue high-waist straight-leg cropped jeans with slightly rolled cuffs. White flat canvas sneakers, clean. Empty hands. Calm neutral expression, eyes looking straight into the camera.")
PROMPT_CN = ("美术风格：3D GC 游戏风格、高精度 3D 建模、PBR 物理渲染、清晰硬表面"
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


def gen(w: int, h: int, steps: int, cfg_v: float) -> None:
    wf = {
        "unet": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "flux1-dev-fp8-e4m3fn.safetensors",
            "weight_dtype": "default"}},
        "clip": {"class_type": "DualCLIPLoader", "inputs": {
            "clip_name1": "t5xxl_fp8_e4m3fn.safetensors",
            "clip_name2": "clip_l.safetensors",
            "type": "flux", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {
            "vae_name": "flux-vae-bf16.safetensors"}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["clip", 0], "text": PROMPT}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["clip", 0], "text": NEG}},
        "guidance": {"class_type": "FluxGuidance", "inputs": {
            "conditioning": ["pos", 0], "guidance": 3.5}},
        "noise": {"class_type": "RandomNoise", "inputs": {
            "noise_seed": 20250923}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {
            "sampler_name": "euler"}},
        "sigmas": {"class_type": "BasicScheduler", "inputs": {
            "model": ["unet", 0], "scheduler": "simple",
            "steps": steps, "denoise": 1.0}},
        "guider": {"class_type": "CFGGuider", "inputs": {
            "model": ["unet", 0], "positive": ["guidance", 0],
            "negative": ["neg", 0], "cfg": cfg_v}},
        "latent": {"class_type": "EmptySD3LatentImage", "inputs": {
            "width": w, "height": h, "batch_size": 1}},
        "sample": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0],
            "sampler": ["sampler", 0], "sigmas": ["sigmas", 0],
            "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {
            "samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {
            "images": ["decode", 0],
            "filename_prefix": f"flux1dev_{w}x{h}"}},
    }
    t0 = time.time()
    req = urllib.request.Request(
        BASE + "/prompt", data=json.dumps(
            {"prompt": wf, "client_id": "flux1poc"}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        pid = json.load(r)["prompt_id"]
    print(f"[{w}x{h} steps={steps} cfg={cfg_v}] pid={pid}", flush=True)
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
    gen(832, 480, 20, 3.5)
    gen(1024, 1024, 28, 3.5)
