# -*- coding: utf-8 -*-
"""在 zimage_train.py 的优化器 prepare 之后插桩：列出 CPU 上的可训练参数名。"""
from pathlib import Path

p = Path("E:/OmniSpace/tools/musubi-tuner/src/musubi_tuner/zimage_train.py")
s = p.read_text(encoding="utf-8")

anchor = "        optimizer, train_dataloader, lr_scheduler = accelerator.prepare(optimizer, train_dataloader, lr_scheduler)\n"
probe = (
    "        # OMNISPACE DEBUG: locate CPU trainable params\n"
    "        for _n, _p in transformer.named_parameters():\n"
    "            if _p.requires_grad and _p.device.type == \"cpu\":\n"
    "                accelerator.print(\"[Z2-DBG] CPU trainable:\", _n, tuple(_p.shape))\n"
    "        for _gi, _g in enumerate(optimizer.param_groups):\n"
    "            for _p in _g[\"params\"]:\n"
    "                if _p.device.type == \"cpu\":\n"
    "                    accelerator.print(\"[Z2-DBG] optimizer CPU param: shape\", tuple(_p.shape), \"group\", _gi)\n"
)
assert anchor in s, "anchor missing"
s = s.replace(anchor, anchor + probe, 1)
p.write_text(s, encoding="utf-8", newline="")

import py_compile
py_compile.compile(str(p), doraise=True)
print("probe inserted & compiles")
