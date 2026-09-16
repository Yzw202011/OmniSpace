"""A/B 第五轮·四视图纯文字对标竞品（2026-09-15）。
用户原描述词直入，各栈生产最优档：4b=20步、9b=quality 档 36步/cfg4.0。
"""
import sys, time
from pathlib import Path
sys.path.insert(0, r"E:\OmniSpace")

PROMPT = ("生成角色4视图：正面全身、侧面全身、背面全身、上半身特写。纯白色背景，"
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
SEED = 20260914
OUT = Path(r"E:\OmniSpace\data\generated\images")

from src.services.inference.paint_engine import get_paint_engine
pe = get_paint_engine()
assert pe.ensure_loaded("flux2-klein-4b"), pe.get_status().get("last_error")
t0 = time.perf_counter()
r = pe.generate({"prompt": PROMPT, "negative": "", "seed": SEED,
                 "steps": 20, "cfg": 7.5, "sampler": "euler_a",
                 "width": 1280, "height": 720})
r["images"][0].save(OUT / "ab5_4view_4b.png", "PNG")
print(f"[4b 四视图 1280x720] wall={time.perf_counter()-t0:.1f}s", flush=True)

from src.services.inference.comfy_paint_engine import get_comfy_paint_engine
from src.services.inference.comfy_proc import get_comfy_proc
eng = get_comfy_paint_engine()
t0 = time.perf_counter()
r = eng.generate({"prompt": PROMPT, "negative": "", "seed": SEED,
                  "steps": 36, "cfg": 4.0, "sampler": "euler",
                  "width": 1280, "height": 720})
r["images"][0].save(OUT / "ab5_4view_9b.png", "PNG")
print(f"[9b 四视图 quality档] wall={time.perf_counter()-t0:.1f}s", flush=True)
get_comfy_proc().shutdown()
print("AB5-DONE")
