
import triton
import triton.language as tl

from torch._inductor.runtime import triton_helpers, triton_heuristics
from torch._inductor.runtime.triton_helpers import libdevice, math as tl_math
from torch._inductor.runtime.hints import AutotuneHint, ReductionHint, TileHint, DeviceProperties
triton_helpers.set_driver_to_gpu()

from torch._dynamo.testing import rand_strided
from torch._C import _cuda_getCurrentRawStream as get_raw_stream
import torch

@triton_heuristics.pointwise(
    size_hints={'x': 4194304}, 
    filename=__file__,
    triton_meta={'signature': {'in_ptr0': '*fp16', 'in_ptr1': '*fp32', 'in_ptr2': '*fp16', 'in_ptr3': '*fp16', 'in_ptr4': '*fp16', 'out_ptr0': '*fp16', 'out_ptr1': '*fp16', 'xnumel': 'i32', 'XBLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'native_matmul': False, 'enable_fp_fusion': True, 'launch_pdl': False, 'disable_ftz': False, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (3,): [['tt.divisibility', 16]], (4,): [['tt.divisibility', 16]], (5,): [['tt.divisibility', 16]], (6,): [['tt.divisibility', 16]], (7,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'Grid1D', 'autotune_hints': set(), 'kernel_name': 'Placeholder.DESCRIPTIVE_NAME', 'mutated_arg_names': [], 'optimize_mem': True, 'no_x_dim': False, 'atomic_add_found': False, 'num_load': 7, 'num_store': 2, 'num_reduction': 0, 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False, 'kernel_num_gb': 0.03434112, 'kernel_flop': 0},
    min_elem_per_thread=0
)
@triton.jit
def triton_(in_ptr0, in_ptr1, in_ptr2, in_ptr3, in_ptr4, out_ptr0, out_ptr1, xnumel, XBLOCK : tl.constexpr):
    xoffset = tl.program_id(0) * XBLOCK
    xindex = xoffset + tl.arange(0, XBLOCK)[:]
    xmask = xindex < xnumel
    x0 = (xindex % 64)
    x1 = ((xindex // 64) % 32)
    x2 = xindex // 2048
    x3 = xindex // 64
    tmp0 = tl.load(in_ptr0 + (x0 + 128*x1 + 6144*x2), xmask).to(tl.float32)
    tmp2 = tl.load(in_ptr1 + (x3), xmask, eviction_policy='evict_last')
    tmp10 = tl.load(in_ptr2 + (x0), xmask, eviction_policy='evict_last').to(tl.float32)
    tmp12 = tl.load(in_ptr3 + (x0 + 64*x2), xmask, eviction_policy='evict_last').to(tl.float32)
    tmp14 = tl.load(in_ptr0 + (64 + x0 + 128*x1 + 6144*x2), xmask).to(tl.float32)
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


def get_args():
    arg_0 = rand_strided((2048, 6144), (6144, 1), device='cuda:0', dtype=torch.float16)
    arg_1 = rand_strided((2048, 32, 1), (32, 1, 65536), device='cuda:0', dtype=torch.float32)
    arg_2 = rand_strided((128,), (1,), device='cuda:0', dtype=torch.float16)
    arg_3 = rand_strided((2048, 64), (64, 1), device='cuda:0', dtype=torch.float16)
    arg_4 = rand_strided((2048, 64), (64, 1), device='cuda:0', dtype=torch.float16)
    arg_5 = rand_strided((2048, 32, 64), (4096, 128, 1), device='cuda:0', dtype=torch.float16)
    arg_6 = rand_strided((2048, 32, 64), (4096, 128, 1), device='cuda:0', dtype=torch.float16)
    return arg_0, arg_1, arg_2, arg_3, arg_4, arg_5, arg_6, 4194304,


def call(args):
    with torch.cuda._DeviceGuard(0):
        torch.cuda.set_device(0)
        stream0 = get_raw_stream(0)
        triton_.run(*args, stream=stream0)


def benchmark_all_configs(args):
    with torch.cuda._DeviceGuard(0):
        torch.cuda.set_device(0)
        return triton_.benchmark_all_configs(*args)


if __name__ == '__main__':
    from torch._inductor.runtime.benchmarking import benchmarker

    args = get_args()
    ms = benchmarker.benchmark(lambda: call(args), device='cuda', rep=40)
    num_gb = 0.03434112
    gb_per_s = num_gb / (ms / 1e3)
    print(f"{ms:.3f}ms    {num_gb:.3f}GB    {gb_per_s:.2f}GB/s")
