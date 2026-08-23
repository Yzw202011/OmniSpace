
import triton
import triton.language as tl

from torch._inductor.runtime import triton_helpers, triton_heuristics
from torch._inductor.runtime.triton_helpers import libdevice, math as tl_math
from torch._inductor.runtime.hints import AutotuneHint, ReductionHint, TileHint, DeviceProperties

from torch._dynamo.testing import rand_strided
from torch._C import _cuda_getCurrentRawStream as get_raw_stream
import torch

@triton_heuristics.pointwise(
    size_hints={'x': 4194304}, tile_hint=TileHint.DEFAULT,
    filename=__file__,
    triton_meta={'signature': {'in_ptr0': '*fp16', 'in_ptr1': '*fp32', 'in_ptr2': '*fp16', 'in_ptr3': '*fp16', 'in_ptr4': '*fp16', 'in_ptr5': '*fp32', 'in_ptr6': '*fp16', 'out_ptr0': '*fp16', 'out_ptr1': '*fp16', 'out_ptr2': '*fp16', 'out_ptr3': '*fp16', 'xnumel_0': 'i32', 'xnumel_1': 'i32', 'XBLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'enable_fp_fusion': True, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (3,): [['tt.divisibility', 16]], (4,): [['tt.divisibility', 16]], (5,): [['tt.divisibility', 16]], (6,): [['tt.divisibility', 16]], (7,): [['tt.divisibility', 16]], (8,): [['tt.divisibility', 16]], (9,): [['tt.divisibility', 16]], (10,): [['tt.divisibility', 16]], (11,): [['tt.divisibility', 16]], (12,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'SequentialComboKernelGrid', 'combo_grid_meta': {'num_kernels': 2, 'min_blocks': None, 'default_config': None, 'no_x_dim_0': False, 'xnumel_0': None, 'no_x_dim_1': False, 'xnumel_1': None}, 'kernel_name': 'triton_poi_fused_5', 'mutated_arg_names': [], 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False}
)
@triton.jit
def triton_poi_fused_5(in_ptr0, in_ptr1, in_ptr2, in_ptr3, in_ptr4, in_ptr5, in_ptr6, out_ptr0, out_ptr1, out_ptr2, out_ptr3, xnumel_0, xnumel_1, XBLOCK : tl.constexpr):
    pid = tl.program_id(0)
    num_xblocks_0 = tl.cdiv(xnumel_0, XBLOCK)
    num_xblocks_1 = num_xblocks_0 + tl.cdiv(xnumel_1, XBLOCK)
    if pid < num_xblocks_0:
        pid_offset = pid
        r0_numel = 1
        xoffset = pid_offset * XBLOCK
        xindex = xoffset + tl.arange(0, XBLOCK)[:]
        xmask = xindex < xnumel_0
        x0 = (xindex % 64)
        x1 = ((xindex // 64) % 8)
        x2 = xindex // 512
        x3 = xindex // 64
        tmp0 = tl.load(in_ptr0 + (4096 + x0 + 128*x1 + 6144*x2), xmask).to(tl.float32)
        tmp2 = tl.load(in_ptr1 + (x3), xmask, eviction_policy='evict_last')
        tmp10 = tl.load(in_ptr2 + (x0), xmask, eviction_policy='evict_last').to(tl.float32)
        tmp12 = tl.load(in_ptr3 + (x0 + 64*x2), xmask, eviction_policy='evict_last').to(tl.float32)
        tmp14 = tl.load(in_ptr0 + (4160 + x0 + 128*x1 + 6144*x2), xmask).to(tl.float32)
        tmp18 = tl.load(in_ptr2 + (64 + x0), xmask, eviction_policy='evict_last').to(tl.float32)
        tmp20 = tl.load(in_ptr4 + (x0 + 64*x2), xmask, eviction_policy='evict_last').to(tl.float32)
        tmp1 = tmp0.to(tl.float32)
        tmp3 = tl.full([1], 128.0, tl.float32)
        tmp4 = (tmp2 / tmp3)
        tmp5 = tl.full([1], 1e-06, tl.float32)
        tmp6 = tmp4 + tmp5
        tmp7 = libdevice.rsqrt(tmp6)
        tmp8 = tmp1 * tmp7
        tmp9 = tmp8.to(tl.float32)
        tmp11 = tmp9 * tmp10
        tmp13 = tmp11 * tmp12
        tmp15 = tmp14.to(tl.float32)
        tmp16 = tmp15 * tmp7
        tmp17 = tmp16.to(tl.float32)
        tmp19 = tmp17 * tmp18
        tmp21 = tmp19 * tmp20
        tmp22 = tmp13 - tmp21
        tmp23 = tmp19 * tmp12
        tmp24 = tmp11 * tmp20
        tmp25 = tmp23 + tmp24
        tl.store(out_ptr0 + (x0 + 128*x3), tmp22, xmask)
        tl.store(out_ptr1 + (x0 + 128*x3), tmp25, xmask)
    elif pid < num_xblocks_1:
        pid_offset = pid - num_xblocks_0
        r0_numel = 1
        xoffset = pid_offset * XBLOCK
        xindex = xoffset + tl.arange(0, XBLOCK)[:]
        xmask = xindex < xnumel_1
        x4 = (xindex % 64)
        x5 = ((xindex // 64) % 32)
        x6 = xindex // 2048
        x7 = xindex // 64
        tmp26 = tl.load(in_ptr0 + (x4 + 128*x5 + 6144*x6), xmask).to(tl.float32)
        tmp28 = tl.load(in_ptr5 + (x7), xmask, eviction_policy='evict_last')
        tmp36 = tl.load(in_ptr6 + (x4), xmask, eviction_policy='evict_last').to(tl.float32)
        tmp38 = tl.load(in_ptr3 + (x4 + 64*x6), xmask, eviction_policy='evict_last').to(tl.float32)
        tmp40 = tl.load(in_ptr0 + (64 + x4 + 128*x5 + 6144*x6), xmask).to(tl.float32)
        tmp44 = tl.load(in_ptr6 + (64 + x4), xmask, eviction_policy='evict_last').to(tl.float32)
        tmp46 = tl.load(in_ptr4 + (x4 + 64*x6), xmask, eviction_policy='evict_last').to(tl.float32)
        tmp27 = tmp26.to(tl.float32)
        tmp29 = tl.full([1], 128.0, tl.float32)
        tmp30 = (tmp28 / tmp29)
        tmp31 = tl.full([1], 1e-06, tl.float32)
        tmp32 = tmp30 + tmp31
        tmp33 = libdevice.rsqrt(tmp32)
        tmp34 = tmp27 * tmp33
        tmp35 = tmp34.to(tl.float32)
        tmp37 = tmp35 * tmp36
        tmp39 = tmp37 * tmp38
        tmp41 = tmp40.to(tl.float32)
        tmp42 = tmp41 * tmp33
        tmp43 = tmp42.to(tl.float32)
        tmp45 = tmp43 * tmp44
        tmp47 = tmp45 * tmp46
        tmp48 = tmp39 - tmp47
        tmp49 = tmp45 * tmp38
        tmp50 = tmp37 * tmp46
        tmp51 = tmp49 + tmp50
        tl.store(out_ptr2 + (x4 + 128*x7), tmp48, xmask)
        tl.store(out_ptr3 + (x4 + 128*x7), tmp51, xmask)
    else:
        pass


def get_args():
    arg_0 = rand_strided((2048, 6144), (6144, 1), device='cuda:0', dtype=torch.float16)
    arg_1 = rand_strided((2048, 8, 1), (8, 1, 16384), device='cuda:0', dtype=torch.float32)
    arg_2 = rand_strided((128,), (1,), device='cuda:0', dtype=torch.float16)
    arg_3 = rand_strided((2048, 64), (64, 1), device='cuda:0', dtype=torch.float16)
    arg_4 = rand_strided((2048, 64), (64, 1), device='cuda:0', dtype=torch.float16)
    arg_5 = rand_strided((2048, 32, 1), (32, 1, 65536), device='cuda:0', dtype=torch.float32)
    arg_6 = rand_strided((128,), (1,), device='cuda:0', dtype=torch.float16)
    arg_7 = rand_strided((2048, 8, 64), (1024, 128, 1), device='cuda:0', dtype=torch.float16)
    arg_8 = rand_strided((2048, 8, 64), (1024, 128, 1), device='cuda:0', dtype=torch.float16)
    arg_9 = rand_strided((2048, 32, 64), (4096, 128, 1), device='cuda:0', dtype=torch.float16)
    arg_10 = rand_strided((2048, 32, 64), (4096, 128, 1), device='cuda:0', dtype=torch.float16)
    return arg_0, arg_1, arg_2, arg_3, arg_4, arg_5, arg_6, arg_7, arg_8, arg_9, arg_10, 1048576, 4194304,


def call(args):
    with torch.cuda._DeviceGuard(0):
        torch.cuda.set_device(0)
        stream0 = get_raw_stream(0)
        triton_poi_fused_5.run(*args, stream=stream0)


def benchmark_all_configs(args):
    with torch.cuda._DeviceGuard(0):
        torch.cuda.set_device(0)
        return triton_poi_fused_5.benchmark_all_configs(*args)


if __name__ == '__main__':
    from torch._inductor.runtime.benchmarking import benchmarker

    args = get_args()
    ms = benchmarker.benchmark(call, fn_args=(args,), device=cuda,rep=40)
    num_gb = 0
    gb_per_s = num_gb / (ms / 1e3)
    print(f"{ms:.3f}ms    {num_gb:.3f}GB    {gb_per_s:.2f}GB/s")
