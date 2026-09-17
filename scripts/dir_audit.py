#!/usr/bin/env python3
"""OmniSpace 项目目录规范审计（docs/项目目录规范.md §6 配套工具）。

审计项：
  1. 根目录散件（§1.3 白名单外）
  2. 顶层目录白名单外目录（§1）
  3. ComfyUI ↔ models 重复副本（同名 ≥1GB 文件三分类：合规硬链接 /
     可零风险合并 / 版本冲突须人工裁定）
  4. logs/ 异常大文件与 ComfyUI output/ 体积播报

用法：
  runtime\\py310\\python.exe scripts\\dir_audit.py [--json] [--skip-models]

退出码：0 干净 / 1 有违规。只读不写，不做任何删除。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMFY_MODELS = ROOT / "tools" / "ComfyUI_windows_portable" / "ComfyUI" / "models"
# 2026-09-02 存储统一：ComfyUI 产物目录重定向至 data/comfyui/output
# （启动参数 --output-directory），引擎树内不再落产物
COMFY_OUTPUT = ROOT / "data" / "comfyui" / "output"
LOGS_DIR = ROOT / "logs"
# 存储统一绊线：引擎树内不允许出现这四个可写目录名下的任何文件
# （ComfyUI 导入期会在引擎树重建 0 字节空壳 input，后端关闭时自愈清除；
#  空壳不算违规，出现任何文件=重定向被绕过，算违规）
COMFY_ENGINE = ROOT / "tools" / "ComfyUI_windows_portable" / "ComfyUI"
ENGINE_WRITABLE_NAMES = ("output", "input", "temp", "user")

# §1.3 根目录散件白名单
ROOT_FILE_WHITELIST = {
    "CLAUDE.md", "README.md", "requirements.txt", "requirements-lock.txt",
    "pytest.ini", ".gitignore", "ruff.toml",
    # 09-08 AI 规矩门卫（纯指针文件，真源唯一=CLAUDE.md 防双源漂移）
    "AGENTS.md",
    "启动OmniSpace.bat", "停止OmniSpace.bat",
    # 09-02 改名：开发版 exe 带「开发版」后缀，防与发行包入口点混（曾因此
    # 把开发环境误当发行包测）；包内交付名不变，见 make_dist COPY_FILES
    "启动OmniSpace-开发版.exe",
    # 09-05 A5：打包台入口（7530 一条命）+ 控制台子进程调试遗留
    # （console.log 由打包台 exe 运行可再生，登记免报）
    "启动打包台.bat", "启动打包台.exe", "console.log",
}

# §1 顶层目录白名单
DIR_WHITELIST = {
    # 源码与工具链（git）
    "src", "frontend", "launcher", "scripts", "skills",
    "license_console", "packaging_console", "build_tools", "tests",
    "docs", "tools",
    # src 扁平化后 backend/ 仅剩兼容 shim（git 跟踪两个转发文件）
    "backend",
    # 09-17 工具缓存钉 E 盘：项目缓存根（pip/HF/triton/playwright）
    # .cache 在下方工具缓存免审区也有——此处移除避免 B033 重复
    # 09-08 升级机制批1：升级独立执行器（stdlib-only，随包出厂，
    # 方案=docs/升级机制方案-2026-09-08.md）
    "updater",
    # 插件系统 P1/P2 2026-09-16：进程内插件目录（PR#2 首包已接入
    # OSP v1 运行时；发行包不含——make_dist 白名单制天然排除）
    "plugin",
    # 09-11 升级机制批2：用户拖升级包的指定目录（运行时生成，payload
    # 禁触区=升级自愈工作区；方案 §2.1）
    "updates",
    # 资产与运行时（非 git）；根 ffmpeg/ 空壳已删（真身 runtime/ffmpeg，09-02）
    "models", "runtime", "pydeps", "keys", "data", "logs",
    # 09-02 规则：发行产物一律住 D:\ccd，开发目录出现 dist_out 即告警
    # 登记的历史目录（清理清单处置中，处置完删除登记行）
    "采集素材",
    # 工具缓存（免审）
    ".cache", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".githooks", ".vscode",
    ".mimosa", ".zcode", ".trae", ".trae-html-share-packages", ".git",
}

# 重复副本扫描门槛
MIN_DUP_BYTES = 1024 ** 3
# HF 缓存通用文件名：两侧树上同名不同物是常态（09-10 实录：ComfyUI 侧
# CLIP-ViT-L 快照 1.59G vs 项目侧 Qwen9B 主权重 10.2G 同名「版本冲突」
# 误报），不参与裸名比对
_GENERIC_HF_NAMES = {"model.safetensors", "pytorch_model.bin"}
# logs 单文件报警线（轮转 10M×5 之外的大文件）
LOG_BIG_BYTES = 200 * 1024 * 1024

_GBK = sys.stdout.encoding and "gbk" in sys.stdout.encoding.lower()
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.2f}GB"


def audit_root_files(report: dict) -> None:
    files = sorted(p for p in ROOT.iterdir() if p.is_file())
    bad = [p.name for p in files if p.name not in ROOT_FILE_WHITELIST]
    report["root_files"]["total"] = len(files)
    report["root_files"]["violations"] = bad


def audit_dirs(report: dict) -> None:
    dirs = sorted(p.name for p in ROOT.iterdir() if p.is_dir())
    bad = [d for d in dirs if d not in DIR_WHITELIST]
    report["top_dirs"]["total"] = len(dirs)
    report["top_dirs"]["violations"] = bad


def _index_tree(base: Path) -> dict[str, dict]:
    """按文件名索引 ≥1GB 文件：{name: {path, size, inode, nlink}}。"""
    out: dict[str, dict] = {}
    if not base.is_dir():
        return out
    for dirpath, _dirnames, filenames in os.walk(base):
        for fn in filenames:
            fp = Path(dirpath) / fn
            try:
                st = fp.stat()
            except OSError:
                continue
            if st.st_size < MIN_DUP_BYTES:
                continue
            # 同名多份时保留较大者（保守：重复副本按最大算浪费）
            prev = out.get(fn)
            if prev is None or st.st_size > prev["size"]:
                out[fn] = {
                    "path": str(fp), "size": st.st_size,
                    "inode": st.st_ino, "nlink": st.st_nlink,
                }
    return out


def audit_model_dups(report: dict) -> None:
    comfy = _index_tree(COMFY_MODELS)
    ours = _index_tree(ROOT / "models")
    linked, mergeable, conflict = [], [], []
    for fn, c in comfy.items():
        if fn in _GENERIC_HF_NAMES:
            continue  # 通用名同物率低，比对只会产伪冲突
        o = ours.get(fn)
        if o is None:
            continue  # 仅 ComfyUI 侧（例外登记或工具文件），不在本审计口径
        if c["inode"] == o["inode"]:
            linked.append(fn)
        elif c["size"] == o["size"]:
            mergeable.append({"name": fn, "size_gb": round(c["size"] / 1024 ** 3, 2),
                              "comfy": c["path"], "ours": o["path"]})
        else:
            conflict.append({
                "name": fn,
                "comfy": {"path": c["path"], "size": _gb(c["size"])},
                "ours": {"path": o["path"], "size": _gb(o["size"])},
            })
    report["model_dups"] = {
        "linked_ok": sorted(linked),
        "mergeable": sorted(mergeable, key=lambda x: -x["size_gb"]),
        "conflict": sorted(conflict, key=lambda x: x["name"]),
        "waste_gb_mergeable": round(sum(x["size_gb"] for x in mergeable), 2),
    }


def audit_logs_and_output(report: dict) -> None:
    big_logs = []
    if LOGS_DIR.is_dir():
        for p in LOGS_DIR.rglob("*"):
            if p.is_file():
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue
                if sz >= LOG_BIG_BYTES:
                    big_logs.append({"path": p.name, "size": _gb(sz)})
    out_gb = 0.0
    if COMFY_OUTPUT.is_dir():
        for p in COMFY_OUTPUT.rglob("*"):
            if p.is_file():
                try:
                    out_gb += p.stat().st_size
                except OSError:
                    pass
    report["products"] = {
        "big_logs": big_logs,
        "comfy_output_gb": round(out_gb / 1024 ** 3, 2),
    }


def audit_engine_tree_purity(report: dict) -> None:
    """存储统一绊线：引擎树四个可写目录名下出现任何文件即违规。"""
    found: list[dict] = []
    empty: list[str] = []
    for name in ENGINE_WRITABLE_NAMES:
        d = COMFY_ENGINE / name
        if not d.exists():
            continue
        files = [p for p in d.rglob("*") if p.is_file()]
        if files:
            sz = 0
            for p in files:
                try:
                    sz += p.stat().st_size
                except OSError:
                    pass
            found.append({"name": name, "files": len(files),
                          "mb": round(sz / 1024 ** 2, 2)})
        else:
            empty.append(name)
    report["engine_tree"] = {"files_found": found, "empty_placeholders": empty}


def print_report(report: dict) -> None:
    r = report
    print("=" * 62)
    print("OmniSpace 项目目录规范体检（docs/项目目录规范.md）")
    print("=" * 62)

    rf = r["root_files"]
    print(f"\n[1] 根目录散件：共 {rf['total']} 个文件，白名单 {len(ROOT_FILE_WHITELIST)} 个")
    if rf["violations"]:
        print(f"    ✗ 违规 {len(rf['violations'])} 个（去 scripts/scratch/ 或删除）：")
        for name in rf["violations"][:80]:
            print(f"      - {name}")
        if len(rf["violations"]) > 80:
            print(f"      … 其余 {len(rf['violations']) - 80} 个见 --json")
    else:
        print("    ✓ 全部在白名单内")

    td = r["top_dirs"]
    print(f"\n[2] 顶层目录：共 {td['total']} 个")
    if td["violations"]:
        print(f"    ✗ 白名单外 {len(td['violations'])} 个（登记进规范或清理）：")
        for name in td["violations"]:
            print(f"      - {name}/")
    else:
        print("    ✓ 全部在白名单内")

    if "model_dups" in r:
        md = r["model_dups"]
        print(f"\n[3] ComfyUI ↔ models 重复副本（同名 ≥1GB，共 "
              f"{len(md['linked_ok']) + len(md['mergeable']) + len(md['conflict'])} 对）：")
        print(f"    ✓ 合规硬链接 {len(md['linked_ok'])} 对（同 inode 零冗余）")
        if md["mergeable"]:
            print(f"    ◐ 可零风险合并 {len(md['mergeable'])} 对，浪费合计 "
                  f"{md['waste_gb_mergeable']}GB（os.link 挂接即可）：")
            for x in md["mergeable"]:
                print(f"      - {x['name']}（{x['size_gb']}GB）")
        if md["conflict"]:
            print(f"    ✗ 版本冲突 {len(md['conflict'])} 对（两边字节不同，须人工裁定保留哪版）：")
            for x in md["conflict"]:
                print(f"      - {x['name']}：ComfyUI 侧 {x['comfy']['size']}"
                      f" vs 项目侧 {x['ours']['size']}")
        if not md["mergeable"] and not md["conflict"]:
            print("    ✓ 无重复副本")

    pr = r["products"]
    print(f"\n[4] 运行产物：ComfyUI output/（data/comfyui）累计 {pr['comfy_output_gb']}GB")
    if pr["big_logs"]:
        print("    ◐ logs/ 超大单文件（>200MB，超出 10M×5 轮转口径）：")
        for x in pr["big_logs"]:
            print(f"      - {x['path']}（{x['size']}）")
    else:
        print("    ✓ logs/ 无异常大文件")

    et = r.get("engine_tree", {})
    ef = et.get("files_found", [])
    ep = et.get("empty_placeholders", [])
    print("\n[5] 引擎树纯净度（存储统一绊线：可写目录应只存在于 data/comfyui）")
    if ef:
        print("    ✗ 引擎树内发现落盘文件（重定向被绕过，须排查）：")
        for x in ef:
            print(f"      - ComfyUI/{x['name']}/：{x['files']} 个文件（{x['mb']}MB）")
    else:
        print("    ✓ 引擎树内无可写数据落盘")
    if ep:
        print(f"    ◐ 0 字节启动占位 {len(ep)} 个（{', '.join(ep)}；"
              f"后端关闭时自动清除，非违规）")

    violations = (len(rf["violations"]) + len(td["violations"])
                  + len(r.get("model_dups", {}).get("mergeable", []))
                  + len(r.get("model_dups", {}).get("conflict", []))
                  + len(ef))
    print("\n" + "=" * 62)
    print("结论：" + ("✓ 干净，符合规范" if violations == 0
                        else f"✗ 共 {violations} 项违规（散件+目录+模型副本）"))
    print("=" * 62)


def main() -> int:
    parser = argparse.ArgumentParser(description="OmniSpace 项目目录规范审计")
    parser.add_argument("--json", action="store_true", help="机读 JSON 输出")
    parser.add_argument("--skip-models", action="store_true",
                        help="跳过 ComfyUI ↔ models 大扫描（秒出）")
    args = parser.parse_args()

    report: dict = {"root_files": {}, "top_dirs": {}}
    audit_root_files(report)
    audit_dirs(report)
    if not args.skip_models:
        audit_model_dups(report)
    audit_logs_and_output(report)
    audit_engine_tree_purity(report)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)

    violations = (len(report["root_files"]["violations"])
                  + len(report["top_dirs"]["violations"])
                  + len(report.get("model_dups", {}).get("mergeable", []))
                  + len(report.get("model_dups", {}).get("conflict", [])))
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
