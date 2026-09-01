#!/usr/bin/env python3
"""一键发行流（2026-09-02 双开事故后落地）：make_dist → 复制副本 → protect_build。

大白话：出发行包从此只跑这一个脚本——先白名单出源码包，再整份复制一份
就地加壳编译，全程零手工步骤。手工拼装正是 09-01 事故的温床：暂存目录
新旧混装，出了个没有单实例守卫的包，构建号却盖着最新提交；验收又被
工作目录泄漏骗过去，跑的其实是仓库源码。protect_build 内置产物哨兵，
守卫没真实编进二进制直接拒出厂。

用法：
  runtime/py310/python.exe tools/make_release.py             # 出包到 D:/ccd
  runtime/py310/python.exe tools/make_release.py --pubkey <hex>   # 正式发行（激活门禁）
产出：
  <out>/OmniSpace-<版本>-g<哈希>             源码包（可移植性验收用）
  <out>/OmniSpace-<版本>-g<哈希>-protected   加壳发行包（交付物）
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="D:/ccd", help="输出根目录（默认 D:/ccd，"
                    "发行产物不进开发目录）")
    ap.add_argument("--jobs", type=int, default=2,
                    help="Cython 并行编译数（默认 2 控内存，开发机在跑时勿调高）")
    ap.add_argument("--pubkey", default="",
                    help="发行公钥 hex（透传 make_dist；注入后激活门禁生效）")
    args = ap.parse_args()
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    # ① 白名单出源码包（make_dist 自带 dest 清场 rmtree + 包内三查自检）
    print("== ① make_dist 出源码包 ==", flush=True)
    cmd = [PY, str(REPO / "tools" / "make_dist.py"), "--out", str(out_root)]
    if args.pubkey:
        cmd += ["--pubkey", args.pubkey]
    r = subprocess.run(cmd, cwd=str(REPO))
    if r.returncode != 0:
        print("❌ make_dist 失败")
        return 1

    # ② 找到刚出的源码包目录（取最新非 protected 的 OmniSpace-*），
    # robocopy 镜像复制为 -protected 副本。/PURGE 先清走目标侧多余文件
    # （上一轮的 .pyd/.c/build 残留）——陈旧中间产物复用是 09-01 事故类
    # 根源，宁可整拷也不用增量；robocopy 原生扛长路径（ComfyUI torch 许可目录）
    cands = [d for d in out_root.glob("OmniSpace-*")
             if d.is_dir() and not d.name.endswith("-protected")]
    if not cands:
        print("❌ 找不到 make_dist 产物目录")
        return 1
    src = max(cands, key=lambda p: p.stat().st_mtime)
    dest = src.with_name(src.name + "-protected")
    print(f"== ② 复制 {src.name} → {dest.name} ==", flush=True)
    rc = subprocess.run(["robocopy", str(src), str(dest), "/E", "/PURGE",
                         "/R:1", "/W:1", "/NFL", "/NDL", "/NP"]).returncode
    if rc >= 8:
        print(f"❌ 复制失败（robocopy rc={rc}）")
        return 1

    # ③ 就地加壳编译（protect_build 内含产物哨兵：单实例守卫等关键标记
    # 未编进二进制 → 拒收不出厂）
    print("== ③ protect_build 加壳编译 ==", flush=True)
    r = subprocess.run([PY, str(REPO / "tools" / "protect_build.py"),
                        str(dest), "--jobs", str(args.jobs)], cwd=str(REPO))
    if r.returncode != 0:
        print("❌ protect_build 失败")
        return 1

    print(f"\n发行完成：\n  源码包 {src}\n  加壳包 {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
