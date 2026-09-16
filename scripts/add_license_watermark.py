"""授权水印补打脚本（2026-09-10 全量 474 处的复跑工具）。

对第一方代码（src/launcher/scripts/frontend/src）逐文件检查，
缺水印者补一条（随机头部/尾部位置，幂等——已含则跳过）。
新增加的代码文件跑一遍即可补齐。

用法：runtime/py310/python.exe scripts/add_license_watermark.py
"""
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATERMARK_PY = "# 本项目仅供学习使用，商业授权请+Q 3559331368"
WATERMARK_JS = "// 本项目仅供学习使用，商业授权请+Q 3559331368"

def main() -> int:
    random.seed()
    roots = [ROOT / "backend", ROOT / "launcher", ROOT / "scripts",
             ROOT / "frontend" / "src"]
    files = []
    for root in roots:
        for p in root.rglob("*"):
            if p.suffix in (".py", ".ts", ".tsx", ".js"):
                s = str(p)
                if "__pycache__" in s or "node_modules" in s or "/dist/" in s:
                    continue
                if p.name.endswith(".d.ts"):
                    continue
                files.append(p)
    random.shuffle(files)
    done = skipped = 0
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            skipped += 1
            continue
        if "商业授权请" in text:
            skipped += 1
            continue
        lines = text.splitlines(keepends=True)
        wm = WATERMARK_PY if p.suffix == ".py" else WATERMARK_JS
        if random.choice(["header", "footer"]) == "header":
            lines.insert(0, wm + "\n")
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] = lines[-1] + "\n"
            lines.append(wm + "\n")
        p.write_text("".join(lines), encoding="utf-8")
        done += 1
    print(f"补打 {done} 处，已含跳过 {skipped} 个，总文件 {len(files)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
