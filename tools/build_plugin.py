# 本项目仅供学习使用，商业授权请+Q 3559331368
"""OmniSpace 插件独立打包工具（P1 根修 2026-09-22）。

大白话：让插件开发者不需要 clone 整个 OmniSpace 仓库就能打包 .CuteMamen。
用法（在 OmniSpace 仓库内跑，但逻辑独立，后续可拆成独立 pip 包）：

    runtime/py312/python.exe tools/build_plugin.py <plugin.py> <output.CuteMamen> [--skills chat,novel]

    runtime/py312/python.exe tools/build_plugin.py examples/hello-plugin/plugin.py hello.CuteMamen --skills chat

设计：复用 src/cutemamen/pkg.py 的 save_pkg（单一真源），
本工具只做 CLI 壳——解析参数→动态导入插件→调 save_pkg→报告产物。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.cutemamen.pkg import save_pkg  # noqa: E402

VALID_FEATURES = {"chat", "novel", "comic", "manga"}


def load_plugin_class(py_path: Path):
    """从 .py 文件动态加载 ExpertPlugin 子类（取发现的第一个）。"""
    # 先注入 omnispace.plugin 垫片（必须在 exec_module 之前）
    import types

    from src.cutemamen.plugin import ExpertPlugin
    if "omnispace" not in sys.modules:
        omni = types.ModuleType("omnispace")
        omni.plugin = sys.modules["src.cutemamen.plugin"]
        sys.modules["omnispace"] = omni
        sys.modules["omnispace.plugin"] = sys.modules["src.cutemamen.plugin"]

    spec = importlib.util.spec_from_file_location("plugin_module", py_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"❌ 无法加载 {py_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 找 ExpertPlugin 子类
    for name in dir(mod):
        obj = getattr(mod, name)
        if (isinstance(obj, type) and issubclass(obj, ExpertPlugin)
                and obj is not ExpertPlugin):
            return obj
    raise SystemExit(f"❌ {py_path} 中未找到 ExpertPlugin 子类")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="OmniSpace 插件打包工具（不需要 clone 仓库→后续拆独立包）")
    ap.add_argument("source", help="插件源码 .py 文件路径")
    ap.add_argument("output", help="输出 .CuteMamen 文件路径")
    ap.add_argument("--skills", default="", help="技能声明（逗号分隔：chat,novel,comic）")
    ap.add_argument("--name", default="", help="插件名（缺省从类名推断）")
    ap.add_argument("--capability", default="", help="能力描述")
    args = ap.parse_args()

    src_path = Path(args.source)
    if not src_path.is_file():
        print(f"❌ 源文件不存在: {src_path}")
        return 1

    out_path = Path(args.output)
    if not out_path.suffix.lower() == ".cutemamen":
        out_path = out_path.with_suffix(".CuteMamen")

    # 解析技能
    skills = [s.strip() for s in args.skills.split(",") if s.strip()]
    invalid = [s for s in skills if s not in VALID_FEATURES]
    if invalid:
        print(f"❌ 无效技能: {invalid}（可选: {sorted(VALID_FEATURES)}）")
        return 1

    # 加载插件类
    cls = load_plugin_class(src_path)
    name = args.name or cls.__name__.replace("Plugin", "").lower()

    # 实例化
    try:
        instance = cls(name=name)
    except TypeError:
        instance = cls()

    # 读源码
    source_text = src_path.read_text(encoding="utf-8")
    if len(source_text.encode("utf-8")) > 256 * 1024:
        print("❌ 源码超过 256KB 上限")
        return 1

    # 打包
    kwargs = {"source_py": source_text}
    if skills:
        kwargs["skills"] = skills
    if args.capability:
        kwargs["capability"] = args.capability

    save_pkg(instance, str(out_path), **kwargs)

    # 验证产物
    size = out_path.stat().st_size
    print(f"✅ 打包成功: {out_path} ({size / 1024:.1f}KB)")
    print(f"   插件名: {name}")
    print(f"   技能: {skills or '（无——不会出现在前端技能面板）'}")
    print(f"\n   导入方法: 设置 → 插件 → 导入 → 选 {out_path.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
