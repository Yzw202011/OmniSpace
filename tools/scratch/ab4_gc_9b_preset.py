"""9b 生产 quality 档复测（36步/cfg4.0）——排除 cfg7.5 对风格的干扰。"""
import sys, time
from pathlib import Path
sys.path.insert(0, r"E:\OmniSpace")

PROMPT = ("美术风格：3D GC 游戏风格、高精度 3D 建模、PBR 物理渲染、清晰硬表面质感、"
          "游戏实时光影、干净明暗过渡、写实二次元游戏质感、画面锐利通透\n"
          "时代背景：当代中国都市，盛夏午后\n"
          "角色设定：22岁中国都市年轻女性，身高162cm。体态纤细偏瘦，肩线窄而柔和，"
          "骨相柔和偏鹅蛋脸，脸部线条圆润略带少量清秀稚气。五官清秀端正，平直细眉，"
          "圆杏眼，鼻梁小巧挺直，唇形偏薄。乌黑中长发扎成低马尾，发丝柔顺垂至肩胛骨下方，"
          "额前有轻薄碎发。气质关键词：干净、清秀、朴素、带初入社会的青涩感。"
          "上身穿白色棉质圆领短袖T恤，衣摆自然垂至胯部；下身穿浅蓝色高腰直筒九分牛仔裤，"
          "裤脚微微卷起。脚穿白色帆布鞋，平底，鞋面干净。空手。常态平静表情，眼睛平视镜头。")
from backend.services.inference.comfy_paint_engine import get_comfy_paint_engine
from backend.services.inference.comfy_proc import get_comfy_proc
eng = get_comfy_paint_engine()
t0 = time.perf_counter()
r = eng.generate({"prompt": PROMPT, "negative": "", "seed": 20260914,
                  "steps": 36, "cfg": 4.0, "sampler": "euler",
                  "width": 512, "height": 768})
r["images"][0].save(r"E:\OmniSpace\data\generated\images\ab4_gc_pure9b_q36.png", "PNG")
print(f"[纯B 9b quality档36步cfg4] wall={time.perf_counter()-t0:.1f}s", flush=True)
get_comfy_proc().shutdown()
print("DONE")
