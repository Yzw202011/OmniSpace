r"""
Compile-time auto-tuning block: 

import torch
from torch._dynamo.testing import rand_strided
from torch._dynamo.utils import preserve_rng_state
from torch._inductor.select_algorithm import AlgorithmSelectorCache
from torch._inductor.async_compile import AsyncCompile

async_compile = AsyncCompile()
generate_example_value = AlgorithmSelectorCache.generate_example_value
empty_strided_cuda = torch._C._dynamo.guards._empty_strided_cuda
empty_strided_xpu = torch._C._dynamo.guards._empty_strided_xpu
get_raw_stream = torch._C._cuda_getCurrentRawStream


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\wf\cwfkr34rpzltukx5wpsiiashomla3aehmv3pq3b753lnj3qooxlt.py
# Unsorted Source Nodes: [], Original ATen: []
# Source node to ATen node mapping:
triton_poi_fused_0 = async_compile.triton('triton_poi_fused_0', '''
import triton
import triton.language as tl

from torch._inductor.runtime import triton_helpers, triton_heuristics
from torch._inductor.runtime.triton_helpers import libdevice, math as tl_math
from torch._inductor.runtime.hints import AutotuneHint, ReductionHint, TileHint, DeviceProperties
triton_helpers.set_driver_to_gpu()

@triton_heuristics.pointwise(
    size_hints={'x': 131072}, 
    filename=__file__,
    triton_meta={'signature': {'in_ptr0': '*i64', 'in_ptr1': '*fp16', 'out_ptr0': '*fp16', 'out_ptr1': '*fp16', 'ks0': 'i64', 'xnumel': 'i32', 'XBLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'native_matmul': False, 'enable_fp_fusion': True, 'launch_pdl': False, 'disable_ftz': False, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (3,): [['tt.divisibility', 16]], (5,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'Grid1D', 'autotune_hints': set(), 'kernel_name': 'triton_poi_fused_0', 'mutated_arg_names': [], 'optimize_mem': True, 'no_x_dim': False, 'atomic_add_found': False, 'num_load': 3, 'num_store': 2, 'num_reduction': 0, 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False},
    min_elem_per_thread=0
)
@triton.jit
def triton_poi_fused_0(in_ptr0, in_ptr1, out_ptr0, out_ptr1, ks0, xnumel, XBLOCK : tl.constexpr):
    xoffset = tl.program_id(0) * XBLOCK
    xindex = xoffset + tl.arange(0, XBLOCK)[:]
    xmask = xindex < xnumel
    x0 = (xindex % 64)
    x1 = xindex // 64
    x2 = xindex
    tmp30 = tl.load(in_ptr0 + (x1), xmask, eviction_policy='evict_last')
    tmp0 = x0
    tmp1 = tl.full([1], 2, tl.int64)
    tmp2 = tmp0 >= tmp1
    tmp3 = tl.full([1], 60, tl.int64)
    tmp4 = tmp0 < tmp3
    tmp5 = (((-2) + x0) % 3)
    tmp6 = tl.full([1], 0, tl.int64)
    tmp7 = tmp5 == tmp6
    tmp8 = tmp2 & tmp4
    tmp9 = tmp8 & tmp7
    tmp10 = tl.load(in_ptr0 + (x1 + 2*ks0), tmp9 & xmask, eviction_policy='evict_last', other=0.0)
    tmp11 = tl.full([XBLOCK], 1048576, tl.int32)
    tmp12 = tmp10 + tmp11
    tmp13 = tmp10 < 0
    tmp14 = tl.where(tmp13, tmp12, tmp10)
    tl.device_assert(((0 <= tl.broadcast_to(tmp14, [XBLOCK])) & (tl.broadcast_to(tmp14, [XBLOCK]) < 1048576)) | ~(tmp9 & xmask), "index out of bounds: 0 <= tl.broadcast_to(tmp14, [XBLOCK]) < 1048576")
    tmp16 = tl.load(in_ptr1 + (2 + 3*(triton_helpers.div_floor_integer((-2) + x0,  3)) + 128*tmp14), tmp9 & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp17 = tl.full([1], 1, tl.int64)
    tmp18 = tmp0 >= tmp17
    tmp19 = (((-1) + x0) % 3)
    tmp20 = tmp19 == tmp6
    tmp21 = tmp18 & tmp4
    tmp22 = tmp21 & tmp20
    tmp23 = tl.load(in_ptr0 + (ks0 + x1), tmp22 & xmask, eviction_policy='evict_last', other=0.0)
    tmp24 = tl.full([XBLOCK], 1048576, tl.int32)
    tmp25 = tmp23 + tmp24
    tmp26 = tmp23 < 0
    tmp27 = tl.where(tmp26, tmp25, tmp23)
    tl.device_assert(((0 <= tl.broadcast_to(tmp27, [XBLOCK])) & (tl.broadcast_to(tmp27, [XBLOCK]) < 1048576)) | ~(tmp22 & xmask), "index out of bounds: 0 <= tl.broadcast_to(tmp27, [XBLOCK]) < 1048576")
    tmp29 = tl.load(in_ptr1 + (1 + 3*(triton_helpers.div_floor_integer((-1) + x0,  3)) + 128*tmp27), tmp22 & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp31 = tl.full([XBLOCK], 1048576, tl.int32)
    tmp32 = tmp30 + tmp31
    tmp33 = tmp30 < 0
    tmp34 = tl.where(tmp33, tmp32, tmp30)
    tl.device_assert(((0 <= tmp34) & (tmp34 < 1048576)) | ~(xmask), "index out of bounds: 0 <= tmp34 < 1048576")
    tmp36 = tl.load(in_ptr1 + (x0 + 128*tmp34), xmask).to(tl.float32)
    tmp37 = tl.where(tmp22, tmp29, tmp36)
    tmp38 = tl.where(tmp9, tmp16, tmp37)
    tmp39 = tl.load(in_ptr1 + (66 + 3*(triton_helpers.div_floor_integer((-2) + x0,  3)) + 128*tmp14), tmp9 & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp40 = tl.load(in_ptr1 + (65 + 3*(triton_helpers.div_floor_integer((-1) + x0,  3)) + 128*tmp27), tmp22 & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp41 = tl.load(in_ptr1 + (64 + x0 + 128*tmp34), xmask).to(tl.float32)
    tmp42 = tl.where(tmp22, tmp40, tmp41)
    tmp43 = tl.where(tmp9, tmp39, tmp42)
    tl.store(out_ptr0 + (x2), tmp38, xmask)
    tl.store(out_ptr1 + (x2), tmp43, xmask)
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\3t\c3tt67dhakpz3wscxctpfv7lwou2oknsvw2k5x3qqkvfdsmcbllv.py
# Unsorted Source Nodes: [], Original ATen: []
# Source node to ATen node mapping:
triton_red_fused_1 = async_compile.triton('triton_red_fused_1', '''
import triton
import triton.language as tl

from torch._inductor.runtime import triton_helpers, triton_heuristics
from torch._inductor.runtime.triton_helpers import libdevice, math as tl_math
from torch._inductor.runtime.hints import AutotuneHint, ReductionHint, TileHint, DeviceProperties
triton_helpers.set_driver_to_gpu()

@triton_heuristics.reduction(
    size_hints={'x': 2048, 'r0_': 4096},
    reduction_hint=ReductionHint.INNER,
    filename=__file__,
    triton_meta={'signature': {'in_ptr0': '*fp16', 'in_ptr1': '*fp16', 'out_ptr1': '*fp16', 'xnumel': 'i32', 'r0_numel': 'i32', 'XBLOCK': 'constexpr', 'R0_BLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'native_matmul': False, 'enable_fp_fusion': True, 'launch_pdl': False, 'disable_ftz': False, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (4,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'Grid1D', 'autotune_hints': set(), 'kernel_name': 'triton_red_fused_1', 'mutated_arg_names': [], 'optimize_mem': True, 'no_x_dim': False, 'atomic_add_found': False, 'num_load': 3, 'num_store': 1, 'num_reduction': 1, 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False, 'add_persistent_rblock': True}
)
@triton.jit
def triton_red_fused_1(in_ptr0, in_ptr1, out_ptr1, xnumel, r0_numel, XBLOCK : tl.constexpr, R0_BLOCK : tl.constexpr):
    r0_numel = 4096
    rnumel = r0_numel
    RBLOCK: tl.constexpr = R0_BLOCK
    xoffset = tl.program_id(0) * XBLOCK
    xindex = xoffset + tl.arange(0, XBLOCK)[:, None]
    xmask = xindex < xnumel
    r0_base = tl.arange(0, R0_BLOCK)[None, :]
    rbase = r0_base
    x0 = xindex
    _tmp4 = tl.full([XBLOCK, R0_BLOCK], 0, tl.float32)
    for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
        r0_index = r0_offset + r0_base
        r0_mask = r0_index < r0_numel
        roffset = r0_offset
        rindex = r0_index
        r0_1 = r0_index
        tmp0 = tl.load(in_ptr0 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp1 = tmp0.to(tl.float32)
        tmp2 = tmp1 * tmp1
        tmp3 = tl.broadcast_to(tmp2, [XBLOCK, R0_BLOCK])
        tmp5 = _tmp4 + tmp3
        _tmp4 = tl.where(r0_mask & xmask, tmp5, _tmp4)
    tmp4 = tl.sum(_tmp4, 1)[:, None]
    for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
        r0_index = r0_offset + r0_base
        r0_mask = r0_index < r0_numel
        roffset = r0_offset
        rindex = r0_index
        r0_1 = r0_index
        tmp6 = tl.load(in_ptr0 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
        tmp15 = tl.load(in_ptr1 + (r0_1), r0_mask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp7 = tmp6.to(tl.float32)
        tmp8 = tl.full([1, 1], 4096.0, tl.float32)
        tmp9 = (tmp4 / tmp8)
        tmp10 = tl.full([1, 1], 1e-06, tl.float32)
        tmp11 = tmp9 + tmp10
        tmp12 = libdevice.rsqrt(tmp11)
        tmp13 = tmp7 * tmp12
        tmp14 = tmp13.to(tl.float32)
        tmp16 = tmp14 * tmp15
        tl.store(out_ptr1 + (r0_1 + 4096*x0), tmp16, r0_mask & xmask)
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\mf\cmfcbxtojaojlsurir27ptx2467fp2twcutls24q6jyf5jkcrk7o.py
# Unsorted Source Nodes: [], Original ATen: []
# Source node to ATen node mapping:
triton_red_fused_2 = async_compile.triton('triton_red_fused_2', '''
import triton
import triton.language as tl

from torch._inductor.runtime import triton_helpers, triton_heuristics
from torch._inductor.runtime.triton_helpers import libdevice, math as tl_math
from torch._inductor.runtime.hints import AutotuneHint, ReductionHint, TileHint, DeviceProperties

from torch._dynamo.testing import rand_strided
from torch._C import _cuda_getCurrentRawStream as get_raw_stream
import torch

@triton_heuristics.reduction(
    size_hints={'x': 65536, 'r0_': 128},
    reduction_hint=ReductionHint.DEFAULT,
    filename=__file__,
    triton_meta={'signature': {'in_ptr0': '*fp16', 'out_ptr0': '*fp32', 'out_ptr1': '*fp32', 'xnumel_0': 'i32', 'xnumel_1': 'i32', 'XBLOCK': 'constexpr', 'R0_BLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'enable_fp_fusion': True, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (3,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'SequentialComboKernelGrid', 'combo_grid_meta': {'num_kernels': 2, 'min_blocks': None, 'default_config': None, 'no_x_dim_0': False, 'xnumel_0': None, 'no_x_dim_1': False, 'xnumel_1': None}, 'kernel_name': 'triton_red_fused_2', 'mutated_arg_names': [], 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False}
)
@triton.jit
def triton_red_fused_2(in_ptr0, out_ptr0, out_ptr1, xnumel_0, xnumel_1, XBLOCK : tl.constexpr, R0_BLOCK : tl.constexpr):
    pid = tl.program_id(0)
    num_xblocks_0 = tl.cdiv(xnumel_0, XBLOCK)
    num_xblocks_1 = num_xblocks_0 + tl.cdiv(xnumel_1, XBLOCK)
    if pid < num_xblocks_0:
        pid_offset = pid
        r0_numel = 128
        rnumel = r0_numel
        RBLOCK: tl.constexpr = R0_BLOCK
        xoffset = pid_offset * XBLOCK
        xindex = xoffset + tl.arange(0, XBLOCK)[:, None]
        xmask = xindex < xnumel_0
        r0_base = tl.arange(0, R0_BLOCK)[None, :]
        rbase = r0_base
        x0 = (xindex % 32)
        x1 = xindex // 32
        _tmp4 = tl.full([XBLOCK, R0_BLOCK], 0, tl.float32)
        x3 = xindex
        for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
            r0_index = r0_offset + r0_base
            r0_mask = r0_index < r0_numel
            roffset = r0_offset
            rindex = r0_index
            r0_2 = r0_index
            tmp0 = tl.load(in_ptr0 + (r0_2 + 128*x0 + 6144*x1), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
            tmp1 = tmp0.to(tl.float32)
            tmp2 = tmp1 * tmp1
            tmp3 = tl.broadcast_to(tmp2, [XBLOCK, R0_BLOCK])
            tmp5 = _tmp4 + tmp3
            _tmp4 = tl.where(r0_mask & xmask, tmp5, _tmp4)
        tmp4 = tl.sum(_tmp4, 1)[:, None]
        tl.store(out_ptr0 + (x3), tmp4, xmask)
    elif pid < num_xblocks_1:
        pid_offset = pid - num_xblocks_0
        r0_numel = 128
        rnumel = r0_numel
        RBLOCK: tl.constexpr = R0_BLOCK
        xoffset = pid_offset * XBLOCK
        xindex = xoffset + tl.arange(0, XBLOCK)[:, None]
        xmask = xindex < xnumel_1
        r0_base = tl.arange(0, R0_BLOCK)[None, :]
        rbase = r0_base
        x4 = (xindex % 8)
        x5 = xindex // 8
        _tmp10 = tl.full([XBLOCK, R0_BLOCK], 0, tl.float32)
        x7 = xindex
        for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
            r0_index = r0_offset + r0_base
            r0_mask = r0_index < r0_numel
            roffset = r0_offset
            rindex = r0_index
            r0_6 = r0_index
            tmp6 = tl.load(in_ptr0 + (4096 + r0_6 + 128*x4 + 6144*x5), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
            tmp7 = tmp6.to(tl.float32)
            tmp8 = tmp7 * tmp7
            tmp9 = tl.broadcast_to(tmp8, [XBLOCK, R0_BLOCK])
            tmp11 = _tmp10 + tmp9
            _tmp10 = tl.where(r0_mask & xmask, tmp11, _tmp10)
        tmp10 = tl.sum(_tmp10, 1)[:, None]
        tl.store(out_ptr1 + (x7), tmp10, xmask)
    else:
        pass


def get_args():
    arg_0 = rand_strided((2048, 6144), (6144, 1), device='cuda:0', dtype=torch.float16)
    arg_1 = rand_strided((2048, 32, 1), (32, 1, 65536), device='cuda:0', dtype=torch.float32)
    arg_2 = rand_strided((2048, 8, 1), (8, 1, 16384), device='cuda:0', dtype=torch.float32)
    return arg_0, arg_1, arg_2, 65536, 16384,


def call(args):
    with torch.cuda._DeviceGuard(0):
        torch.cuda.set_device(0)
        stream0 = get_raw_stream(0)
        triton_red_fused_2.run(*args, stream=stream0)


def benchmark_all_configs(args):
    with torch.cuda._DeviceGuard(0):
        torch.cuda.set_device(0)
        return triton_red_fused_2.benchmark_all_configs(*args)


if __name__ == '__main__':
    from torch._inductor.runtime.benchmarking import benchmarker

    args = get_args()
    ms = benchmarker.benchmark(call, fn_args=(args,), device=cuda,rep=40)
    num_gb = 0
    gb_per_s = num_gb / (ms / 1e3)
    print(f"{ms:.3f}ms    {num_gb:.3f}GB    {gb_per_s:.2f}GB/s")
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\rm\crmmoqps4pe7cfzvrdwvcwffolr4yi7mxesp7l6vdbwdjtqr5l3e.py
# Unsorted Source Nodes: [], Original ATen: []
# Source node to ATen node mapping:
triton_poi_fused_3 = async_compile.triton('triton_poi_fused_3', '''
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
    inductor_meta={'grid_type': 'SequentialComboKernelGrid', 'combo_grid_meta': {'num_kernels': 2, 'min_blocks': None, 'default_config': None, 'no_x_dim_0': False, 'xnumel_0': None, 'no_x_dim_1': False, 'xnumel_1': None}, 'kernel_name': 'triton_poi_fused_3', 'mutated_arg_names': [], 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False}
)
@triton.jit
def triton_poi_fused_3(in_ptr0, in_ptr1, in_ptr2, in_ptr3, in_ptr4, in_ptr5, in_ptr6, out_ptr0, out_ptr1, out_ptr2, out_ptr3, xnumel_0, xnumel_1, XBLOCK : tl.constexpr):
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
        triton_poi_fused_3.run(*args, stream=stream0)


def benchmark_all_configs(args):
    with torch.cuda._DeviceGuard(0):
        torch.cuda.set_device(0)
        return triton_poi_fused_3.benchmark_all_configs(*args)


if __name__ == '__main__':
    from torch._inductor.runtime.benchmarking import benchmarker

    args = get_args()
    ms = benchmarker.benchmark(call, fn_args=(args,), device=cuda,rep=40)
    num_gb = 0
    gb_per_s = num_gb / (ms / 1e3)
    print(f"{ms:.3f}ms    {num_gb:.3f}GB    {gb_per_s:.2f}GB/s")
''', device_str='cuda')

async_compile.wait(globals())
del async_compile

import triton
import triton.language as tl
from torch._inductor.runtime.triton_heuristics import start_graph, end_graph
from torch._C import _cuda_getCurrentRawStream as get_raw_stream
with torch.cuda._DeviceGuard(0):
    stream0 = get_raw_stream(0)
stream0 = get_raw_stream(0)
arg13_1 = generate_example_value((3, 2048), (2049, 1), 'cuda:0', torch.int64, 0, (3, 2048))
arg12_1 = generate_example_value((1048576, 128), (128, 1), 'cuda:0', torch.float16, 0, (1048576, 128))
buf6 = generate_example_value((2048, 64), (64, 1), 'cuda:0', torch.float16, 0, (2048, 64))
buf7 = generate_example_value((2048, 64), (64, 1), 'cuda:0', torch.float16, 0, (2048, 64))
with torch.cuda._DeviceGuard(0):
    triton_poi_fused_0.run(arg13_1, arg12_1, buf6, buf7, 2049, 131072, stream=stream0)
del arg13_1, arg12_1

stream0 = get_raw_stream(0)
arg1_1 = generate_example_value((2048, 4096), (4096, 1), 'cuda:0', torch.float16, 0, (2048, 4096))
arg0_1 = generate_example_value((4096,), (1,), 'cuda:0', torch.float16, 0, (4096,))
buf1 = generate_example_value((2048, 4096), (4096, 1), 'cuda:0', torch.float16, 0, (2048, 4096))
with torch.cuda._DeviceGuard(0):
    triton_red_fused_1.run(arg1_1, arg0_1, buf1, 2048, 4096, stream=stream0)
del arg1_1, arg0_1, buf1

stream0 = get_raw_stream(0)
buf3 = generate_example_value((2048, 6144), (6144, 1), 'cuda:0', torch.float16, 0, (2048, 6144))
buf4 = generate_example_value((2048, 32, 1), (32, 1, 65536), 'cuda:0', torch.float32, 0, (2048, 32, 1))
buf5 = generate_example_value((2048, 8, 1), (8, 1, 16384), 'cuda:0', torch.float32, 0, (2048, 8, 1))
with torch.cuda._DeviceGuard(0):
    triton_red_fused_2.run(buf3, buf4, buf5, 65536, 16384, stream=stream0)

stream0 = get_raw_stream(0)
arg11_1 = generate_example_value((128,), (1,), 'cuda:0', torch.float16, 0, (128,))
arg10_1 = generate_example_value((128,), (1,), 'cuda:0', torch.float16, 0, (128,))
buf8 = generate_example_value((2048, 8, 64), (1024, 128, 1), 'cuda:0', torch.float16, 0, (2048, 8, 64))
buf9 = generate_example_value((2048, 8, 64), (1024, 128, 1), 'cuda:0', torch.float16, 0, (2048, 8, 64))
buf11 = generate_example_value((2048, 32, 64), (4096, 128, 1), 'cuda:0', torch.float16, 0, (2048, 32, 64))
buf12 = generate_example_value((2048, 32, 64), (4096, 128, 1), 'cuda:0', torch.float16, 0, (2048, 32, 64))
with torch.cuda._DeviceGuard(0):
    triton_poi_fused_3.run(buf3, buf5, arg11_1, buf6, buf7, buf4, arg10_1, buf8, buf9, buf11, buf12, 1048576, 4194304, stream=stream0)
del buf6, buf7, buf3, buf4, buf5, arg11_1, arg10_1, buf8, buf9, buf11, buf12

"""
# AOT ID: ['0_inference']
from ctypes import c_void_p, c_long, c_int
import torch
import math
import random
import os
import tempfile
from math import inf, nan
from cmath import nanj
from torch._inductor.hooks import run_intermediate_hooks
from torch._inductor.utils import maybe_profile
from torch._inductor.codegen.memory_planning import _align as align
from torch import device, empty_strided
from torch._inductor.async_compile import AsyncCompile
from torch._inductor.select_algorithm import extern_kernels
import triton
import triton.language as tl
from torch._inductor.runtime.triton_heuristics import start_graph, end_graph
from torch._C import _cuda_getCurrentRawStream as get_raw_stream

aten = torch.ops.aten
inductor_ops = torch.ops.inductor
_quantized = torch.ops._quantized
assert_size_stride = torch._C._dynamo.guards.assert_size_stride
assert_alignment = torch._C._dynamo.guards.assert_alignment
empty_strided_cpu = torch._C._dynamo.guards._empty_strided_cpu
empty_strided_cpu_pinned = torch._C._dynamo.guards._empty_strided_cpu_pinned
empty_strided_cuda = torch._C._dynamo.guards._empty_strided_cuda
empty_strided_xpu = torch._C._dynamo.guards._empty_strided_xpu
empty_strided_mtia = torch._C._dynamo.guards._empty_strided_mtia
reinterpret_tensor = torch._C._dynamo.guards._reinterpret_tensor
alloc_from_pool = torch.ops.inductor._alloc_from_pool
async_compile = AsyncCompile()
empty_strided_p2p = torch._C._distributed_c10d._SymmetricMemory.empty_strided_p2p


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\wf\cwfkr34rpzltukx5wpsiiashomla3aehmv3pq3b753lnj3qooxlt.py
# Unsorted Source Nodes: [], Original ATen: []
# Source node to ATen node mapping:
triton_poi_fused_0 = async_compile.triton('triton_poi_fused_0', '''
import triton
import triton.language as tl

from torch._inductor.runtime import triton_helpers, triton_heuristics
from torch._inductor.runtime.triton_helpers import libdevice, math as tl_math
from torch._inductor.runtime.hints import AutotuneHint, ReductionHint, TileHint, DeviceProperties
triton_helpers.set_driver_to_gpu()

@triton_heuristics.pointwise(
    size_hints={'x': 131072}, 
    filename=__file__,
    triton_meta={'signature': {'in_ptr0': '*i64', 'in_ptr1': '*fp16', 'out_ptr0': '*fp16', 'out_ptr1': '*fp16', 'ks0': 'i64', 'xnumel': 'i32', 'XBLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'native_matmul': False, 'enable_fp_fusion': True, 'launch_pdl': False, 'disable_ftz': False, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (3,): [['tt.divisibility', 16]], (5,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'Grid1D', 'autotune_hints': set(), 'kernel_name': 'triton_poi_fused_0', 'mutated_arg_names': [], 'optimize_mem': True, 'no_x_dim': False, 'atomic_add_found': False, 'num_load': 3, 'num_store': 2, 'num_reduction': 0, 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False},
    min_elem_per_thread=0
)
@triton.jit
def triton_poi_fused_0(in_ptr0, in_ptr1, out_ptr0, out_ptr1, ks0, xnumel, XBLOCK : tl.constexpr):
    xoffset = tl.program_id(0) * XBLOCK
    xindex = xoffset + tl.arange(0, XBLOCK)[:]
    xmask = xindex < xnumel
    x0 = (xindex % 64)
    x1 = xindex // 64
    x2 = xindex
    tmp30 = tl.load(in_ptr0 + (x1), xmask, eviction_policy='evict_last')
    tmp0 = x0
    tmp1 = tl.full([1], 2, tl.int64)
    tmp2 = tmp0 >= tmp1
    tmp3 = tl.full([1], 60, tl.int64)
    tmp4 = tmp0 < tmp3
    tmp5 = (((-2) + x0) % 3)
    tmp6 = tl.full([1], 0, tl.int64)
    tmp7 = tmp5 == tmp6
    tmp8 = tmp2 & tmp4
    tmp9 = tmp8 & tmp7
    tmp10 = tl.load(in_ptr0 + (x1 + 2*ks0), tmp9 & xmask, eviction_policy='evict_last', other=0.0)
    tmp11 = tl.full([XBLOCK], 1048576, tl.int32)
    tmp12 = tmp10 + tmp11
    tmp13 = tmp10 < 0
    tmp14 = tl.where(tmp13, tmp12, tmp10)
    tl.device_assert(((0 <= tl.broadcast_to(tmp14, [XBLOCK])) & (tl.broadcast_to(tmp14, [XBLOCK]) < 1048576)) | ~(tmp9 & xmask), "index out of bounds: 0 <= tl.broadcast_to(tmp14, [XBLOCK]) < 1048576")
    tmp16 = tl.load(in_ptr1 + (2 + 3*(triton_helpers.div_floor_integer((-2) + x0,  3)) + 128*tmp14), tmp9 & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp17 = tl.full([1], 1, tl.int64)
    tmp18 = tmp0 >= tmp17
    tmp19 = (((-1) + x0) % 3)
    tmp20 = tmp19 == tmp6
    tmp21 = tmp18 & tmp4
    tmp22 = tmp21 & tmp20
    tmp23 = tl.load(in_ptr0 + (ks0 + x1), tmp22 & xmask, eviction_policy='evict_last', other=0.0)
    tmp24 = tl.full([XBLOCK], 1048576, tl.int32)
    tmp25 = tmp23 + tmp24
    tmp26 = tmp23 < 0
    tmp27 = tl.where(tmp26, tmp25, tmp23)
    tl.device_assert(((0 <= tl.broadcast_to(tmp27, [XBLOCK])) & (tl.broadcast_to(tmp27, [XBLOCK]) < 1048576)) | ~(tmp22 & xmask), "index out of bounds: 0 <= tl.broadcast_to(tmp27, [XBLOCK]) < 1048576")
    tmp29 = tl.load(in_ptr1 + (1 + 3*(triton_helpers.div_floor_integer((-1) + x0,  3)) + 128*tmp27), tmp22 & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp31 = tl.full([XBLOCK], 1048576, tl.int32)
    tmp32 = tmp30 + tmp31
    tmp33 = tmp30 < 0
    tmp34 = tl.where(tmp33, tmp32, tmp30)
    tl.device_assert(((0 <= tmp34) & (tmp34 < 1048576)) | ~(xmask), "index out of bounds: 0 <= tmp34 < 1048576")
    tmp36 = tl.load(in_ptr1 + (x0 + 128*tmp34), xmask).to(tl.float32)
    tmp37 = tl.where(tmp22, tmp29, tmp36)
    tmp38 = tl.where(tmp9, tmp16, tmp37)
    tmp39 = tl.load(in_ptr1 + (66 + 3*(triton_helpers.div_floor_integer((-2) + x0,  3)) + 128*tmp14), tmp9 & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp40 = tl.load(in_ptr1 + (65 + 3*(triton_helpers.div_floor_integer((-1) + x0,  3)) + 128*tmp27), tmp22 & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp41 = tl.load(in_ptr1 + (64 + x0 + 128*tmp34), xmask).to(tl.float32)
    tmp42 = tl.where(tmp22, tmp40, tmp41)
    tmp43 = tl.where(tmp9, tmp39, tmp42)
    tl.store(out_ptr0 + (x2), tmp38, xmask)
    tl.store(out_ptr1 + (x2), tmp43, xmask)
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\3t\c3tt67dhakpz3wscxctpfv7lwou2oknsvw2k5x3qqkvfdsmcbllv.py
# Unsorted Source Nodes: [], Original ATen: []
# Source node to ATen node mapping:
triton_red_fused_1 = async_compile.triton('triton_red_fused_1', '''
import triton
import triton.language as tl

from torch._inductor.runtime import triton_helpers, triton_heuristics
from torch._inductor.runtime.triton_helpers import libdevice, math as tl_math
from torch._inductor.runtime.hints import AutotuneHint, ReductionHint, TileHint, DeviceProperties
triton_helpers.set_driver_to_gpu()

@triton_heuristics.reduction(
    size_hints={'x': 2048, 'r0_': 4096},
    reduction_hint=ReductionHint.INNER,
    filename=__file__,
    triton_meta={'signature': {'in_ptr0': '*fp16', 'in_ptr1': '*fp16', 'out_ptr1': '*fp16', 'xnumel': 'i32', 'r0_numel': 'i32', 'XBLOCK': 'constexpr', 'R0_BLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'native_matmul': False, 'enable_fp_fusion': True, 'launch_pdl': False, 'disable_ftz': False, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (4,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'Grid1D', 'autotune_hints': set(), 'kernel_name': 'triton_red_fused_1', 'mutated_arg_names': [], 'optimize_mem': True, 'no_x_dim': False, 'atomic_add_found': False, 'num_load': 3, 'num_store': 1, 'num_reduction': 1, 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False, 'add_persistent_rblock': True}
)
@triton.jit
def triton_red_fused_1(in_ptr0, in_ptr1, out_ptr1, xnumel, r0_numel, XBLOCK : tl.constexpr, R0_BLOCK : tl.constexpr):
    r0_numel = 4096
    rnumel = r0_numel
    RBLOCK: tl.constexpr = R0_BLOCK
    xoffset = tl.program_id(0) * XBLOCK
    xindex = xoffset + tl.arange(0, XBLOCK)[:, None]
    xmask = xindex < xnumel
    r0_base = tl.arange(0, R0_BLOCK)[None, :]
    rbase = r0_base
    x0 = xindex
    _tmp4 = tl.full([XBLOCK, R0_BLOCK], 0, tl.float32)
    for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
        r0_index = r0_offset + r0_base
        r0_mask = r0_index < r0_numel
        roffset = r0_offset
        rindex = r0_index
        r0_1 = r0_index
        tmp0 = tl.load(in_ptr0 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp1 = tmp0.to(tl.float32)
        tmp2 = tmp1 * tmp1
        tmp3 = tl.broadcast_to(tmp2, [XBLOCK, R0_BLOCK])
        tmp5 = _tmp4 + tmp3
        _tmp4 = tl.where(r0_mask & xmask, tmp5, _tmp4)
    tmp4 = tl.sum(_tmp4, 1)[:, None]
    for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
        r0_index = r0_offset + r0_base
        r0_mask = r0_index < r0_numel
        roffset = r0_offset
        rindex = r0_index
        r0_1 = r0_index
        tmp6 = tl.load(in_ptr0 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
        tmp15 = tl.load(in_ptr1 + (r0_1), r0_mask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp7 = tmp6.to(tl.float32)
        tmp8 = tl.full([1, 1], 4096.0, tl.float32)
        tmp9 = (tmp4 / tmp8)
        tmp10 = tl.full([1, 1], 1e-06, tl.float32)
        tmp11 = tmp9 + tmp10
        tmp12 = libdevice.rsqrt(tmp11)
        tmp13 = tmp7 * tmp12
        tmp14 = tmp13.to(tl.float32)
        tmp16 = tmp14 * tmp15
        tl.store(out_ptr1 + (r0_1 + 4096*x0), tmp16, r0_mask & xmask)
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\mf\cmfcbxtojaojlsurir27ptx2467fp2twcutls24q6jyf5jkcrk7o.py
# Unsorted Source Nodes: [], Original ATen: []
# Source node to ATen node mapping:
triton_red_fused_2 = async_compile.triton('triton_red_fused_2', '''
import triton
import triton.language as tl

from torch._inductor.runtime import triton_helpers, triton_heuristics
from torch._inductor.runtime.triton_helpers import libdevice, math as tl_math
from torch._inductor.runtime.hints import AutotuneHint, ReductionHint, TileHint, DeviceProperties

from torch._dynamo.testing import rand_strided
from torch._C import _cuda_getCurrentRawStream as get_raw_stream
import torch

@triton_heuristics.reduction(
    size_hints={'x': 65536, 'r0_': 128},
    reduction_hint=ReductionHint.DEFAULT,
    filename=__file__,
    triton_meta={'signature': {'in_ptr0': '*fp16', 'out_ptr0': '*fp32', 'out_ptr1': '*fp32', 'xnumel_0': 'i32', 'xnumel_1': 'i32', 'XBLOCK': 'constexpr', 'R0_BLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'enable_fp_fusion': True, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (3,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'SequentialComboKernelGrid', 'combo_grid_meta': {'num_kernels': 2, 'min_blocks': None, 'default_config': None, 'no_x_dim_0': False, 'xnumel_0': None, 'no_x_dim_1': False, 'xnumel_1': None}, 'kernel_name': 'triton_red_fused_2', 'mutated_arg_names': [], 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False}
)
@triton.jit
def triton_red_fused_2(in_ptr0, out_ptr0, out_ptr1, xnumel_0, xnumel_1, XBLOCK : tl.constexpr, R0_BLOCK : tl.constexpr):
    pid = tl.program_id(0)
    num_xblocks_0 = tl.cdiv(xnumel_0, XBLOCK)
    num_xblocks_1 = num_xblocks_0 + tl.cdiv(xnumel_1, XBLOCK)
    if pid < num_xblocks_0:
        pid_offset = pid
        r0_numel = 128
        rnumel = r0_numel
        RBLOCK: tl.constexpr = R0_BLOCK
        xoffset = pid_offset * XBLOCK
        xindex = xoffset + tl.arange(0, XBLOCK)[:, None]
        xmask = xindex < xnumel_0
        r0_base = tl.arange(0, R0_BLOCK)[None, :]
        rbase = r0_base
        x0 = (xindex % 32)
        x1 = xindex // 32
        _tmp4 = tl.full([XBLOCK, R0_BLOCK], 0, tl.float32)
        x3 = xindex
        for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
            r0_index = r0_offset + r0_base
            r0_mask = r0_index < r0_numel
            roffset = r0_offset
            rindex = r0_index
            r0_2 = r0_index
            tmp0 = tl.load(in_ptr0 + (r0_2 + 128*x0 + 6144*x1), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
            tmp1 = tmp0.to(tl.float32)
            tmp2 = tmp1 * tmp1
            tmp3 = tl.broadcast_to(tmp2, [XBLOCK, R0_BLOCK])
            tmp5 = _tmp4 + tmp3
            _tmp4 = tl.where(r0_mask & xmask, tmp5, _tmp4)
        tmp4 = tl.sum(_tmp4, 1)[:, None]
        tl.store(out_ptr0 + (x3), tmp4, xmask)
    elif pid < num_xblocks_1:
        pid_offset = pid - num_xblocks_0
        r0_numel = 128
        rnumel = r0_numel
        RBLOCK: tl.constexpr = R0_BLOCK
        xoffset = pid_offset * XBLOCK
        xindex = xoffset + tl.arange(0, XBLOCK)[:, None]
        xmask = xindex < xnumel_1
        r0_base = tl.arange(0, R0_BLOCK)[None, :]
        rbase = r0_base
        x4 = (xindex % 8)
        x5 = xindex // 8
        _tmp10 = tl.full([XBLOCK, R0_BLOCK], 0, tl.float32)
        x7 = xindex
        for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
            r0_index = r0_offset + r0_base
            r0_mask = r0_index < r0_numel
            roffset = r0_offset
            rindex = r0_index
            r0_6 = r0_index
            tmp6 = tl.load(in_ptr0 + (4096 + r0_6 + 128*x4 + 6144*x5), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
            tmp7 = tmp6.to(tl.float32)
            tmp8 = tmp7 * tmp7
            tmp9 = tl.broadcast_to(tmp8, [XBLOCK, R0_BLOCK])
            tmp11 = _tmp10 + tmp9
            _tmp10 = tl.where(r0_mask & xmask, tmp11, _tmp10)
        tmp10 = tl.sum(_tmp10, 1)[:, None]
        tl.store(out_ptr1 + (x7), tmp10, xmask)
    else:
        pass


def get_args():
    arg_0 = rand_strided((2048, 6144), (6144, 1), device='cuda:0', dtype=torch.float16)
    arg_1 = rand_strided((2048, 32, 1), (32, 1, 65536), device='cuda:0', dtype=torch.float32)
    arg_2 = rand_strided((2048, 8, 1), (8, 1, 16384), device='cuda:0', dtype=torch.float32)
    return arg_0, arg_1, arg_2, 65536, 16384,


def call(args):
    with torch.cuda._DeviceGuard(0):
        torch.cuda.set_device(0)
        stream0 = get_raw_stream(0)
        triton_red_fused_2.run(*args, stream=stream0)


def benchmark_all_configs(args):
    with torch.cuda._DeviceGuard(0):
        torch.cuda.set_device(0)
        return triton_red_fused_2.benchmark_all_configs(*args)


if __name__ == '__main__':
    from torch._inductor.runtime.benchmarking import benchmarker

    args = get_args()
    ms = benchmarker.benchmark(call, fn_args=(args,), device=cuda,rep=40)
    num_gb = 0
    gb_per_s = num_gb / (ms / 1e3)
    print(f"{ms:.3f}ms    {num_gb:.3f}GB    {gb_per_s:.2f}GB/s")
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\rm\crmmoqps4pe7cfzvrdwvcwffolr4yi7mxesp7l6vdbwdjtqr5l3e.py
# Unsorted Source Nodes: [], Original ATen: []
# Source node to ATen node mapping:
triton_poi_fused_3 = async_compile.triton('triton_poi_fused_3', '''
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
    inductor_meta={'grid_type': 'SequentialComboKernelGrid', 'combo_grid_meta': {'num_kernels': 2, 'min_blocks': None, 'default_config': None, 'no_x_dim_0': False, 'xnumel_0': None, 'no_x_dim_1': False, 'xnumel_1': None}, 'kernel_name': 'triton_poi_fused_3', 'mutated_arg_names': [], 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False}
)
@triton.jit
def triton_poi_fused_3(in_ptr0, in_ptr1, in_ptr2, in_ptr3, in_ptr4, in_ptr5, in_ptr6, out_ptr0, out_ptr1, out_ptr2, out_ptr3, xnumel_0, xnumel_1, XBLOCK : tl.constexpr):
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
        triton_poi_fused_3.run(*args, stream=stream0)


def benchmark_all_configs(args):
    with torch.cuda._DeviceGuard(0):
        torch.cuda.set_device(0)
        return triton_poi_fused_3.benchmark_all_configs(*args)


if __name__ == '__main__':
    from torch._inductor.runtime.benchmarking import benchmarker

    args = get_args()
    ms = benchmarker.benchmark(call, fn_args=(args,), device=cuda,rep=40)
    num_gb = 0
    gb_per_s = num_gb / (ms / 1e3)
    print(f"{ms:.3f}ms    {num_gb:.3f}GB    {gb_per_s:.2f}GB/s")
''', device_str='cuda')


async_compile.wait(globals())
del async_compile

class Runner:
    def __init__(self, partitions):
        self.partitions = partitions

    def recursively_apply_fns(self, fns):
        new_callables = []
        for fn, c in zip(fns, self.partitions):
            new_callables.append(fn(c))
        self.partitions = new_callables

    def call(self, args):
        arg0_1, arg1_1, arg2_1, arg3_1, arg4_1, arg5_1, arg6_1, arg7_1, arg8_1, arg9_1, arg10_1, arg11_1, arg12_1, arg13_1, arg14_1 = args
        args.clear()
        s59 = arg2_1
        s18 = arg9_1
        s7 = arg14_1
        with torch.cuda._DeviceGuard(0):
            torch.cuda.set_device(0)
            buf6 = empty_strided_cuda((s18, 64), (64, 1), torch.float16)
            buf7 = empty_strided_cuda((s18, 64), (64, 1), torch.float16)
            buf1 = empty_strided_cuda((s18, 4096), (4096, 1), torch.float16)
            # Unsorted Source Nodes: [], Original ATen: []
            triton_poi_fused_0_xnumel = 64*s18
            stream0 = get_raw_stream(0)
            triton_poi_fused_0.run(arg13_1, arg12_1, buf6, buf7, s7, triton_poi_fused_0_xnumel, stream=stream0)
            # Unsorted Source Nodes: [], Original ATen: []
            stream0 = get_raw_stream(0)
            triton_red_fused_1.run(arg1_1, arg0_1, buf1, s18, 4096, stream=stream0)
            del arg0_1
            del arg12_1
            del arg13_1
            del arg1_1
            # Topologically Sorted Source Nodes: [rms_norm_default, marlin_gemm], Original ATen: [vllm_ir.rms_norm, _C.marlin_gemm]
            buf2 = torch.ops._C.marlin_gemm.default(buf1, None, arg3_1, None, arg4_1, None, None, arg5_1, arg6_1, arg7_1, arg8_1, 1125899907892224, s18, 6144, 4096, True, False, True, False)
            del arg3_1
            del arg4_1
            del arg5_1
            del arg6_1
            del arg7_1
            del arg8_1
            del buf1
            buf3 = buf2
            del buf2
            buf4 = empty_strided_cuda((s18, 32, 1), (32, 1, 32*s18), torch.float32)
            buf5 = empty_strided_cuda((s18, 8, 1), (8, 1, 8*s18), torch.float32)
            # Topologically Sorted Source Nodes: [split, view, rms_norm_default_1, view_2, rms_norm_default_2], Original ATen: [aten.split_with_sizes, aten.view, vllm_ir.rms_norm]
            triton_red_fused_2_xnumel_0 = 32*s18
            triton_red_fused_2_xnumel_1 = 8*s18
            stream0 = get_raw_stream(0)
            triton_red_fused_2.run(buf3, buf4, buf5, triton_red_fused_2_xnumel_0, triton_red_fused_2_xnumel_1, stream=stream0)
            buf10 = empty_strided_cuda((s18, 8, 128), (1024, 128, 1), torch.float16)
            buf8 = reinterpret_tensor(buf10, (s18, 8, 64), (1024, 128, 1), 0)  # alias
            buf9 = reinterpret_tensor(buf10, (s18, 8, 64), (1024, 128, 1), 64)  # alias
            buf13 = empty_strided_cuda((s18, 32, 128), (4096, 128, 1), torch.float16)
            buf11 = reinterpret_tensor(buf13, (s18, 32, 64), (4096, 128, 1), 0)  # alias
            buf12 = reinterpret_tensor(buf13, (s18, 32, 64), (4096, 128, 1), 64)  # alias
            # Unsorted Source Nodes: [], Original ATen: []
            triton_poi_fused_3_xnumel_0 = 512*s18
            triton_poi_fused_3_xnumel_1 = 2048*s18
            stream0 = get_raw_stream(0)
            triton_poi_fused_3.run(buf3, buf5, arg11_1, buf6, buf7, buf4, arg10_1, buf8, buf9, buf11, buf12, triton_poi_fused_3_xnumel_0, triton_poi_fused_3_xnumel_1, stream=stream0)
            del arg10_1
            del arg11_1
            del buf4
            del buf5
            del buf6
            del buf7
            buf14 = empty_strided_cuda((s18, 4096), (4096, 1), torch.float16)
        return (buf10, reinterpret_tensor(buf3, (s18, 8, 128), (6144, 128, 1), 5120), buf13, reinterpret_tensor(buf14, (s18, 32, 128), (4096, 128, 1), 0), )

runner = Runner(partitions=[])
call = runner.call
recursively_apply_fns = runner.recursively_apply_fns


def get_args():
    from torch._dynamo.testing import rand_strided
    arg0_1 = rand_strided((4096, ), (1, ), device='cuda:0', dtype=torch.float16)
    arg1_1 = rand_strided((2048, 4096), (4096, 1), device='cuda:0', dtype=torch.float16)
    arg2_1 = 2048
    arg3_1 = rand_strided((256, 12288), (12288, 1), device='cuda:0', dtype=torch.int32)
    arg4_1 = rand_strided((128, 6144), (6144, 1), device='cuda:0', dtype=torch.float16)
    arg5_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg6_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg7_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg8_1 = rand_strided((70, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg9_1 = 2048
    arg10_1 = rand_strided((128, ), (1, ), device='cuda:0', dtype=torch.float16)
    arg11_1 = rand_strided((128, ), (1, ), device='cuda:0', dtype=torch.float16)
    arg12_1 = rand_strided((1048576, 128), (128, 1), device='cuda:0', dtype=torch.float16)
    arg13_1 = rand_strided((3, 2048), (2049, 1), device='cuda:0', dtype=torch.int64)
    arg14_1 = 2049
    return [arg0_1, arg1_1, arg2_1, arg3_1, arg4_1, arg5_1, arg6_1, arg7_1, arg8_1, arg9_1, arg10_1, arg11_1, arg12_1, arg13_1, arg14_1]


def benchmark_compiled_module(args, times=10, repeat=10):
    from torch._inductor.utils import print_performance
    fn = lambda: call(list(args))
    return print_performance(fn, times=times, repeat=repeat)


if __name__ == "__main__":
    from torch._inductor.wrapper_benchmark import compiled_module_main
    args = get_args()
    compiled_module_main('None', lambda times, repeat: benchmark_compiled_module(args, times=times, repeat=repeat))
