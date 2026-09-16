"""T3：Z-Image 写实上限探测——风格行改超写实摄影向。"""
import json, time, urllib.request
from pathlib import Path
BASE = "http://127.0.0.1:8189"
OUT = Path(r"E:\OmniSpace\data\generated\images")
PROMPT = ("美术风格：超写实3D游戏CG、次时代引擎渲染、真实皮肤材质纹理、"
          "电影级光效、8K画质、画面锐利通透、照片级真实感\n"
          "时代背景：当代中国都市，盛夏午后\n"
          "角色设定：22岁中国都市年轻女性，身高162cm。体态纤细偏瘦，肩线窄而柔和，"
          "骨相柔和偏鹅蛋脸，脸部线条圆润略带少量清秀稚气。五官清秀端正，平直细眉，"
          "圆杏眼，鼻梁小巧挺直，唇形偏薄。乌黑中长发扎成低马尾，发丝柔顺垂至肩胛骨下方，"
          "额前有轻薄碎发。气质关键词：干净、清秀、朴素、带初入社会的青涩感。"
          "上身穿白色棉质圆领短袖T恤，衣摆自然垂至胯部；下身穿浅蓝色高腰直筒九分牛仔裤，"
          "裤脚微微卷起。脚穿白色帆布鞋，平底，鞋面干净。空手。常态平静表情，眼睛平视镜头。")

def wf(prompt, w, h, seed):
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": "z_image_ae.safetensors"}},
        "ms": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet", 0], "shift": 3}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}},
        "latent": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "ks": {"class_type": "KSampler", "inputs": {"model": ["ms", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["latent", 0], "seed": 20260914, "steps": 8, "cfg": 1.0, "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1.0}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["dec", 0], "filename_prefix": "zimg"}},
    }

body = json.dumps({"prompt": wf(PROMPT, 512, 768, 20260914), "client_id": "zimg_t3"}).encode()
req = urllib.request.Request(f"{BASE}/prompt", data=body, headers={"Content-Type": "application/json"})
pid = json.load(urllib.request.urlopen(req, timeout=15))["prompt_id"]
t0 = time.time()
while time.time() - t0 < 300:
    h8 = json.load(urllib.request.urlopen(f"{BASE}/history/{pid}", timeout=10))
    if pid in h8:
        for node in h8[pid]["outputs"].values():
            for im in node.get("images", []):
                u = f"{BASE}/view?filename={im['filename']}&subfolder={im.get('subfolder','')}&type=output"
                (OUT / "zimg_t3_photoreal.png").write_bytes(urllib.request.urlopen(u, timeout=30).read())
        print(f"[T3 超写实] {time.time()-t0:.1f}s done", flush=True)
        break
    time.sleep(2)
