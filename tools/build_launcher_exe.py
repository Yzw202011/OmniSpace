"""编译 OmniSpace 启动器 exe（bat 观感升级）。

用法：runtime/py310/python.exe tools/build_launcher_exe.py [--out <路径>]
产物默认：仓库根/启动OmniSpace-开发版.exe（09-02 改名防与发行包入口混淆；
make_dist 打包时会以交付名 启动OmniSpace.exe 落进包内）。
依赖：WinLibs gcc + windres（winget BrechtSanders.WinLibs.POSIX.UCRT）。
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "launcher"
GCC = (Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
       / "BrechtSanders.WinLibs.POSIX.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe"
       / "mingw64/bin")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "启动OmniSpace-开发版.exe"),
                    help="输出 exe 路径（默认仓库根 开发版名）")
    args = ap.parse_args()
    env = dict(os.environ, PATH=str(GCC) + os.pathsep + os.environ.get("PATH", ""))
    gcc = shutil.which("gcc", path=env.get("PATH"))
    windres = shutil.which("windres", path=env.get("PATH"))
    if not gcc or not windres:
        print("缺 WinLibs gcc/windres")
        return 1

    build = REPO / "_launcher_build"
    shutil.rmtree(build, ignore_errors=True)
    build.mkdir()
    r = subprocess.run([windres, str(LAUNCHER / "omnispace.rc"),
                        str(build / "res.o")], cwd=str(LAUNCHER),
                       capture_output=True, encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0:
        print("windres 失败:", (r.stdout + r.stderr)[-300:])
        return 1
    tmp_exe = build / "OmniSpace.exe"
    r = subprocess.run([gcc, "-O2", "-mwindows", "-s",
                        "-o", str(tmp_exe),
                        str(LAUNCHER / "omnispace_exe.c"), str(build / "res.o")],
                       capture_output=True, encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0:
        print("gcc 失败:", (r.stdout + r.stderr)[-400:])
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tmp_exe, out)
    shutil.rmtree(build, ignore_errors=True)
    print(f"OK {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
