"""Z-Image-Turbo 二开参数矩阵（社区玩法 vs 官方基线）。
组合：A=官方基线 8/cfg1/res_multistep/simple/shift3
     B=社区 beta 锐利 8/cfg1/euler/beta/shift3
     C=cfg 提升 10/cfg1.6/res_multistep/simple/shift3 + 真负向
     D=dpmpp 全能 12/cfg2.0/dpmpp_2m/beta/shift3 + 真负向
     E=高 shift 构图 10/cfg1.6/euler/beta/shift5 + 真负向
两题：四视图 720p / 人像超写实 512x768。同 seed。
"""
import json, time, urllib.request
from pathlib import Path
BASE = "http://127.0.0.1:8189"
OUT = Path(r"E:\OmniSpace\data\generated\images")
SEED = 20260914
NEG = "低分辨率, 模糊, 畸形, 多人, 水印, 杂乱背景"

STYLE_3D = ("美术风格：3D GC 游戏风格、高精度 3D 建模、PBR 物理渲染、清晰硬表面质感、"
            "游戏实时光影、干净明暗过渡、写实二次元游戏质感、画面锐利通透\n")
STYLE_REAL = ("美术风格：超写实3D游戏CG、次时代引擎渲染、真实皮肤材质纹理、"
              "电影级光效、8K画质、画面锐利通透、照片级真实感\n")
COMMON = ("时代背景：当代中国都市，盛夏午后\n"
          "角色设定：22岁中国都市年轻女性，身高162cm。体态纤细偏瘦，肩线窄而柔和，"
          "骨相柔和偏鹅蛋脸，脸部线条圆润略带少量清秀稚气。五官清秀端正，平直细眉，"
          "圆杏眼，鼻梁小巧挺直，唇形偏薄。乌黑中长发扎成低马尾，发丝柔顺垂至肩胛骨下方，"
          "额前有轻薄碎发。气质关键词：干净、清秀、朴素、带初入社会的青涩感。"
          "上身穿白色棉质圆领短袖T恤，衣摆自然垂至胯部；下身穿浅蓝色高腰直筒九分牛仔裤，"
          "裤脚微微卷起。脚穿白色帆布鞋，平底，鞋面干净。空手。常态平静表情，眼睛平视镜头。")
P4V = ("生成角色4视图：正面全身、侧面全身、背面全身、上半身特写。纯白色背景，"
       "禁止纹理，全局光照，禁止投影。图片左上角标注中文角色名：夏沐沐；"
       "各视图下方标注中文：正面全身、侧面全身、背面全身、上半身特写；"
       "禁止出现任何其他文字；禁止外语字符。\n" + STYLE_3D + COMMON)

COMBOS = [
    ("A_official", dict(steps=8,  cfg=1.0, sampler="res_multistep", scheduler="simple", shift=3, real_neg=False)),
    ("B_beta",     dict(steps=8,  cfg=1.0, sampler="euler",         scheduler="beta",   shift=3, real_neg=False)),
    ("C_cfg16",    dict(steps=10, cfg=1.6, sampler="res_multistep", scheduler="simple", shift=3, real_neg=True)),
    ("D_dpmpp2m",  dict(steps=12, cfg=2.0, sampler="dpmpp_2m",     scheduler="beta",   shift=3, real_neg=True)),
    ("E_shift5",   dict(steps=10, cfg=1.6, sampler="euler",         scheduler="beta",   shift=5, real_neg=True)),
]

def wf(prompt, w, h, seed, p):
    neg_node = ("neg", {"class_type": "CLIPTextEncode",
                        "inputs": {"clip": ["clip", 0], "text": NEG}}) if p["real_neg"] \
        else ("negz", {"class_type": "ConditioningZeroOut",
                       "inputs": {"conditioning": ["pos", 0]}})
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": "z_image_ae.safetensors"}},
        "ms": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet", 0], "shift": p["shift"]}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        neg_node[0]: neg_node[1],
        "latent": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "ks": {"class_type": "KSampler", "inputs": {"model": ["ms", 0], "positive": ["pos", 0], "negative": [neg_node[0], 0], "latent_image": ["latent", 0], "seed": seed, "steps": p["steps"], "cfg": p["cfg"], "sampler_name": p["sampler"], "scheduler": p["scheduler"], "denoise": 1.0}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["dec", 0], "filename_prefix": "zimg2"}},
    }

def run(tag, prompt, w, h, p):
    body = json.dumps({"prompt": wf(prompt, w, h, SEED, p), "client_id": "matrix2"}).encode()
    req = urllib.request.Request(f"{BASE}/prompt", data=body, headers={"Content-Type": "application/json"})
    pid = json.load(urllib.request.urlopen(req, timeout=15))["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < 300:
        h8 = json.load(urllib.request.urlopen(f"{BASE}/history/{pid}", timeout=10))
        if pid in h8:
            st = h8[pid].get("status", {}).get("status_str")
            if st == "error":
                print(f"[{tag}] EXEC_ERROR", flush=True); return
            for node in h8[pid]["outputs"].values():
                for im in node.get("images", []):
                    u = f"{BASE}/view?filename={im['filename']}&subfolder={im.get('subfolder','')}&type=output"
                    (OUT / f"zimg2_{tag}.png").write_bytes(urllib.request.urlopen(u, timeout=30).read())
            print(f"[{tag}] {time.time()-t0:.1f}s done", flush=True); return
        time.sleep(2)
    print(f"[{tag}] TIMEOUT", flush=True)

for name, p in COMBOS:
    run(f"{name}_4v", P4V, 1280, 720, p)
    run(f"{name}_portrait", STYLE_REAL + COMMON, 512, 768, p)
print("MATRIX2-DONE")
