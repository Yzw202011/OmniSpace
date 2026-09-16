"""distributed 测试族夹具：内核存档目录隔离（卫生修复 2026-09-16）。

CuteMamenKernel 的 unmount / 内存预算 LRU 淘汰会向 ``pkg_dir``
自动存档 .CuteMamen 包；默认值是仓库内 tracked 的
``cutemamen_pkgs/``——测试一跑就把 git 工作区写脏（历史上
a/echo/x.CuteMamen 反复变脏即此机理）。本夹具强制本目录下所有
内核实例（含 CubeGPTKernel 子类，经 super() 传导）的存档进
tmp_path，仓库文件零写入。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_kernel_pkg_dir(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.cutemamen.kernel as kernel_mod

    orig_init = kernel_mod.CuteMamenKernel.__init__
    fixed_dir = str(tmp_path / "cutemamen_pkgs")  # type: ignore[operator]

    def _init(self: object, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("pkg_dir", fixed_dir)  # type: ignore[arg-type]
        orig_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(kernel_mod.CuteMamenKernel, "__init__", _init)
