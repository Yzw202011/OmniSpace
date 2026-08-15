# -*- coding: utf-8 -*-
"""盘点非 PASS 用例，按可修复性启发式分类输出。"""
import json
import re
from pathlib import Path

R = Path(__file__).parent / "results"
MODULES = ["sys", "chat", "paint", "comic", "learn", "model", "style", "set", "cross"]

# 分类规则（命中关键词即归类，顺序即优先级）
RULES = [
    ("A_模型资产未随包_不可离线修复",
     ["未随包", "未分发", "未下载", "权重不存在", "基座目录不存在", "LTX-2", "Wan2.1",
      "CogVideoX", "FLUX", "Kolors", "IP-Adapter-Plus", "ControlNet-OpenPose",
      "Real-ESRGAN", "Qwen3-VL-8B", "Qwen3-14B", "ChatGLM", "Yi-1.5", "vLLM",
      "Stable Zero123", "Wonder3D", "LGM", "CosyVoice", "ChatTTS"]),
    ("B_纯前端交互_可浏览器冒烟转PASS",
     ["纯前端", "留待UI", "留待浏览器", "留待冒烟", "UI冒烟", "前端交互", "前端逻辑",
      "留待UI冒烟", "页面", "拖拽", "置灰", "悬浮", "折叠", "展开"]),
    ("C_已随包模型零接线_可代码修复",
     ["零接线", "无 API 端点", "TripoSR", "SAM", "MiDaS", "YOLO", "GPT-SoVITS",
      "AnimateLCM", "已随包"]),
    ("D_端点字段缺失_可代码修复",
     ["端点缺失", "字段缺失", "未实现", "无 camera_", "无 duration", "无 sort_index"]),
    ("E_物理环境依赖_不可自动化修复",
     ["物理机", "物理更换", "低配", "环境矩阵", "断网物理", "拔网线", "长稳环境",
      "8小时", "Tauri壳", "物理断网"]),
]

cats: dict[str, list] = {k: [] for k, _ in RULES}
cats["Z_待人工分诊"] = []

for m in MODULES:
    p = R / f"{m}.json"
    if not p.is_file():
        continue
    doc = json.loads(p.read_text(encoding="utf-8"))
    for c in doc["cases"]:
        if c["status"] == "PASS":
            continue
        text = f"{c['name']} {c.get('detail') or ''}"
        hit = "Z_待人工分诊"
        for cat, kws in RULES:
            if any(kw in text for kw in kws):
                hit = cat
                break
        cats[hit].append((c["tc_id"], c["status"], c.get("priority", ""), c["name"],
                          (c.get("detail") or "")[:90]))

total = 0
for cat, items in cats.items():
    print(f"\n===== {cat} ({len(items)}) =====")
    by_status = {}
    for it in items:
        by_status[it[1]] = by_status.get(it[1], 0) + 1
    print("  状态分布:", by_status)
    total += len(items)
print("\n非 PASS 总数:", total)

# 输出 D/C 类全量（可代码修复主战场）
for cat in ("D_端点字段缺失_可代码修复", "C_已随包模型零接线_可代码修复"):
    print(f"\n----- {cat} 明细 -----")
    for tid, st, pri, name, det in cats[cat]:
        print(f"{tid} [{st}/{pri}] {name} | {det}")
