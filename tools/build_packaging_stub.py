"""编译 启动打包台.exe——packaging_console 的 C 桩入口（同 build_launcher_exe 模式）。

用法：runtime/py310/python.exe tools/build_packaging_stub.py [--out <路径>]
产物默认：仓库根/启动打包台.exe（双击=pythonw -m packaging_console 开桌面窗口；
参数透传，如「启动打包台.exe --no-window」=纯服务调试）。
图标：复用 launcher/omnispace.ico（星门，09-02 定稿）；依赖 WinLibs gcc+windres。
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "packaging_console"
ICON = REPO / "launcher" / "omnispace.ico"
GCC = (Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
       / "BrechtSanders.WinLibs.POSIX.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe"
       / "mingw64/bin")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "启动打包台.exe"),
                    help="输出 exe 路径（默认仓库根 启动打包台.exe）")
    args = ap.parse_args()
    env = dict(os.environ, PATH=str(GCC) + os.pathsep + os.environ.get("PATH", ""))
    gcc = shutil.which("gcc", path=env.get("PATH"))
    windres = shutil.which("windres", path=env.get("PATH"))
    if not gcc or not windres:
        print("缺 WinLibs gcc/windres")
        return 1
    if not ICON.exists():
        print(f"缺图标 {ICON}")
        return 1

    # rc 里引用的是相对名，把 ico 拷进构建目录再以它为 cwd 编资源
    build = REPO / "_packaging_stub_build"
    shutil.rmtree(build, ignore_errors=True)
    build.mkdir()
    shutil.copy2(ICON, build / "omnispace.ico")
    r = subprocess.run([windres, str(PKG / "packaging_stub.rc"),
                        str(build / "res.o")], cwd=str(build),
                       capture_output=True, encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0:
        print("windres 失败:", (r.stdout + r.stderr)[-300:])
        return 1
    r = subprocess.run([gcc, "-O2", "-mwindows", "-s",
                        "-o", str(build / "OmniSpacePack.exe"),
                        str(PKG / "packaging_stub.c"), str(build / "res.o")],
                       capture_output=True, encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0:
        print("gcc 失败:", (r.stdout + r.stderr)[-400:])
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(build / "OmniSpacePack.exe", out)
    shutil.rmtree(build, ignore_errors=True)
    print(f"OK {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
