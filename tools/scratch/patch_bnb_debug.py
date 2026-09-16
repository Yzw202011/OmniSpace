# -*- coding: utf-8 -*-
"""临时插桩：bnb update_step 打印 CPU 可训练参数（诊断后移除）。"""
from pathlib import Path

p = Path("E:/OmniSpace/tools/musubi-tuner/venv/lib/site-packages/bitsandbytes/optim/optimizer.py")
s = p.read_text(encoding="utf-8")

old = (
    "    def update_step(self, group, p, gindex, pindex):\n"
    "        # avoid update error from non-contiguous memory layout\n"
    "        p.data = p.data.contiguous()\n"
    "        p.grad = p.grad.contiguous()\n"
)
new = (
    "    def update_step(self, group, p, gindex, pindex):\n"
    "        # avoid update error from non-contiguous memory layout\n"
    "        p.data = p.data.contiguous()\n"
    '        if p.device.type == "cpu":\n'
    '            print("[BNB-DBG] cpu-param", tuple(p.shape), p.numel())\n'
    "        p.grad = p.grad.contiguous()\n"
)
n = s.count(old)
print("occurrences:", n)
assert n >= 1
s = s.replace(old, new)
p.write_text(s, encoding="utf-8", newline="")

import py_compile
py_compile.compile(str(p), doraise=True)
print("instrumented & compiles")
