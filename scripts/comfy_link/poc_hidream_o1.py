"""HiDream-O1-Image Dev fp8 人像美感 POC（2026-09-25，D 线第二候选实弹）。

8B UiT 像素统一架构 checkpoint 单文件直载（内嵌 TE/VAE sentinel）；
同提示词同 seed 20250923 与 qwen21/klein 基线对比人脸美感。
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


def gen(w: int, h: int, steps: int, cfg_v: float) -> None:
    wf = {
        "ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {
            "ckpt_name": "hidream_o1_image_dev_fp8_scaled.safetensors"}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["ckpt", 1], "text": PROMPT}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["ckpt", 1], "text": NEG}},
        "empty": {"class_type": "EmptyHiDreamO1LatentImage", "inputs": {
            "width": w, "height": h, "batch_size": 1}},
        "noise": {"class_type": "RandomNoise", "inputs": {
            "noise_seed": 20250923}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {
            "sampler_name": "euler"}},
        "sigmas": {"class_type": "BasicScheduler", "inputs": {
            "model": ["ckpt", 0], "scheduler": "simple",
            "steps": steps, "denoise": 1.0}},
        "guider": {"class_type": "CFGGuider", "inputs": {
            "model": ["ckpt", 0], "positive": ["pos", 0],
            "negative": ["neg", 0], "cfg": cfg_v}},
        "sample": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0],
            "sampler": ["sampler", 0], "sigmas": ["sigmas", 0],
            "latent_image": ["empty", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {
            "samples": ["sample", 0], "vae": ["ckpt", 2]}},
        "save": {"class_type": "SaveImage", "inputs": {
            "images": ["decode", 0],
            "filename_prefix": f"hidream_o1_{w}x{h}"}},
    }
    # UiT 像素空间：无 VAE decode（sample 输出即像素）——latent 名对应
    # 像素转换，SaveImage 直接收 sample 输出
    t0 = time.time()
    req = urllib.request.Request(
        BASE + "/prompt", data=json.dumps(
            {"prompt": wf, "client_id": "hdpo"}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            pid = json.load(r)["prompt_id"]
    except urllib.error.HTTPError as e:
        print(f"[{w}x{h}] 400:", e.read().decode()[:300], flush=True)
        return
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
    # 先探 latent 节点名
    oi = json.load(urllib.request.urlopen(BASE + "/object_info", timeout=15))
    lat = [k for k in oi if "LatentImage" in k and ("HiDream" in k or "Empty" in k)]
    print("latent nodes:", lat[:8], flush=True)
    gen(832, 480, 28, 4.0)
    gen(1024, 1024, 28, 4.0)
