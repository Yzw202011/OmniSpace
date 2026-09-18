# 本项目仅供学习使用，商业授权请+Q 3559331368
"""LoRA adapter 格式转换器（diffusers peft → ComfyUI LoraLoader）。

diffusers peft 训练出来的 LoRA 键名与 ComfyUI LoraLoader 期望的键名不同，
需要逐键映射。核心难点：双流块 img 侧 to_q/to_k/to_v 三个独立 LoRA
需要合并为 img_attn.qkv 单个 LoRA（A 沿 output dim 拼接，B 沿 input dim
拼接——和 base 模型的 QKV 融合方式一致）。

输出键名格式（ComfyUI Flux2 标准）：
  double_blocks.N.img_attn.qkv.weight 等 → LoRA 用 lora_A.weight / lora_B.weight 后缀
"""
from __future__ import annotations

from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

# ── 块前缀映射 ──
_BLOCK_MAP = [
    ("single_transformer_blocks.", "single_blocks."),
    ("transformer_blocks.", "double_blocks."),
]

# ── 双流块内部映射（LoRA base 键无尾部点号）──
_DOUBLE_MAP = [
    (".attn.to_out.0", ".img_attn.proj"),
    (".attn.norm_q", ".img_attn.norm.query_norm"),
    (".attn.norm_k", ".img_attn.norm.key_norm"),
    (".attn.to_add_out", ".txt_attn.proj"),
    (".attn.norm_added_q", ".txt_attn.norm.query_norm"),
    (".attn.norm_added_k", ".txt_attn.norm.key_norm"),
    (".ff.linear_in", ".img_mlp.0"),
    (".ff.linear_out", ".img_mlp.2"),
    (".ff_context.linear_in", ".txt_mlp.0"),
    (".ff_context.linear_out", ".txt_mlp.2"),
    (".norm1.linear", ".modulation.img.lin"),
    (".norm1_context.linear", ".modulation.txt.lin"),
]

# ── 单流块内部映射 ──
_SINGLE_MAP = [
    (".attn.to_qkv_mlp_proj", ".linear1"),
    (".attn.to_out", ".linear2"),
    (".attn.norm_q", ".attn.norm.query_norm"),
    (".attn.norm_k", ".attn.norm.key_norm"),
    (".norm.linear", ".modulation.lin"),
]

# ── 通用映射 ──
_GLOBAL_MAP = [
    ("x_embedder", "img_in"),
    ("context_embedder", "txt_in"),
    ("time_guidance_embed.timestep_embedder.linear_1.", "time_in.in_layer."),
    ("time_guidance_embed.timestep_embedder.linear_2.", "time_in.out_layer."),
    ("single_stream_modulation.linear.", "single_stream_modulation.lin."),
    ("double_stream_modulation_img.linear.", "double_stream_modulation_img.lin."),
    ("double_stream_modulation_txt.linear.", "double_stream_modulation_txt.lin."),
    ("norm_out.linear.", "final_layer.adaLN_modulation.1."),
    ("proj_out.", "final_layer.linear."),
]


def convert_key(key: str) -> str:
    """diffusers LoRA 基础名 → ComfyUI 模型键名（无 QKV 合并，仅重命名）。"""
    nk = key
    for old, new in _GLOBAL_MAP:
        nk = nk.replace(old, new)
    # 块前缀替换（先长后短），再据此判断块类型
    is_single = "single_blocks." in nk.replace("single_transformer_blocks.", "single_blocks.")
    for old, new in _BLOCK_MAP:
        nk = nk.replace(old, new)
    mappings = _SINGLE_MAP if is_single else _DOUBLE_MAP
    for old, new in mappings:
        nk = nk.replace(old, new)
    # 最终收尾
    nk = nk.replace("norm_out.linear.", "final_layer.adaLN_modulation.1.")
    nk = nk.replace("proj_out.", "final_layer.linear.")
    return nk


def convert_lora_adapter(src: str | Path, dst: str | Path) -> int:
    """peft LoRA adapter → ComfyUI LoraLoader 兼容格式。

    处理三类键：
    1. 双流块 to_q/to_k/to_v → 合并为 img_attn.qkv（A 沿 dim=0 拼接）
    2. 双流块 add_q/add_k/add_v → 合并为 txt_attn.qkv
    3. 其余键 → 重命名 + 保留 lora_A/lora_B 后缀
    """
    sd = load_file(str(src))
    out: dict[str, torch.Tensor] = {}

    # 收集 QKV 零散键
    qkv_a: dict[str, list[torch.Tensor]] = {}  # img 侧 A
    qkv_b: dict[str, list[torch.Tensor]] = {}  # img 侧 B
    qkv_txt_a: dict[str, list[torch.Tensor]] = {}
    qkv_txt_b: dict[str, list[torch.Tensor]] = {}

    for k, v in sd.items():
        clean = k
        for prefix in ("base_model.model.", "base_model."):
            if clean.startswith(prefix):
                clean = clean[len(prefix):]
                break

        if ".attn.to_q.lora_A." in clean:
            qkv_a.setdefault(clean.split(".attn.to_q.")[0], []).append(v)
            continue
        if ".attn.to_k.lora_A." in clean:
            qkv_a.setdefault(clean.split(".attn.to_k.")[0], []).append(v)
            continue
        if ".attn.to_v.lora_A." in clean:
            qkv_a.setdefault(clean.split(".attn.to_v.")[0], []).append(v)
            continue
        if ".attn.to_q.lora_B." in clean:
            qkv_b.setdefault(clean.split(".attn.to_q.")[0], []).append(v)
            continue
        if ".attn.to_k.lora_B." in clean:
            qkv_b.setdefault(clean.split(".attn.to_k.")[0], []).append(v)
            continue
        if ".attn.to_v.lora_B." in clean:
            qkv_b.setdefault(clean.split(".attn.to_v.")[0], []).append(v)
            continue
        if ".attn.add_q_proj.lora_A." in clean:
            qkv_txt_a.setdefault(clean.split(".attn.add_q_proj.")[0], []).append(v)
            continue
        if ".attn.add_k_proj.lora_A." in clean:
            qkv_txt_a.setdefault(clean.split(".attn.add_k_proj.")[0], []).append(v)
            continue
        if ".attn.add_v_proj.lora_A." in clean:
            qkv_txt_a.setdefault(clean.split(".attn.add_v_proj.")[0], []).append(v)
            continue
        if ".attn.add_q_proj.lora_B." in clean:
            qkv_txt_b.setdefault(clean.split(".attn.add_q_proj.")[0], []).append(v)
            continue
        if ".attn.add_k_proj.lora_B." in clean:
            qkv_txt_b.setdefault(clean.split(".attn.add_k_proj.")[0], []).append(v)
            continue
        if ".attn.add_v_proj.lora_B." in clean:
            qkv_txt_b.setdefault(clean.split(".attn.add_v_proj.")[0], []).append(v)
            continue

        # 非 QKV：重命名
        comfy_base = convert_key(clean.split(".lora_A.")[0].split(".lora_B.")[0])
        if ".lora_A." in clean:
            out[f"{comfy_base}.lora_A.weight"] = v
        elif ".lora_B." in clean:
            out[f"{comfy_base}.lora_B.weight"] = v
        else:
            # lora_dropout 等非权重键跳过
            pass

    # QKV 合并
    for base, parts in qkv_a.items():
        if len(parts) == 3:
            comfy_base = convert_key(base)
            out[f"{comfy_base}.img_attn.qkv.lora_A.weight"] = torch.cat(parts, dim=0)
    for base, parts in qkv_b.items():
        if len(parts) == 3:
            comfy_base = convert_key(base)
            out[f"{comfy_base}.img_attn.qkv.lora_B.weight"] = torch.cat(parts, dim=0)
    for base, parts in qkv_txt_a.items():
        if len(parts) == 3:
            comfy_base = convert_key(base)
            out[f"{comfy_base}.txt_attn.qkv.lora_A.weight"] = torch.cat(parts, dim=0)
    for base, parts in qkv_txt_b.items():
        if len(parts) == 3:
            comfy_base = convert_key(base)
            out[f"{comfy_base}.txt_attn.qkv.lora_B.weight"] = torch.cat(parts, dim=0)

    save_file(out, str(dst))
    return len(out)
