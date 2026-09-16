#!/usr/bin/env python3
"""发行包代码保护器 v2（P3）：Cython 编译整树 → .pyd 真二进制。

大白话：把包里的自有 Python 代码（src/launcher 可导入部分/skills）全部
编译成 .pyd 二进制扩展——源码从包里消失，变成机器码。这是比字节码混淆更
硬的保护（反编译工具面对的是机器码不是可还原字节码），且免费、无单文件
大小限制。混淆完必须重跑金母版 verify，指纹一致才算保护成功。

用法：
  runtime/py310/python.exe tools/protect_build.py <包目录>   # 就地编译

依赖：build_tools/ 的 cython+setuptools；WinLibs gcc（winget 包
  BrechtSanders.WinLibs.POSIX.UCRT，gcc bin 需在 PATH）。

关键约束（实测坑）：
  - 被当「脚本」直接跑的文件（boot.py/stop.py/make_shortcut.py）不能编译，
    保留 .py；其余全部 import 使用，可安全编译。
  - MinGW 下必须 -DMS_WIN64（pyconfig.h 只在 _MSC_VER 分支定义它，
    否则 SIZEOF_VOID_P 缺失 → Cython sizeof 校验枚举除零报错）。
  - E 盘实例在跑时控制并发（nthreads 默认 2，吸取进程炸弹夜内存压顶教训）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILD_TOOLS = REPO / "build_tools"
sys.path.insert(0, str(REPO / "tools"))

# 编译目标（相对包根）；src 扁平化（2026-09-15）后后端真源=src/
TARGETS = ["src", "launcher", "skills"]
# 被当脚本运行（python xxx.py 直启）的文件——编译成 .pyd 就没人能启动它了
SCRIPT_RUN = {"launcher/boot.py", "launcher/stop.py", "launcher/make_shortcut.py"}
GCC_FLAGS = ["-DMS_WIN64", "-O1"]

# WinLibs gcc 位置（winget 安装默认路径，找不到则依赖 PATH）
WINLIBS_GCC = (Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
               / "BrechtSanders.WinLibs.POSIX.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe"
               / "mingw64/bin")


def gcc_ready() -> tuple[bool, dict]:
    env = dict(os.environ)
    if WINLIBS_GCC.is_dir():
        env["PATH"] = str(WINLIBS_GCC) + os.pathsep + env.get("PATH", "")
    gcc_exe = shutil.which("gcc", path=env.get("PATH"))
    if gcc_exe is None:
        return False, env
    print(f"  gcc: {gcc_exe}")
    return True, env


def _module_name(rel: Path) -> str:
    """源相对路径 → 扩展模块名。__init__.py 归一为包名（src/__init__.py
    → backend.__init__：实验实证该命名产物正确落于 src/__init__..pyd）。"""
    parts = list(rel.with_suffix("").parts)
    return ".".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pkg", help="要保护的包目录（就地编译，先自行复制副本）")
    ap.add_argument("--jobs", type=int, default=2, help="并行编译数（默认 2，控内存）")
    args = ap.parse_args()
    pkg = Path(args.pkg).resolve()
    if not pkg.is_dir():
        raise SystemExit(f"包目录不存在：{pkg}")
    if not BUILD_TOOLS.is_dir():
        raise SystemExit("缺 build_tools/（先 pip --target 安装 cython setuptools）")

    ok_env = gcc_ready()
    if not ok_env[0]:
        raise SystemExit("gcc 不可用（检查 WinLibs 安装/PATH）")
    _, env = ok_env

    files = [p for t in TARGETS for p in (pkg / t).rglob("*.py")
             if p.relative_to(pkg).as_posix() not in SCRIPT_RUN
             and "_extracted" not in p.relative_to(pkg).parts]  # 第三方上游技能解包源码，保持原样
    print(f"保护目标：{pkg.name}（{len(files)} 个可编译 + {len(SCRIPT_RUN)} 个脚本保留）")

    code = """import sys, os
sys.path.insert(0, r'{tools}')
os.chdir(r'{pkg}')
from setuptools import Extension, setup
from Cython.Build import cythonize

exts = [Extension(m, [f], extra_compile_args={flags!r})
        for m, f in {mods!r}]

DIRECTIVES = dict(
    language_level='3',
    # FastAPI 兼容三件套（缺一致命）：
    # annotation_typing=False —— 注解不当 C 类型（否则 `x: dict = Body(...)`
    #   会对默认值做类型检查，路由注册即 TypeError: Expected dict, got Body）
    # binding=True —— 编译函数保持 Python 函数行为（FastAPI 依赖注入
    #   需要运行时反射签名）
    # embedsignature=True —— 签名嵌入 docstring 供 inspect
    annotation_typing=False,
    binding=True,
    embedsignature=True,
)

setup(name='cy_protect',
      ext_modules=cythonize(exts, nthreads={jobs}, compiler_directives=DIRECTIVES),
      script_args=['build_ext', '--compiler=mingw32'])
print('BUILD_OK', len(exts))
""".format(tools=BUILD_TOOLS, pkg=pkg, flags=GCC_FLAGS,
           mods=[(_module_name(p.relative_to(pkg)), str(p))
                 for p in files],
           jobs=args.jobs)

    t0 = time.time()
    r = subprocess.run([sys.executable, "-c", code], cwd=str(pkg),
                       capture_output=True, encoding="utf-8", errors="replace",
                       env=env)
    tail = (r.stdout + r.stderr).strip().splitlines()
    for line in tail[-6:]:
        print("  [cython]", line)
    if r.returncode != 0 or "BUILD_OK" not in r.stdout:
        raise SystemExit(f"编译失败（rc={r.returncode}）")

    # 收尾：按编译清单精确落位——每个源 .py 对应 build/lib 下的同名 .pyd，
    # 拷回源文件旁（保留 tag 后缀，导入优先级天然高于 .py），再删源 .py。
    # 注：不做后缀名反推（tag 文件名数学不可靠），全部走真实清单。
    # 2026-09-02 加固：只认本轮 build/lib 的产出——源码旁若有上轮遗留 .pyd
    # （暂存复用/中断残留）一律判陈旧产物拒收，绝不静默顶包出厂。
    libroot = next((pkg / "build").glob("lib.*"), None)
    if libroot is None:
        raise SystemExit("找不到 build/lib.* 输出目录")
    moved = 0
    missing = []
    stale = []
    for f in files:
        stem = f.name[:-3]  # 去掉 .py
        built = [p for p in libroot.rglob(stem + ".*.pyd")
                 if p.parent.name == f.parent.name]
        if not built:
            if list(f.parent.glob(stem + "*.pyd")):
                stale.append(f.relative_to(pkg).as_posix())
            else:
                missing.append(f.relative_to(pkg).as_posix())
            continue
        shutil.copy2(built[0], f.parent / built[0].name)
        f.unlink()  # 删源
        moved += 1
    shutil.rmtree(pkg / "build", ignore_errors=True)
    if missing or stale:
        if missing:
            print(f"❌ {len(missing)} 个模块本轮没有产出 .pyd：{missing[:8]}")
        if stale:
            print(f"❌ {len(stale)} 个模块只剩陈旧 .pyd（本轮未编译出，拒收"
                  f"防旧代码混包）：{stale[:8]}")
        return 1

    # 产物哨兵 v2（2026-09-02 当晚两次复盘修正）。初版按「.pyd 字节含
    # single_instance」验身——实测 Cython 3.3 对函数内相对导入的模块名不做
    # 明文存储：main.c 含 22 处引用、.pyd 0 处、功能实测照样拒双开，好包
    # 被误杀。v2 改两道真验，防的仍是同一件事：旧源码/陈旧产物混进包：
    #   ① 编译期：cythonize 生成的 .c 必须含守卫标识（源码没守卫，.c 就没有
    #      这些标识符）——在 .c 清理之前执行
    #   ② 功能期：包内 python 双进程互斥实测——真锁真拒才算守卫入包
    SENTINEL_C_MARKERS = {
        "src/main": ["single_instance", "style_seed", "license_gate"],
        "src/single_instance": ["singleinstance", "CreateMutexW"],
    }
    for mod_rel, markers in SENTINEL_C_MARKERS.items():
        cs = list((pkg / mod_rel).parent.glob(Path(mod_rel).name + ".c"))
        if not cs:
            print(f"❌ 哨兵失败：{mod_rel}.c 不存在（编译产物异常）")
            return 1
        text = cs[0].read_text("utf-8", errors="replace")
        for m in markers:
            if m not in text:
                print(f"❌ 哨兵未命中：{mod_rel}.c 缺「{m}」——"
                      "源码八成是旧版（无此机制），拒出厂")
                return 1
    print("✅ 编译期哨兵命中（守卫标识存在于生成的 C 代码）")

    # 残余 .c（cythonize 中间产物）与清单刷新
    for cc in [p for t in TARGETS for p in (pkg / t).rglob("*.c")]:
        cc.unlink()

    # 功能哨兵：用包内自己的 runtime 起两个进程——A 持锁，B 试锁必须被拒。
    # 不依赖任何字符串编码假设，直接验证「这台机器上第二实例必被拦」。
    pkg_python = pkg / "runtime" / "py310" / "python.exe"
    if not pkg_python.exists():
        print("❌ 功能哨兵失败：包内缺 runtime/py310/python.exe")
        return 1
    _HOLD = ("import sys, time; sys.path.insert(0, '.'); "
             "from src import single_instance as si; "
             "assert si.acquire(), 'holder 拿锁失败'; "
             "print('HELD', flush=True); time.sleep(120)")
    _PROBE = ("import sys; sys.path.insert(0, '.'); "
              "from src import single_instance as si; "
              "print('PROBE', si.acquire())")
    # 预探测：开发机上若正有真实例持锁（互斥体是全机唯一），功能哨兵无法
    # 自建持锁态——降级跳过（编译期哨兵已把关源码新旧），不算失败
    pre = subprocess.run([str(pkg_python), "-c", _PROBE], cwd=str(pkg),
                         capture_output=True, text=True, timeout=30,
                         encoding="utf-8", errors="replace")
    if "PROBE False" in (pre.stdout or ""):
        print("⚠️ 功能哨兵降级：本机已有 OmniSpace 实例持锁（编译期哨兵已过，"
              "功能实测留待静默机器/验收环境补做）")
    else:
        holder = subprocess.Popen([str(pkg_python), "-c", _HOLD], cwd=str(pkg),
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", errors="replace")
        try:
            first = holder.stdout.readline().strip() if holder.stdout else ""
            if first != "HELD":
                print(f"❌ 功能哨兵失败：持锁进程未就绪（输出={first!r}）")
                return 1
            probe = subprocess.run([str(pkg_python), "-c", _PROBE], cwd=str(pkg),
                                   capture_output=True, text=True, timeout=30,
                                   encoding="utf-8", errors="replace")
            out = (probe.stdout or "").strip()
            if "PROBE False" not in out:
                print(f"❌ 功能哨兵失败：第二个进程未被互斥体拦截"
                      f"（输出={out!r}）——单实例守卫未生效，拒出厂")
                return 1
            print("✅ 功能哨兵命中（包内双进程互斥实测：第二实例被拒）")
        finally:
            holder.terminate()
            try:
                holder.wait(timeout=10)
            except Exception:  # noqa: BLE001
                holder.kill()

    # 刷新 dist_manifest.json（哈希全变了）
    old = pkg / "dist_manifest.json"
    build_id = "unknown"
    if old.exists():
        try:
            build_id = json.loads(old.read_text("utf-8")).get("build_id", build_id)
        except Exception:  # noqa: BLE001
            pass
    from make_dist import write_manifest  # noqa: E402
    n = write_manifest(pkg, build_id + "+cython")

    # 校验：目标区内不得残留 .py（脚本白名单与 _extracted 第三方上游源码除外
    # ——后者按「保持原样」原则刻意不编译，见 files 列表的同名排除）
    leftover = [p.relative_to(pkg).as_posix() for p in
                [f for t in TARGETS for f in (pkg / t).rglob("*.py")]
                if p.relative_to(pkg).as_posix() not in SCRIPT_RUN
                and "_extracted" not in p.parts]
    print(f"\n编译完成（{time.time() - t0:.0f}s）：{moved} 个 .pyd，"
          f"清单 {n} 条已刷新")
    if leftover:
        print(f"❌ {len(leftover)} 个源码残留：{leftover[:8]}")
        return 1
    print("✅ 源码全灭，包内只剩二进制。下一步：清 data/ 点火跑 golden_master verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
