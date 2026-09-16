#!/usr/bin/env python3
"""build_rc.py — 一键生成 RC 交付目录（TASK-P0-01，审计发现 P20）。

用法（必须用嵌入式 Python）:
  e:\\OmniSpace\\runtime\\py310\\python.exe tools\\build_rc.py              # 同步 + 校验
  e:\\OmniSpace\\runtime\\py310\\python.exe tools\\build_rc.py --verify    # 仅校验差异（不复制）
  e:\\OmniSpace\\runtime\\py310\\python.exe tools\\build_rc.py --dest D:\\RC1002

规则（2026-08-19 TASK-P0-01 裁定）:
  1. E:\\RC1002 是脚本产出物，禁止手工改动；任何交付变更 = 改源目录 + 重跑本脚本
  2. 镜像同步（/MIR）：源侧删除会同步删除目标侧；排除清单内目录两侧都不动
  3. 排除清单变更 = 修改本文件 DEFAULT_* 常量并提交，改动了什么必须可追溯
  4. robocopy 退出码 0-7 为成功（0=无变化 1=有复制 …），>=8 为失败

排除理由登记:
  - .git / logs / data / __pycache__ / .pytest_cache: 审计任务原始规定（版本库/运行日志/用户数据/字节码缓存）
  - tools/git:            MinGit 版本控制工具（开发机专用，交付不需要）
  - node_modules:         前端构建期依赖，交付运行只用 frontend/dist
  - .vscode / .trae-html-share-packages: IDE/工具链私有目录
  - .cache:                vLLM/Triton 编译缓存（机器本地可再生，2026-08-21 vLLM 集成新增）
  - tmp_* / _tmp_* / *.log / e2e_state.json / *.pyc / Thumbs.db: 临时脚本与运行残留

pydeps 必须交付（2026-08-19 TASK-P0-06 实测裁定，推翻审计 P23 结论）:
  pydeps 不是残留，是与 runtime/py310/Lib/site-packages 互补的承重依赖站点
  （fastapi/numpy/scipy/diffusers/chromadb/modelscope/pytest 等 156 个包仅存于此，
  由 python310._pth 挂载且优先级高于 runtime）。排除它 = RC 后端无法启动。
  审计报告"37 空壳包"仅描述其中 dist-info 残壳；真死目录（pydeps/torch、
  pydeps/sympy、pydeps/tokenizers、pydeps/tests）已随 TASK-P0-06 删除。

注意（教训）: /XD 若用裸名 "data" 会连带排除 src/data 源码包
（.gitignore 同款事故），故顶层目录一律绝对路径，仅 __pycache__/node_modules
这类全树同名目录用裸名。

退出码: 0=同步且校验通过 | 2=复制失败 | 3=校验发现差异 | 4=参数/环境错误
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
DEFAULT_DEST = Path(r"E:\RC1002")

# 顶层排除（绝对路径，防误杀同名源码目录）
EXCLUDE_DIRS_TOP = [
    ".git", "data", "logs", ".pytest_cache", ".vscode",
    ".trae-html-share-packages", "tools/git", ".cache",
]
# 全树同名排除（裸名，任意层级生效）
EXCLUDE_DIRS_ANY = ["__pycache__", "node_modules"]
# 文件排除（通配符）
EXCLUDE_FILES = ["*.log", "e2e_state.json", "tmp_*", "_tmp_*", "*.pyc",
                 "Thumbs.db", ".DS_Store"]


def _robocopy_base() -> list[str]:
    args = ["robocopy", str(SRC), "", "/MIR", "/R:1", "/W:1",
            "/COPY:DAT", "/DCOPY:DAT", "/MT:32", "/NP", "/NDL"]
    args += ["/XD"] + [str(SRC / d) for d in EXCLUDE_DIRS_TOP] + EXCLUDE_DIRS_ANY
    args += ["/XF"] + EXCLUDE_FILES
    return args


def run_sync(dest: Path) -> int:
    print(f"[build_rc] 同步 {SRC} -> {dest}（排除 {len(EXCLUDE_DIRS_TOP)} 顶层目录 + "
          f"{len(EXCLUDE_DIRS_ANY)} 全树目录 + {len(EXCLUDE_FILES)} 文件模式）")
    args = _robocopy_base()
    args[args.index("") ] = str(dest)
    proc = subprocess.run(args)
    if proc.returncode >= 8:
        print(f"[build_rc] 失败：robocopy 退出码 {proc.returncode}")
        return 2
    print(f"[build_rc] 复制完成（robocopy 退出码 {proc.returncode}）")
    return 0


def run_verify(dest: Path) -> int:
    """列表模式比对（/L 不写盘）：输出为空 = 两侧零差异。"""
    args = _robocopy_base()
    args[args.index("")] = str(dest)
    args += ["/L", "/NJH", "/NJS", "/NS", "/NC"]
    proc = subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode >= 8:
        print(f"[build_rc] 校验命令失败：退出码 {proc.returncode}\n{proc.stdout}")
        return 2
    diffs = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if diffs:
        print(f"[build_rc] 校验发现 {len(diffs)} 处差异（前 20 条）：")
        for ln in diffs[:20]:
            print("  " + ln)
        print("[build_rc] 处置：修正源目录后重跑本脚本（禁止直接改 RC 目录）")
        return 3
    print("[build_rc] 校验通过：源与 RC 零差异（排除清单内项除外）")
    return 0


def main() -> int:
    if sys.platform != "win32":
        print("仅支持 Windows（robocopy）")
        return 4
    parser = argparse.ArgumentParser(description="一键生成 RC 交付目录")
    parser.add_argument("--dest", default=str(DEFAULT_DEST), help="目标目录（默认 E:\\RC1002）")
    parser.add_argument("--verify", action="store_true", help="仅校验差异，不复制")
    opts = parser.parse_args()
    dest = Path(opts.dest)

    if not SRC.is_dir():
        print(f"[build_rc] 源目录不存在: {SRC}")
        return 4
    dest.mkdir(parents=True, exist_ok=True)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if opts.verify:
        return run_verify(dest)
    rc = run_sync(dest)
    if rc != 0:
        return rc
    return run_verify(dest)


if __name__ == "__main__":
    sys.exit(main())
