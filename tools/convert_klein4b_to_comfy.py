# 本项目仅供学习使用，商业授权请+Q 3559331368
"""W3-C Phase 2（2026-09-18 二轮校准）：klein-4b diffusers → ComfyUI 格式。

一轮产物被 ComfyUI 拒检（"Could not detect model type"）——根因：
双流块内部注意力层名映射不完整（检测条件需要 img_attn.norm.key_norm
等 comfy 专属命名，一轮只做了外层前缀替换）。

二轮修正：完整映射表（双流 16 条+单流 4 条规则），涵盖 img_attn/
txt_attn 分离、norm 拆分（norm_q→query_norm/norm_k→key_norm）、
modulation 分 img/txt 路径。
"""
from __future__ import annotations

from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "models/paint/flux2-klein-4b/transformer/diffusion_pytorch_model.safetensors"
DST = ROOT / "tools/ComfyUI_windows_portable/ComfyUI/models/diffusion_models/flux-2-klein-4b.safetensors"


def map_key(k: str) -> str:
    """diffusers Flux2 klein-4b → comfy Flux2 完整键名映射。

    三轮校准（2026-09-18）：对照 klein-9b（ComfyUI 已工作）顶层键，
    补齐三个关键映射——time_in/txt_in/final_layer。
    """
    nk = k

    # ── 外层组件（三轮新增：对照 klein-9b 实际键名）──
    nk = nk.replace("x_embedder", "img_in")
    # klein-9b 用 txt_in（非 vector_in！）——text embedding 投影
    nk = nk.replace("context_embedder", "txt_in")
    # 时间嵌入：diffusers time_guidance_embed.timestep_embedder.linear_N → comfy time_in
    nk = nk.replace("time_guidance_embed.timestep_embedder.linear_1.", "time_in.in_layer.")
    nk = nk.replace("time_guidance_embed.timestep_embedder.linear_2.", "time_in.out_layer.")
    nk = nk.replace("single_stream_modulation.linear.", "single_stream_modulation.lin.")
    nk = nk.replace("double_stream_modulation_img.linear.", "double_stream_modulation_img.lin.")
    nk = nk.replace("double_stream_modulation_txt.linear.", "double_stream_modulation_txt.lin.")

    # ── 块前缀 ──
    nk = nk.replace("transformer_blocks.", "double_blocks.")
    nk = nk.replace("single_transformer_blocks.", "single_blocks.")

    # ── 双流块内部（核心校准区）──
    # img 侧（self-attention on image tokens）
    nk = nk.replace(".attn.to_out.0.", ".img_attn.proj.")
    nk = nk.replace(".attn.norm_q.", ".img_attn.norm.query_norm.")
    nk = nk.replace(".attn.norm_k.", ".img_attn.norm.key_norm.")
    # txt 侧（cross-attention on text tokens）
    nk = nk.replace(".attn.to_add_out.", ".txt_attn.proj.")
    nk = nk.replace(".attn.norm_added_q.", ".txt_attn.norm.query_norm.")
    nk = nk.replace(".attn.norm_added_k.", ".txt_attn.norm.key_norm.")
    # add_q/k/v → txt_attn.qkv（QKV 三合一由 convert() 处理）
    # FF 层
    nk = nk.replace(".ff.linear_in.", ".img_mlp.0.")
    nk = nk.replace(".ff.linear_out.", ".img_mlp.2.")
    nk = nk.replace(".ff_context.linear_in.", ".txt_mlp.0.")
    nk = nk.replace(".ff_context.linear_out.", ".txt_mlp.2.")
    # modulation（分 img/txt 路径）
    nk = nk.replace(".norm1.linear.", ".modulation.lin.img.")
    nk = nk.replace(".norm1_context.linear.", ".modulation.lin.txt.")

    # ── 单流块内部 ──
    # to_qkv_mlp_proj → linear1（已融合，直通但需改名）
    nk = nk.replace(".attn.to_qkv_mlp_proj.", ".linear1.")
    nk = nk.replace(".attn.to_out.", ".linear2.")  # 无 .0
    nk = nk.replace(".attn.norm_q.", ".norm.query_norm.")  # 复用（但单流块无 img_attn 前缀）
    nk = nk.replace(".attn.norm_k.", ".norm.key_norm.")
    nk = nk.replace(".norm.linear.", ".modulation.lin.")

    # 输出层（三轮：norm_out.linear → final_layer.adaLN_modulation.1 / proj_out → final_layer.linear）
    nk = nk.replace("norm_out.linear.", "final_layer.adaLN_modulation.1.")
    nk = nk.replace("proj_out.", "final_layer.FINAL_PROJ.")

    # 通用：残留 .linear. → .lin.（最短最后执行）
    nk = nk.replace(".linear.", ".lin.")
    nk = nk.replace("FINAL_PROJ", "linear")
    return nk


def convert() -> None:
    print(f"读入: {SRC}")
    sd = load_file(str(SRC))
    out: dict[str, torch.Tensor] = {}
    qkv_img: dict[str, list[tuple[str, torch.Tensor]]] = {}
    qkv_txt: dict[str, list[tuple[str, torch.Tensor]]] = {}

    for k, v in sd.items():
        # ── 双流块 img QKV 三合一 ──
        if ".attn.to_q." in k:
            base = k.replace(".attn.to_q.weight", "")
            qkv_img.setdefault(base, []).append(("q", v))
            continue
        if ".attn.to_k." in k:
            base = k.replace(".attn.to_k.weight", "")
            qkv_img.setdefault(base, []).append(("k", v))
            continue
        if ".attn.to_v." in k:
            base = k.replace(".attn.to_v.weight", "")
            qkv_img.setdefault(base, []).append(("v", v))
            continue
        # ── 双流块 txt QKV 三合一 ──
        if ".attn.add_q_proj." in k:
            base = k.replace(".attn.add_q_proj.weight", "")
            qkv_txt.setdefault(base, []).append(("q", v))
            continue
        if ".attn.add_k_proj." in k:
            base = k.replace(".attn.add_k_proj.weight", "")
            qkv_txt.setdefault(base, []).append(("k", v))
            continue
        if ".attn.add_v_proj." in k:
            base = k.replace(".attn.add_v_proj.weight", "")
            qkv_txt.setdefault(base, []).append(("v", v))
            continue

        out[map_key(k)] = v

    # QKV 合并（img 侧）
    for base, parts in qkv_img.items():
        order = {p[0]: p[1] for p in parts}
        if all(x in order for x in "qkv"):
            fused = torch.cat([order["q"], order["k"], order["v"]], dim=0)
            comfy_base = map_key(base + ".dummy").replace(".dummy", "")
            out[f"{comfy_base}.img_attn.qkv.weight"] = fused

    # QKV 合并（txt 侧）
    for base, parts in qkv_txt.items():
        order = {p[0]: p[1] for p in parts}
        if all(x in order for x in "qkv"):
            fused = torch.cat([order["q"], order["k"], order["v"]], dim=0)
            comfy_base = map_key(base + ".dummy").replace(".dummy", "")
            out[f"{comfy_base}.txt_attn.qkv.weight"] = fused

    save_file(out, str(DST))
    size_gb = DST.stat().st_size / 1024**3
    # 验证检测条件
    detect1 = "double_blocks.0.img_attn.norm.key_norm.weight" in out
    detect2 = "img_in.weight" in out
    detect3 = "double_stream_modulation_img.lin.weight" in out
    print(f"输出: {DST} ({size_gb:.2f} GB, {len(out)} 键)")
    print(f"检测条件 1 (double_blocks.0.img_attn.norm.key_norm): {detect1}")
    print(f"检测条件 2 (img_in.weight): {detect2}")
    print(f"检测条件 3 (double_stream_modulation_img.lin): {detect3}")
    if detect1 and detect2:
        print("✓ ComfyUI Flux2 检测条件全满足")
    else:
        print("✗ 检测条件不满足！需修正映射表")


if __name__ == "__main__":
    convert()
