# 本项目仅供学习使用，商业授权请+Q 3559331368
"""W3-C Phase 2（2026-09-18）：klein-4b diffusers → ComfyUI 格式转换。

纯 CPU 权重键重映射+QKV 三合一（无 GPU 需求）：
- 150 条简单重命名（linear→lin / x_embedder→img_in / transformer_blocks
  →double_blocks / single_transformer_blocks→single_blocks / attn 层名）
- 15 条 QKV 合并（to_q+to_k+to_v → img_attn.qkv 沿 dim=0 拼接）
- 输出单文件 safetensors 落 ComfyUI diffusion_models/

用法：runtime/py312/python.exe tools/convert_klein4b_to_comfy.py
"""
from __future__ import annotations

from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "models/paint/flux2-klein-4b/transformer/diffusion_pytorch_model.safetensors"
DST = ROOT / "tools/ComfyUI_windows_portable/ComfyUI/models/diffusion_models/flux-2-klein-4b.safetensors"


def map_key(k: str) -> str:
    """diffusers Flux2 → comfy Flux2 键名映射（非 QKV 部分）。"""
    nk = k
    nk = nk.replace(".linear.", ".lin.")
    nk = nk.replace("x_embedder", "img_in")
    nk = nk.replace("transformer_blocks.", "double_blocks.")
    nk = nk.replace("single_transformer_blocks.", "single_blocks.")
    # 双流块内部（diffusers → comfy）
    nk = nk.replace(".attn.to_out.0.", ".img_attn.proj.")
    nk = nk.replace(".attn.add_q_proj.", ".img_attn.qkv.")  # context 侧
    nk = nk.replace(".attn.add_k_proj.", ".img_attn.qkv.")
    nk = nk.replace(".attn.add_v_proj.", ".img_attn.qkv.")
    nk = nk.replace(".attn.to_add_out.", ".img_attn.proj.")
    # FF 层
    nk = nk.replace(".ff.linear_in.", ".img_mlp.0.")
    nk = nk.replace(".ff.linear_out.", ".img_mlp.2.")
    nk = nk.replace(".ff_context.linear_in.", ".txt_mlp.0.")
    nk = nk.replace(".ff_context.linear_out.", ".txt_mlp.2.")
    # norm
    nk = nk.replace(".norm1.linear.", ".modulation.lin.")
    nk = nk.replace(".norm1_context.linear.", ".modulation.lin.")
    return nk


def convert() -> None:
    print(f"读入: {SRC}")
    sd = load_file(str(SRC))
    out: dict[str, torch.Tensor] = {}
    qkv_buffer: dict[str, list[torch.Tensor]] = {}

    for k, v in sd.items():
        # QKV 三合一（双流块的 to_q/to_k/to_v → img_attn.qkv）
        # 单流块的 to_qkv_mlp_proj 已是融合形态，直通
        if ".attn.to_q." in k:
            base = k.replace(".attn.to_q.weight", "")
            qkv_buffer.setdefault(base, []).append(("q", v))
            continue
        if ".attn.to_k." in k:
            base = k.replace(".attn.to_k.weight", "")
            qkv_buffer.setdefault(base, []).append(("k", v))
            continue
        if ".attn.to_v." in k:
            base = k.replace(".attn.to_v.weight", "")
            qkv_buffer.setdefault(base, []).append(("v", v))
            continue

        nk = map_key(k)
        out[nk] = v

    # QKV 合并
    for base, parts in qkv_buffer.items():
        order = {p[0]: p[1] for p in parts}
        if not all(x in order for x in "qkv"):
            print(f"  ⚠️ {base} 缺 Q/K/V，跳过")
            continue
        fused = torch.cat([order["q"], order["k"], order["v"]], dim=0)
        comfy_base = map_key(base + ".dummy")
        comfy_base = comfy_base.replace(".dummy", "")
        comfy_key = f"{comfy_base}.img_attn.qkv.weight"
        out[comfy_key] = fused

    save_file(out, str(DST))
    size_gb = DST.stat().st_size / 1024**3
    print(f"输出: {DST} ({size_gb:.2f} GB, {len(out)} 键)")
    print("✓ 转换完成——ComfyUI diffusion_models/ 下已就位")


if __name__ == "__main__":
    convert()
