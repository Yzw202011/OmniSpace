# -*- coding: utf-8 -*-
"""修复 musubi z-image 训练器：LoRA 网络创建后底座被意外解冻的问题。

networks/lora.py:780/832 的 requires_grad_(True) 会级联到被包裹的
org_module（底座 Linear），导致整个 6B 底座进入训练态（优化器状态
爆显存 + 底座权重可训练驻留 CPU 等连锁）。本补丁在优化器创建后
重新声明冻结：仅 lora_up/lora_down 参数可训练。

(upstream ostris/musubi-tuner, OmniSpace 2026-09-15)
"""
from pathlib import Path

p = Path("E:/OmniSpace/tools/musubi-tuner/src/musubi_tuner/zimage_train.py")
s = p.read_text(encoding="utf-8")

anchor = "        lr_scheduler = self.get_lr_scheduler(args, optimizer, accelerator.num_processes)\n"
fix = anchor + (
    "        # PATCH (OmniSpace 2026-09-15): lora.py 的 requires_grad_(True) 会\n"
    "        # 级联解冻被包裹的底座 Linear（org_module 是 LoRA 模块的注册子\n"
    "        # 模块），导致 6B 底座整座进入训练态——优化器状态爆显存、底座\n"
    "        # 权重以可训练态驻留 CPU。此处重新声明冻结：仅 LoRA 参数可训练。\n"
    "        for _n, _p in transformer.named_parameters():\n"
    "            _p.requires_grad_((\"lora_up\" in _n) or (\"lora_down\" in _n))\n"
)
assert s.count(anchor) == 1, s.count(anchor)
s = s.replace(anchor, fix, 1)
p.write_text(s, encoding="utf-8", newline="")

import py_compile
py_compile.compile(str(p), doraise=True)
print("freeze patch applied & compiles")
