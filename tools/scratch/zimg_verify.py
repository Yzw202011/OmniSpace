"""Z-Image-Turbo 同题验证：T1=四视图 1280x720（对标 ab5/竞品）；T2=人像 512x768（对标 ab4）。"""
import json, time, urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8189"
OUT = Path(r"E:\OmniSpace\data\generated\images")
SEED = 20260914

PROMPT_4V = ("生成角色4视图：正面全身、侧面全身、背面全身、上半身特写。纯白色背景，"
             "禁止纹理，全局光照，禁止投影。图片左上角标注中文角色名：夏沐沐；"
             "各视图下方标注中文：正面全身、侧面全身、背面全身、上半身特写；"
             "禁止出现任何其他文字；禁止外语字符。\n"
             "美术风格：3D GC 游戏风格、高精度 3D 建模、PBR 物理渲染、清晰硬表面质感、"
             "游戏实时光影、干净明暗过渡、写实二次元游戏质感、画面锐利通透\n"
             "时代背景：当代中国都市，盛夏午后\n"
             "角色设定：22岁中国都市年轻女性，身高162cm。体态纤细偏瘦，肩线窄而柔和，"
             "骨相柔和偏鹅蛋脸，脸部线条圆润略带少量清秀稚气。五官清秀端正，平直细眉，"
             "圆杏眼，鼻梁小巧挺直，唇形偏薄。乌黑中长发扎成低马尾，发丝柔顺垂至肩胛骨下方，"
             "额前有轻薄碎发。气质关键词：干净、清秀、朴素、带初入社会的青涩感。"
             "上身穿白色棉质圆领短袖T恤，衣摆自然垂至胯部；下身穿浅蓝色高腰直筒九分牛仔裤，"
             "裤脚微微卷起。脚穿白色帆布鞋，平底，鞋面干净。空手。常态平静表情，眼睛平视镜头。")
PROMPT_PORTRAIT = PROMPT_4V.split("\n", 1)[1]  # 同 ab4：去 4 视图版式行

def wf(prompt, w, h, seed):
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": "z_image_ae.safetensors"}},
        "ms": {"class_type": "ModelSamplingAuraFlow", "inputs": {
            "model": ["unet", 0], "shift": 3}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}},
        "latent": {"class_type": "EmptySD3LatentImage", "inputs": {
            "width": w, "height": h, "batch_size": 1}},
        "ks": {"class_type": "KSampler", "inputs": {
            "model": ["ms", 0], "positive": ["pos", 0], "negative": ["neg", 0],
            "latent_image": ["latent", 0], "seed": seed, "steps": 8, "cfg": 1.0,
            "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1.0}},
        "dec": {"class_type": "VAEDecode", "inputs": {
            "samples": ["ks", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {
            "images": ["dec", 0], "filename_prefix": "zimg"}},
    }

def run(tag, prompt, w, h, seed):
    body = json.dumps({"prompt": wf(prompt, w, h, seed), "client_id": "zimg_verify"}).encode()
    req = urllib.request.Request(f"{BASE}/prompt", data=body,
                                 headers={"Content-Type": "application/json"})
    pid = json.load(urllib.request.urlopen(req, timeout=15))["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < 600:
        h8 = json.load(urllib.request.urlopen(f"{BASE}/history/{pid}", timeout=10))
        if pid in h8:
            outs = h8[pid]["outputs"]
            for node in outs.values():
                for im in node.get("images", []):
                    if im.get("type") == "output":
                        fn, sub = im["filename"], im.get("subfolder", "")
                        u = f"{BASE}/view?filename={fn}&subfolder={sub}&type=output"
                        data = urllib.request.urlopen(u, timeout=30).read()
                        p = OUT / f"zimg_{tag}.png"
                        p.write_bytes(data)
                        print(f"[{tag}] {time.time()-t0:.1f}s -> {p} ({len(data)//1024}KB)", flush=True)
                        return True
        time.sleep(2)
    print(f"[{tag}] TIMEOUT", flush=True)
    return False

ok1 = run("4view_720p", PROMPT_4V, 1280, 720, SEED)
ok2 = run("portrait_512x768", PROMPT_PORTRAIT, 512, 768, SEED)
print("ZIMG-VERIFY", "DONE" if ok1 and ok2 else "PARTIAL")
