"""Klein-4B 官方 fp8 网格噪声验证（立项方案 E 实弹，2026-09-23）。

同提示词同 seed 分别在 832×480 与 1024² 出图，对照 bf16 版的网格噪声。
工作流结构对照 comfy_paint_engine._build_workflow（klein 族基线）。
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8190"
PROMPT = ("美术风格：3D GC 游戏风格、高精度 3D 建模、PBR 物理渲染、清晰硬表面质感、"
          "游戏实时光影、干净明暗过渡、写实二次元游戏质感、画面锐利通透。时代背景："
          "当代中国都市，盛夏午后。角色设定：22岁中国都市年轻女性，身高162cm。体态纤细"
          "偏瘦，肩线窄而柔和，骨相柔和偏鹅蛋脸，脸部线条圆润略带少量清秀稚气。五官清秀"
          "端正，平直细眉，圆杏眼，鼻梁小巧挺直，唇形偏薄。乌黑中长发扎成低马尾，发丝"
          "柔顺垂至肩胛骨下方，额前有轻薄碎发。气质关键词：干净、清秀、朴素、带初入社会"
          "的青涩感。上身穿白色棉质圆领短袖T恤，衣摆自然垂至胯部；下身穿浅蓝色高腰直筒"
          "九分牛仔裤，裤脚微微卷起。脚穿白色帆布鞋，平底，鞋面干净。空手。常态平静表情，"
          "眼睛平视镜头。")
NEG = "低清，畸形，多余肢体，乱码文字"


def gen(tag: str, w: int, h: int) -> None:
    wf = {
        "unet": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "flux-2-klein-4b-fp8.safetensors",
            "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen_3_4b.safetensors",
            "type": "flux2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {
            "vae_name": "flux2-vae.safetensors"}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["clip", 0], "text": PROMPT}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["clip", 0], "text": NEG}},
        "guidance": {"class_type": "FluxGuidance", "inputs": {
            "conditioning": ["pos", 0], "guidance": 4.0}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": 20250923}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {
            "sampler_name": "euler"}},
        "sigmas": {"class_type": "BasicScheduler", "inputs": {
            "model": ["unet", 0], "scheduler": "simple",
            "steps": 36, "denoise": 1.0}},
        "guider": {"class_type": "CFGGuider", "inputs": {
            "model": ["unet", 0], "positive": ["guidance", 0],
            "negative": ["neg", 0], "cfg": 4.0}},
        "latent": {"class_type": "EmptySD3LatentImage", "inputs": {
            "width": w, "height": h, "batch_size": 1}},
        "sample": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0],
            "sampler": ["sampler", 0], "sigmas": ["sigmas", 0],
            "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {
            "samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {
            "images": ["decode", 0], "filename_prefix": f"klein4bfp8_{tag}"}},
    }
    t0 = time.time()
    req = urllib.request.Request(
        BASE + "/prompt", data=json.dumps(
            {"prompt": wf, "client_id": "poc4bfp8"}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        pid = json.load(r)["prompt_id"]
    print(f"[{tag}] prompt_id={pid}", flush=True)
    while True:
        time.sleep(5)
        with urllib.request.urlopen(BASE + "/history/" + pid, timeout=10) as r:
            hist = json.load(r)
        if pid not in hist:
            print(f"[{tag}] {time.time()-t0:.0f}s running...", flush=True)
            continue
        st = hist[pid].get("status", {})
        if st.get("status_str") == "error":
            print(f"[{tag}] EXEC ERROR:", json.dumps(
                st.get("messages", []))[-400:], flush=True)
            return
        for node_out in hist[pid].get("outputs", {}).values():
            for img in node_out.get("images", []):
                if img.get("type") == "output":
                    print(f"[{tag}] DONE {time.time()-t0:.0f}s -> "
                          f"{img['filename']}", flush=True)
                    return


if __name__ == "__main__":
    gen("832x480", 832, 480)
    gen("1024", 1024, 1024)
