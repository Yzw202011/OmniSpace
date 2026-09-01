"""Cython 翻译级预扫：只做 Python→C 翻译检查，不调 gcc，快速收齐全部编译错误。

用法：runtime/py310/python.exe tools/cython_scan.py <包目录>
"""
import sys
from pathlib import Path

sys.path.insert(0, r"E:\OmniSpace\build_tools")

from Cython.Compiler.Main import compile as cy_compile  # noqa: E402
from Cython.Compiler.Options import CompilationOptions  # noqa: E402

TARGETS = ["backend", "launcher", "skills"]
SCRIPT_RUN = {"launcher/boot.py", "launcher/stop.py", "launcher/make_shortcut.py"}

pkg = Path(sys.argv[1]).resolve()
files = [p for t in TARGETS for p in (pkg / t).rglob("*.py")
         if p.relative_to(pkg).as_posix() not in SCRIPT_RUN
         and "_extracted" not in p.relative_to(pkg).parts]

opts = CompilationOptions(language_level=3, compiler_directives={"infer_types": False})
bad = []
for _i, f in enumerate(files, 1):
    try:
        r = cy_compile(str(f), opts)
        if r.num_errors:
            bad.append(str(f.relative_to(pkg)))
    except Exception as exc:  # noqa: BLE001
        bad.append(f"{f.relative_to(pkg)} (EXC {type(exc).__name__})")

print(f"扫描 {len(files)} 个文件，翻译失败 {len(bad)} 个：")
for b in bad:
    print("  ✗", b)
sys.exit(1 if bad else 0)
