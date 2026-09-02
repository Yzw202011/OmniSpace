"""Qwen-Image GGUF 流式推理（WDDM 性能修复，2026-08-22）。

问题：GGUF transformer（12.4GB 量化权重）常驻 GPU 后，16GB 卡 dedicated
显存耗尽（driver_free=0），WDDM 进入 demand-paging——所有 GEMM 的物理页
被反复换出/换入，慢 25-55 倍，实测 84s/步（30 步生图 42 分钟）。

方案：量化权重常驻 CPU（pageable RAM），逐层 H2D 传输（~12.4GB/步，
PCIe ≈500ms）+ GPU 反量化（Triton kernel，fp32 位精确对拍）+ GEMM。
GPU 峰值显存 0.49GB（原 12.9GB），实测 2.15s/步（39 倍加速），且
12GB 基线卡（RTX 3060）可跑——符合 CLAUDE.md 硬件基线。

WDDM 踩坑记录（勿重蹈）：
- 大规模 pin_memory 不可用：pinned 内存吃 GPU commit budget，累计 pin
  11GB 后任何 GPU 分配（含 0.5MB）都报 cudaErrorMemoryAllocation；
- expandable_segments 在 Windows/WDDM 不支持（torch 2.11）；
- empty_cache 无法归还加载碎片（reserved 13.24GB 中 0.85GB 死块）；
- 反量化临时 tensor 落 sysmem 的根因不是分配路径，而是 dedicated 耗尽
  后 WDDM 对全部 GPU 工作集换页。

用法（paint_engine GGUF 分支）::

    from .qwen_gguf_stream import install_streaming
    info = install_streaming(transformer)   # 权重保持 CPU，替换 forward
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import torch

logger = __import__("logging").getLogger("omnispace.inference.qwen_stream")

# Triton kernel 延迟加载（import triton 前必须注入 CC，见 _ensure_kernels）
_KERNELS: dict[str, Any] | None = None
_STATE: dict[str, Any] = {"buf": None, "orig_forward": None}

_ROOT = Path(__file__).resolve().parents[3]  # 项目根
_TCC = _ROOT / "pydeps" / "triton" / "runtime" / "tcc" / "tcc.exe"


def _ensure_kernels() -> dict[str, Any] | None:
    """延迟定义 Triton 反量化 kernels（Q4_K/Q5_K/Q6_K/Q8_0）。

    数值对拍：fp32 输出与 diffusers 参考实现 bit-exact；bf16 输出
    maxrel≈3.9e-3（kernel 走 fp32 中间数学，参考实现走 fp16）。
    返回 None 表示 Triton 不可用（调用方回退参考反量化路径）。
    """
    global _KERNELS
    if _KERNELS is not None:
        return _KERNELS if _KERNELS else None
    try:
        if _TCC.is_file():
            os.environ.setdefault("CC", str(_TCC))  # triton-windows TCC 编译器
        import triton
        import triton.language as tl
        from diffusers.quantizers.gguf import utils as ggu

        ggml_sizes = ggu.GGML_QUANT_SIZES

        @triton.jit
        def _deq_q4k(q8p, f16p, op, OUT_DTYPE: tl.constexpr):
            pid = tl.program_id(0)
            base = q8p + pid * 144
            d = tl.load(f16p + pid * 72).to(tl.float32)
            dm = tl.load(f16p + pid * 72 + 1).to(tl.float32)
            offs = tl.arange(0, 256)
            g = offs // 32
            p = offs % 32
            b_d = tl.load(base + 4 + tl.where(g < 4, g, g - 4)).to(tl.int32)
            b_m = tl.load(base + 4 + tl.where(g < 4, g + 4, g)).to(tl.int32)
            b_md = tl.load(base + 8 + g).to(tl.int32)
            sc = tl.where(g < 4, b_d & 0x3F,
                          (b_md & 0x0F) | ((b_d >> 2) & 0x30)).to(tl.float32)
            m = tl.where(g < 4, b_m & 0x3F,
                         (b_md >> 4) | ((b_m >> 2) & 0x30)).to(tl.float32)
            qb = tl.load(base + 16 + (g // 2) * 32 + p).to(tl.int32)
            nib = (qb >> ((g % 2) * 4)) & 0x0F
            out = d * sc * nib - dm * m
            tl.store(op + pid * 256 + offs, out.to(OUT_DTYPE))

        @triton.jit
        def _deq_q5k(q8p, f16p, op, OUT_DTYPE: tl.constexpr):
            pid = tl.program_id(0)
            base = q8p + pid * 176
            d = tl.load(f16p + pid * 88).to(tl.float32)
            dm = tl.load(f16p + pid * 88 + 1).to(tl.float32)
            offs = tl.arange(0, 256)
            g = offs // 32
            p = offs % 32
            b_d = tl.load(base + 4 + tl.where(g < 4, g, g - 4)).to(tl.int32)
            b_m = tl.load(base + 4 + tl.where(g < 4, g + 4, g)).to(tl.int32)
            b_md = tl.load(base + 8 + g).to(tl.int32)
            sc = tl.where(g < 4, b_d & 0x3F,
                          (b_md & 0x0F) | ((b_d >> 2) & 0x30)).to(tl.float32)
            m = tl.where(g < 4, b_m & 0x3F,
                         (b_md >> 4) | ((b_m >> 2) & 0x30)).to(tl.float32)
            bit = (tl.load(base + 16 + p).to(tl.int32) >> g) & 1
            qb = tl.load(base + 48 + (g // 2) * 32 + p).to(tl.int32)
            nib = (qb >> ((g % 2) * 4)) & 0x0F
            q = nib | (bit << 4)
            out = d * sc * q - dm * m
            tl.store(op + pid * 256 + offs, out.to(OUT_DTYPE))

        @triton.jit
        def _deq_q6k(q8p, f16p, i8p, op, OUT_DTYPE: tl.constexpr):
            pid = tl.program_id(0)
            base = q8p + pid * 210
            d = tl.load(f16p + pid * 105 + 104).to(tl.float32)
            offs = tl.arange(0, 256)
            g = offs // 32
            p = offs % 32
            qlb = tl.load(base + (g // 4) * 64 + (g % 2) * 32 + p).to(tl.int32)
            nib = (qlb >> (((g % 4) // 2) * 4)) & 0x0F
            qhb = tl.load(base + 128 + (g // 4) * 32 + p).to(tl.int32)
            q2 = (qhb >> ((g % 4) * 2)) & 0x03
            q = ((q2 << 4) | nib) - 32
            sc = tl.load(i8p + pid * 210 + 192 + offs // 16).to(tl.float32)
            out = d * sc * q
            tl.store(op + pid * 256 + offs, out.to(OUT_DTYPE))

        @triton.jit
        def _deq_q80(q8p, f16p, i8p, op, n_blocks, OUT_DTYPE: tl.constexpr):
            pid = tl.program_id(0)
            offs = tl.arange(0, 256)
            b = offs // 32
            i = offs % 32
            blk = pid * 8 + b
            mask = blk < n_blocks
            d = tl.load(f16p + blk * 17, mask=mask, other=0.0).to(tl.float32)
            x = tl.load(i8p + blk * 34 + 2 + i, mask=mask, other=0).to(tl.float32)
            out = d * x
            tl.store(op + blk * 32 + i, out.to(OUT_DTYPE), mask=mask)

        _KERNELS = {
            "triton": triton, "sizes": ggml_sizes,
            "q4k": _deq_q4k, "q5k": _deq_q5k, "q6k": _deq_q6k, "q80": _deq_q80,
            "unquant": ggu.UNQUANTIZED_TYPES, "dequantize": ggu.dequantize_gguf_tensor,
        }
    except Exception as exc:  # pragma: no cover - 环境异常路径
        _KERNELS = {}
        logger.warning("Triton kernels 不可用（回退参考反量化）: %s", exc)
    return _KERNELS if _KERNELS else None


def _cdiv(a: int, b: int) -> int:
    return (a + b - 1) // b


def _dequant_into(qw: torch.Tensor, qt: int, k: dict[str, Any],
                  buf: torch.Tensor) -> torch.Tensor:
    """Triton 反量化直写持久 buf，返回 buf 前段的 (rows, cols) 视图。"""
    import gguf

    Q = gguf.GGMLQuantizationType
    bs, ts = k["sizes"][qt]
    rows = qw.shape[0]
    cols = qw.shape[1] // ts * bs
    n_blocks = qw.numel() // ts
    flat = qw.reshape(-1)
    out = buf[: n_blocks * bs].view(n_blocks, bs)
    # OUT_DTYPE 由 buf.dtype 决定（bf16）；fp16/fp32 按需扩展
    if buf.dtype == torch.bfloat16:
        from triton.language import bfloat16 as OUT
    elif buf.dtype == torch.float16:
        from triton.language import float16 as OUT
    else:
        from triton.language import float32 as OUT
    if qt == Q.Q4_K:
        k["q4k"][(n_blocks,)](flat, flat.view(torch.float16), out, OUT)
    elif qt == Q.Q5_K:
        k["q5k"][(n_blocks,)](flat, flat.view(torch.float16), out, OUT)
    elif qt == Q.Q6_K:
        k["q6k"][(n_blocks,)](flat, flat.view(torch.float16),
                              flat.view(torch.int8), out, OUT)
    elif qt == Q.Q8_0:
        k["q80"][(_cdiv(n_blocks, 8),)](
            flat, flat.view(torch.float16), flat.view(torch.int8),
            out, n_blocks, OUT)
    else:
        raise ValueError(f"不支持的量化类型: {qt}")
    return buf[: rows * cols].view(rows, cols)


def _streamed_forward(self: Any, inputs: torch.Tensor) -> torch.Tensor:
    """GGUFLinear 流式前向：权重 CPU→GPU 传输 + 反量化 + GEMM。

    GPU 常驻仅 ~0.5GB（BUF 108MB + 激活），不触发 WDDM 换页。
    输入与权重统一到 compute_dtype（bf16）——对齐原生 forward_cuda 的
    inputs.to(compute_dtype) 语义（pipeline 传入的 latents 是 fp32）。
    """
    qw = self.weight.to("cuda", non_blocking=True)  # pageable H2D
    qt = self._stream_qtype
    dt = getattr(self, "compute_dtype", None) or inputs.dtype
    k = _KERNELS if _KERNELS else None
    if k and qt in k["unquant"]:
        # 注意 dequantize_gguf_tensor 对 BF16 层返回 fp32，必须显式转 dt
        w = k["dequantize"](qw).to(dt)
        return torch.nn.functional.linear(inputs.to(dt), w, self.bias)
    if (k is not None and _STATE["buf"] is not None
            and _STATE["buf"].dtype == dt):
        buf = _STATE["buf"]
        bs, ts = k["sizes"][qt]
        rows = qw.shape[0]
        cols = qw.shape[1] // ts * bs
        if rows * cols <= buf.numel():
            w = _dequant_into(qw, qt, k, buf)
            return torch.nn.functional.linear(inputs.to(dt), w, self.bias)
    # 回退：参考反量化（GPU 空闲，临时分配落 dedicated，无性能坑）
    from diffusers.quantizers.gguf import utils as ggu

    w = ggu.dequantize_gguf_tensor(qw).to(dt)
    return torch.nn.functional.linear(inputs.to(dt), w, self.bias)


def _warmup(k: dict[str, Any], buf: torch.Tensor) -> None:
    """预触发 4 个 kernel 的 JIT 编译（小块假数据），避免首次生图付编译时间。"""
    import gguf

    Q = gguf.GGMLQuantizationType
    n = 16
    dev = torch.device("cuda")
    for qt, ts in ((Q.Q4_K, 144), (Q.Q5_K, 176), (Q.Q6_K, 210), (Q.Q8_0, 34)):
        try:
            blocks = torch.randint(
                0, 256, (n, ts), dtype=torch.uint8, device=dev)
            _dequant_into(blocks, qt, k, buf)
        except Exception as exc:
            logger.warning("kernel warmup %s 失败: %s", qt, exc)
            break
    torch.cuda.synchronize()


def install_streaming(transformer: Any) -> dict[str, Any]:
    """给 GGUF transformer 安装流式推理布局。

    - 权重保持 CPU（调用方不得再 .to("cuda")）
    - 替换 GGUFLinear.forward 为逐层传输 + GPU 反量化
    - 分配持久反量化 buffer（最大层 108MB，所有层复用）
    返回统计信息 dict；Triton 不可用时自动回退参考反量化（功能不降级）。
    """
    from diffusers.quantizers.gguf import utils as ggu

    layers = [m for m in transformer.modules() if isinstance(m, ggu.GGUFLinear)]
    if not layers:
        raise RuntimeError("transformer 中未找到 GGUFLinear（非 GGUF 布局？）")
    max_elems = 0
    total_bytes = 0
    gguf_weight_ids = set()
    for m in layers:
        m._stream_qtype = m.weight.quant_type
        gguf_weight_ids.add(id(m.weight))
        max_elems = max(max_elems, m.out_features * m.in_features)
        total_bytes += m.weight.numel()
    # 非 GGUF 权重（norm/time embedding/bias 等，~119MB）常驻 GPU——
    # latents 在 cuda，漏搬会在首个 addmm 报 device mismatch
    for _, p in transformer.named_parameters():
        if id(p) not in gguf_weight_ids and p.device.type == "cpu":
            p.data = p.data.to("cuda")
    for _, b in transformer.named_buffers():
        if b.device.type == "cpu":
            b.data = b.data.to("cuda")
    k = _ensure_kernels()
    if _STATE["buf"] is None:
        _STATE["buf"] = torch.empty(
            max_elems, device="cuda", dtype=torch.bfloat16)
    if _STATE["orig_forward"] is None:
        _STATE["orig_forward"] = ggu.GGUFLinear.forward
        ggu.GGUFLinear.forward = _streamed_forward
    if k is not None:
        _warmup(k, _STATE["buf"])
    info = {
        "layers": len(layers),
        "cpu_weights_gb": round(total_bytes / 2**30, 2),
        "buf_mb": round(max_elems * 2 / 2**20, 1),
        "triton": k is not None,
    }
    logger.info("qwen-image 流式布局安装: %s", info)
    return info


def uninstall_streaming() -> None:
    """恢复原生 GGUFLinear.forward（模型卸载时调用，防止影响后续模型）。"""
    global _KERNELS
    if _STATE["orig_forward"] is not None:
        from diffusers.quantizers.gguf import utils as ggu

        ggu.GGUFLinear.forward = _STATE["orig_forward"]
        _STATE["orig_forward"] = None
    if _STATE["buf"] is not None:
        _STATE["buf"] = None
        torch.cuda.empty_cache()
    _KERNELS = None
