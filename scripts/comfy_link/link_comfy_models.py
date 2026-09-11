# 本项目仅供学习使用，商业授权请+Q 3559331368
r"""ComfyUI 模型挂接工具（P8 补洞：包内 ComfyUI 不读中央 models 目录）。

大白话：本机开发环境里，ComfyUI 靠硬链接「一份文件两个门牌」读中央
models；发行包里这层挂接是空的。本工具按随包的 comfy_model_map.json
映射表，把中央 models 里已存在的文件硬链接进 ComfyUI 分类目录——
同卷硬链接零额外占用，幂等可重复执行。

用法（包根目录下）：
  runtime\py310\python.exe modelxiazai\link_comfy_models.py          # 补挂
  runtime\py310\python.exe modelxiazai\link_comfy_models.py --check  # 只体检
生成映射表（卖家机，扫本机现有硬链接）：
  runtime\py310\python.exe modelxiazai\link_comfy_models.py --export
"""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMFY_MODELS = (REPO / "tools" / "ComfyUI_windows_portable" / "ComfyUI"
                / "models")
CENTRAL = REPO / "models"
MAP_FILE = Path(__file__).resolve().parent / "comfy_model_map.json"

FSCTL_GET_REPARSE_POINT = 0x900A8
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3


def _hardlink_partner(path: Path) -> str | None:
    """返回该文件的另一条硬链接路径（无则 None）。

    fsutil 输出的路径不带盘符（\\OmniSpace\\...），按被扫文件的盘符
    补全（硬链接同卷，盘符必一致）。"""
    try:
        r = subprocess.run(["fsutil", "hardlink", "list", str(path)],
                           capture_output=True, text=True, timeout=10)
        me = str(path).lower()
        for ln in r.stdout.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if ln[1:2] != ":":
                ln = path.drive + ln
            if ln.lower() != me:
                return ln
        return None
    except Exception:  # noqa: BLE001
        return None


def export_map() -> None:
    """扫本机 ComfyUI/models 全部文件的硬链接对端 → 生成映射表。"""
    mapping: dict[str, str] = {}  # comfy 相对路径 -> 中央 models 相对路径
    missing: list[str] = []
    for f in COMFY_MODELS.rglob("*"):
        if not f.is_file() or f.suffix.lower() in (".txt", ".json", ".md"):
            continue
        partner = _hardlink_partner(f)
        if not partner:
            continue
        p = Path(partner)
        try:
            rel = p.relative_to(CENTRAL).as_posix()
        except ValueError:
            continue  # 对端不在中央 models（ComfyUI 私有文件），不进表
        mapping[f.relative_to(COMFY_MODELS).as_posix()] = rel
        if not (CENTRAL / rel).exists():
            missing.append(rel)
    MAP_FILE.write_text(json.dumps(
        {"version": 1, "exported_from": "dev-machine",
         "map": mapping}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"映射表已生成：{len(mapping)} 条 → {MAP_FILE}")
    if missing:
        print(f"⚠️ {len(missing)} 条对端在中央 models 已不存在（将被跳过）")


def _os_link(src: Path, dst: Path) -> None:
    os_link = ctypes.windll.kernel32.CreateHardLinkW
    if not os_link(str(dst), str(src), None):
        raise OSError(ctypes.get_last_error(), "CreateHardLinkW 失败",
                      str(dst))


def link_or_check(check_only: bool) -> int:
    if not MAP_FILE.exists():
        print(f"❌ 找不到映射表：{MAP_FILE}（先在卖家机 --export 生成）")
        return 1
    mapping = json.loads(MAP_FILE.read_text("utf-8"))["map"]
    ok = linked = missing = conflict = 0
    for comfy_rel, central_rel in mapping.items():
        src = CENTRAL / central_rel
        dst = COMFY_MODELS / comfy_rel
        if not src.is_file():
            missing += 1
            continue
        if dst.exists():
            ok += 1
            continue
        if check_only:
            ok += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            _os_link(src, dst)
            linked += 1
        except OSError as exc:
            conflict += 1
            print(f"  ⚠️ 链接失败 {comfy_rel}: {exc}")
    mode = "体检" if check_only else "补挂"
    print(f"{mode}完成：已就位 {ok} + 本次新链 {linked}；"
          f"中央缺文件 {missing}（按模型清单补齐后重跑）；失败 {conflict}")
    return 0 if conflict == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true", help="卖家机生成映射表")
    ap.add_argument("--check", action="store_true", help="只体检不落链接")
    args = ap.parse_args()
    if args.export:
        export_map()
        return 0
    return link_or_check(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
