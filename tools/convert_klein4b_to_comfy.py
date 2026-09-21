# 本项目仅供学习使用，商业授权请+Q 3559331368
"""W3-C Phase 2（四轮校准 2026-09-18）：klein-4b diffusers → ComfyUI 格式转换。

完整键映射表——按 klein-9b（ComfyUI 已工作模型）逐键对照推导：

外层：
  x_embedder → img_in | context_embedder → txt_in
  time_guidance_embed.timestep_embedder.linear_1/2 → time_in.in_layer/out_layer
  *_modulation_*.linear → *.lin

双流块 (transformer_blocks.N → double_blocks.N)：
  .attn.to_q/k/v → .img_attn.qkv（三合一）
  .attn.to_out.0 → .img_attn.proj
  .attn.norm_q/k → .img_attn.norm.query_norm/key_norm
  .attn.add_q/k/v_proj → .txt_attn.qkv（三合一）
  .attn.to_add_out → .txt_attn.proj
  .attn.norm_added_q/k → .txt_attn.norm.query_norm/key_norm
  .ff.linear_in/out → .img_mlp.0/.2
  .ff_context.linear_in/out → .txt_mlp.0/.2
  .norm1.linear → .modulation.img.lin
  .norm1_context.linear → .modulation.txt.lin

单流块 (single_transformer_blocks.N → single_blocks.N)：
  .attn.to_qkv_mlp_proj → .linear1（已融合直通）
  .attn.to_out → .linear2
  .attn.norm_q/k → .attn.norm.query_norm/key_norm
  .norm.linear → .modulation.lin

输出层：
  norm_out.linear → final_layer.adaLN_modulation.1
  proj_out → final_layer.linear

RMSNorm：.weight → .scale（comfy 命名约定）
"""
from __future__ import annotations

from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "models/paint/flux2-klein-4b/transformer/diffusion_pytorch_model.safetensors"
DST = ROOT / "tools/ComfyUI_windows_portable/ComfyUI/models/diffusion_models/flux-2-klein-4b.safetensors"


def map_key(k: str) -> str:
    nk = k

    # ── 外层组件 ──
    nk = nk.replace("x_embedder", "img_in")
    nk = nk.replace("context_embedder", "txt_in")
    nk = nk.replace(
        "time_guidance_embed.timestep_embedder.linear_1.", "time_in.in_layer.")
    nk = nk.replace(
        "time_guidance_embed.timestep_embedder.linear_2.", "time_in.out_layer.")
    nk = nk.replace("single_stream_modulation.linear.",
                    "single_stream_modulation.lin.")
    nk = nk.replace("double_stream_modulation_img.linear.",
                    "double_stream_modulation_img.lin.")
    nk = nk.replace("double_stream_modulation_txt.linear.",
                    "double_stream_modulation_txt.lin.")

    # ── 块前缀（先长后短）──
    is_double = "transformer_blocks." in nk
    nk = nk.replace("single_transformer_blocks.", "single_blocks.")
    nk = nk.replace("transformer_blocks.", "double_blocks.")

    if is_double:
        # ── 双流块：img 侧 ──
        nk = nk.replace(".attn.to_out.0.", ".img_attn.proj.")
        nk = nk.replace(".attn.norm_q.", ".img_attn.norm.query_norm.")
        nk = nk.replace(".attn.norm_k.", ".img_attn.norm.key_norm.")
        # ── 双流块：txt 侧 ──
        nk = nk.replace(".attn.to_add_out.", ".txt_attn.proj.")
        nk = nk.replace(".attn.norm_added_q.", ".txt_attn.norm.query_norm.")
        nk = nk.replace(".attn.norm_added_k.", ".txt_attn.norm.key_norm.")
        # ── 双流块：FF ──
        nk = nk.replace(".ff.linear_in.", ".img_mlp.0.")
        nk = nk.replace(".ff.linear_out.", ".img_mlp.2.")
        nk = nk.replace(".ff_context.linear_in.", ".txt_mlp.0.")
        nk = nk.replace(".ff_context.linear_out.", ".txt_mlp.2.")
        # ── 双流块：modulation ──
        nk = nk.replace(".norm1.linear.", ".modulation.img.lin.")
        nk = nk.replace(".norm1_context.linear.", ".modulation.txt.lin.")
    else:
        # ── 单流块 ──
        nk = nk.replace(".attn.to_qkv_mlp_proj.", ".linear1.")
        nk = nk.replace(".attn.to_out.", ".linear2.")
        nk = nk.replace(".attn.norm_q.", ".attn.norm.query_norm.")
        nk = nk.replace(".attn.norm_k.", ".attn.norm.key_norm.")
        nk = nk.replace(".norm.linear.", ".modulation.lin.")

    # ── 输出层 ──
    nk = nk.replace("norm_out.linear.", "final_layer.adaLN_modulation.1.")
    nk = nk.replace("proj_out.", "final_layer.FINAL_PROJ.")

    # ── 通用收尾 ──
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
        # 双流块 img QKV 三合一
        if ".attn.to_q." in k:
            qkv_img.setdefault(k.replace(".attn.to_q.weight", ""),
                               []).append(("q", v))
            continue
        if ".attn.to_k." in k:
            qkv_img.setdefault(k.replace(".attn.to_k.weight", ""),
                               []).append(("k", v))
            continue
        if ".attn.to_v." in k:
            qkv_img.setdefault(k.replace(".attn.to_v.weight", ""),
                               []).append(("v", v))
            continue
        # 双流块 txt QKV 三合一
        if ".attn.add_q_proj." in k:
            qkv_txt.setdefault(k.replace(".attn.add_q_proj.weight", ""),
                               []).append(("q", v))
            continue
        if ".attn.add_k_proj." in k:
            qkv_txt.setdefault(k.replace(".attn.add_k_proj.weight", ""),
                               []).append(("k", v))
            continue
        if ".attn.add_v_proj." in k:
            qkv_txt.setdefault(k.replace(".attn.add_v_proj.weight", ""),
                               []).append(("v", v))
            continue

        out[map_key(k)] = v

    # QKV 合并
    for base, parts in qkv_img.items():
        d = {p[0]: p[1] for p in parts}
        if all(x in d for x in "qkv"):
            fused = torch.cat([d["q"], d["k"], d["v"]], dim=0)
            cb = map_key(base + ".dummy").replace(".dummy", "")
            out[f"{cb}.img_attn.qkv.weight"] = fused

    for base, parts in qkv_txt.items():
        d = {p[0]: p[1] for p in parts}
        if all(x in d for x in "qkv"):
            fused = torch.cat([d["q"], d["k"], d["v"]], dim=0)
            cb = map_key(base + ".dummy").replace(".dummy", "")
            out[f"{cb}.txt_attn.qkv.weight"] = fused

    # RMSNorm：.weight → .scale（四轮修正）
    renames = {}
    for k in list(out.keys()):
        if ".norm.query_norm.weight" in k:
            renames[k] = k.replace(".norm.query_norm.weight",
                                   ".norm.query_norm.scale")
        elif ".norm.key_norm.weight" in k:
            renames[k] = k.replace(".norm.key_norm.weight",
                                   ".norm.key_norm.scale")
    for old_k, new_k in renames.items():
        out[new_k] = out.pop(old_k)
    if renames:
        print(f"  norm scale 重命名 {len(renames)} 条")

    save_file(out, str(DST))
    size_gb = DST.stat().st_size / 1024**3
    # 验证
    ok1 = "double_blocks.0.img_attn.norm.key_norm.scale" in out
    ok2 = "img_in.weight" in out
    ok3 = "double_stream_modulation_img.lin.weight" in out
    ok4 = "txt_in.weight" in out
    ok5 = "time_in.in_layer.weight" in out
    ok6 = "final_layer.linear.weight" in out
    print(f"输出: {DST.name} ({size_gb:.2f} GB, {len(out)} 键)")
    print(f"检测: norm_scale={ok1} img_in={ok2} mod_lin={ok3} "
          f"txt_in={ok4} time_in={ok5} final={ok6}")
    if all((ok1, ok2, ok3, ok4, ok5, ok6)):
        print("✓ 全部验证通过")
    else:
        print("✗ 有验证项失败！")


if __name__ == "__main__":
    convert()
