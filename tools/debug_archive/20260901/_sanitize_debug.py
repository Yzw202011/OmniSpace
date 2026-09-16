import re
import sys

sys.path.insert(0, r"e:\OmniSpace")
from src.api.manga.comic_asset import _build_character_prompt_v2

setting = ("16岁中国当代普通高中女生，身高160cm。体态匀称清瘦。"
           "上身穿白色与藏青色拼接运动校服外套。")
full = _build_character_prompt_v2("夏沐沐", setting)

pats = (r"【[^】]*】[：:][^\n。]*。?",
        r"[^。]*[四4]视图[^。]*(?:。|$)",
        r"图片[左右]上角[^。]*(?:。|$)",
        r"[^。]*标注[^。]*(?:。|$)",
        r"禁止[^。]*(?:。|$)",
        r"纯白色背景[^。]*(?:。|$)",
        r"全局光照[^。]*(?:。|$)",
        r"^\s*绘图提示词[：:]\s*$",
        r"^\s*美术风格[：:]\s*[^。\n]*(?:。|$)",
        r"^\s*时代背景[：:]\s*",
        )
cleaned = full
for pat in pats:
    before = cleaned
    cleaned = re.sub(pat, "", cleaned, flags=re.MULTILINE)
    if before != cleaned:
        print(f"--- {pat} 命中 ---")
        print(cleaned)
        print()
cleaned = re.sub(r"^\s*角色设定[：:]\s*", "", cleaned, flags=re.MULTILINE)
cleaned = re.sub(r"^\s*角色[：:]\s*", "", cleaned, flags=re.MULTILINE)
print("=== 最终 ===")
print(repr(cleaned))
