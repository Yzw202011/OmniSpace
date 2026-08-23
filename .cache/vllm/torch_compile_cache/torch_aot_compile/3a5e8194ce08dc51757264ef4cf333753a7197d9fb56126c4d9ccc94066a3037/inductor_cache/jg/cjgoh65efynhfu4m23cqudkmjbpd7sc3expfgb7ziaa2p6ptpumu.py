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


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\je\cjeptvlndpg77xcchd73ilr7rywzkyzgqnsuxyjbdb64zkyoye6m.py
# Topologically Sorted Source Nodes: [fused_add_rms_norm_maybe_inplace, marlin_gemm_1], Original ATen: [vllm_ir.fused_add_rms_norm, _C.marlin_gemm]
# Source node to ATen node mapping:
#   fused_add_rms_norm_maybe_inplace => add_tensor_4, add_tensor_5, convert_element_type_default_11, convert_element_type_default_8, convert_element_type_default_9, mean_dim_3, mul_tensor_6, mul_tensor_7, pow_tensor_scalar_3, rsqrt_default_3
#   marlin_gemm_1 => marlin_gemm_1
# Graph fragment:
#   %marlin_gemm : Tensor "f16[s18, 4096][4096, 1]cuda:0" = PlaceHolder[target=marlin_gemm]
#   %arg10_1 : Tensor "f16[s18, 4096][4096, 1]cuda:0" = PlaceHolder[target=arg10_1]
#   %buf2 : Tensor "f32[s18, 1][1, s18]cuda:0" = PlaceHolder[target=buf2]
#   %arg9_1 : Tensor "f16[4096][1]cuda:0" = PlaceHolder[target=arg9_1]
#   %convert_element_type_default_8 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%marlin_gemm, torch.float32), kwargs = {})
#   %convert_element_type_default_9 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%arg10_1, torch.float32), kwargs = {})
#   %add_tensor_4 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=3] = call_function[target=torch.ops.aten.add.Tensor](args = (%convert_element_type_default_8, %convert_element_type_default_9), kwargs = {})
#   %pow_tensor_scalar_3 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.pow.Tensor_Scalar](args = (%add_tensor_4, 2), kwargs = {})
#   %mean_dim_3 : Tensor "f32[s18, 1][1, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mean.dim](args = (%pow_tensor_scalar_3, [-1], True), kwargs = {})
#   %add_tensor_5 : Tensor "f32[s18, 1][1, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.add.Tensor](args = (%mean_dim_3, 1e-06), kwargs = {})
#   %rsqrt_default_3 : Tensor "f32[s18, 1][1, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.rsqrt.default](args = (%add_tensor_5,), kwargs = {})
#   %mul_tensor_6 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mul.Tensor](args = (%add_tensor_4, %rsqrt_default_3), kwargs = {})
#   %convert_element_type_default_11 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%mul_tensor_6, torch.float16), kwargs = {})
#   %mul_tensor_7 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mul.Tensor](args = (%convert_element_type_default_11, %arg9_1), kwargs = {})
#   %marlin_gemm_1 : Tensor "f16[s18, 24576][24576, 1]cuda:0"[num_users=2] = call_function[target=torch.ops._C.marlin_gemm.default](args = (%mul_tensor_7, None, %arg11_1, None, %arg12_1, None, None, %arg13_1, %arg14_1, %arg15_1, %arg16_1, 1125899907892224, %arg8_1, 24576, 4096, True, False, True, False), kwargs = {})
#   return %buf2,%buf3
triton_red_fused_fused_add_rms_norm_marlin_gemm_0 = async_compile.triton('triton_red_fused_fused_add_rms_norm_marlin_gemm_0', '''
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
    triton_meta={'signature': {'in_ptr0': '*fp16', 'in_ptr1': '*fp16', 'in_ptr2': '*fp16', 'out_ptr1': '*fp16', 'xnumel': 'i32', 'r0_numel': 'i32', 'XBLOCK': 'constexpr', 'R0_BLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'native_matmul': False, 'enable_fp_fusion': True, 'launch_pdl': False, 'disable_ftz': False, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (3,): [['tt.divisibility', 16]], (5,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'Grid1D', 'autotune_hints': set(), 'kernel_name': 'triton_red_fused_fused_add_rms_norm_marlin_gemm_0', 'mutated_arg_names': [], 'optimize_mem': True, 'no_x_dim': False, 'atomic_add_found': False, 'num_load': 5, 'num_store': 1, 'num_reduction': 1, 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False, 'add_persistent_rblock': True, 'tiling_scores': {'x': 0, 'r0_': 67117056}}
)
@triton.jit
def triton_red_fused_fused_add_rms_norm_marlin_gemm_0(in_ptr0, in_ptr1, in_ptr2, out_ptr1, xnumel, r0_numel, XBLOCK : tl.constexpr, R0_BLOCK : tl.constexpr):
    r0_numel = 4096
    rnumel = r0_numel
    RBLOCK: tl.constexpr = R0_BLOCK
    xoffset = tl.program_id(0) * XBLOCK
    xindex = xoffset + tl.arange(0, XBLOCK)[:, None]
    xmask = xindex < xnumel
    r0_base = tl.arange(0, R0_BLOCK)[None, :]
    rbase = r0_base
    x0 = xindex
    _tmp7 = tl.full([XBLOCK, R0_BLOCK], 0, tl.float32)
    for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
        r0_index = r0_offset + r0_base
        r0_mask = r0_index < r0_numel
        roffset = r0_offset
        rindex = r0_index
        r0_1 = r0_index
        tmp0 = tl.load(in_ptr0 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp2 = tl.load(in_ptr1 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp1 = tmp0.to(tl.float32)
        tmp3 = tmp2.to(tl.float32)
        tmp4 = tmp1 + tmp3
        tmp5 = tmp4 * tmp4
        tmp6 = tl.broadcast_to(tmp5, [XBLOCK, R0_BLOCK])
        tmp8 = _tmp7 + tmp6
        _tmp7 = tl.where(r0_mask & xmask, tmp8, _tmp7)
    tmp7 = tl.sum(_tmp7, 1)[:, None]
    for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
        r0_index = r0_offset + r0_base
        r0_mask = r0_index < r0_numel
        roffset = r0_offset
        rindex = r0_index
        r0_1 = r0_index
        tmp9 = tl.load(in_ptr0 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
        tmp11 = tl.load(in_ptr1 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
        tmp21 = tl.load(in_ptr2 + (r0_1), r0_mask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp10 = tmp9.to(tl.float32)
        tmp12 = tmp11.to(tl.float32)
        tmp13 = tmp10 + tmp12
        tmp14 = tl.full([1, 1], 4096.0, tl.float32)
        tmp15 = (tmp7 / tmp14)
        tmp16 = tl.full([1, 1], 1e-06, tl.float32)
        tmp17 = tmp15 + tmp16
        tmp18 = libdevice.rsqrt(tmp17)
        tmp19 = tmp13 * tmp18
        tmp20 = tmp19.to(tl.float32)
        tmp22 = tmp20 * tmp21
        tl.store(out_ptr1 + (r0_1 + 4096*x0), tmp22, r0_mask & xmask)
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\x5\cx5ds5nwuvqfkdewj4nbuiiqzhsrewc6zyowysopskecfzi2lemy.py
# Topologically Sorted Source Nodes: [getitem_2, silu, getitem_3, mul, marlin_gemm_2], Original ATen: [aten.slice, aten.silu, aten.mul, _C.marlin_gemm]
# Source node to ATen node mapping:
#   getitem_2 => slice_1
#   getitem_3 => slice_2
#   marlin_gemm_2 => marlin_gemm_2
#   mul => mul_36
#   silu => add_30, convert_element_type, convert_element_type_1, div, exp, neg
# Graph fragment:
#   %marlin_gemm_1 : Tensor "f16[s18, 24576][24576, 1]cuda:0" = PlaceHolder[target=marlin_gemm_1]
#   %slice_1 : Tensor "f16[s18, 12288][24576, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%marlin_gemm_1, 1, 0, 12288), kwargs = {})
#   %convert_element_type : Tensor "f32[s18, 12288][12288, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%slice_1, torch.float32), kwargs = {})
#   %neg : Tensor "f32[s18, 12288][12288, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.neg.default](args = (%convert_element_type,), kwargs = {})
#   %exp : Tensor "f32[s18, 12288][12288, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.exp.default](args = (%neg,), kwargs = {})
#   %add_30 : Tensor "f32[s18, 12288][12288, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.add.Tensor](args = (%exp, 1), kwargs = {})
#   %div : Tensor "f32[s18, 12288][12288, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.div.Tensor](args = (%convert_element_type, %add_30), kwargs = {})
#   %convert_element_type_1 : Tensor "f16[s18, 12288][12288, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%div, torch.float16), kwargs = {})
#   %slice_2 : Tensor "f16[s18, 12288][24576, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%marlin_gemm_1, 1, 12288, 9223372036854775807), kwargs = {})
#   %mul_36 : Tensor "f16[s18, 12288][12288, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mul.Tensor](args = (%convert_element_type_1, %slice_2), kwargs = {})
#   %marlin_gemm_2 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops._C.marlin_gemm.default](args = (%mul_36, None, %arg17_1, None, %arg18_1, None, None, %arg19_1, %arg20_1, %arg21_1, %arg22_1, 1125899907892224, %arg8_1, 4096, 12288, True, False, True, False), kwargs = {})
#   return %buf6
triton_poi_fused_marlin_gemm_mul_silu_slice_1 = async_compile.triton('triton_poi_fused_marlin_gemm_mul_silu_slice_1', '''
import triton
import triton.language as tl

from torch._inductor.runtime import triton_helpers, triton_heuristics
from torch._inductor.runtime.triton_helpers import libdevice, math as tl_math
from torch._inductor.runtime.hints import AutotuneHint, ReductionHint, TileHint, DeviceProperties
triton_helpers.set_driver_to_gpu()

@triton_heuristics.pointwise(
    size_hints={'x': 33554432}, 
    filename=__file__,
    triton_meta={'signature': {'in_ptr0': '*fp16', 'out_ptr0': '*fp16', 'xnumel': 'i32', 'XBLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'native_matmul': False, 'enable_fp_fusion': True, 'launch_pdl': False, 'disable_ftz': False, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'Grid1D', 'autotune_hints': set(), 'kernel_name': 'triton_poi_fused_marlin_gemm_mul_silu_slice_1', 'mutated_arg_names': [], 'optimize_mem': True, 'no_x_dim': False, 'atomic_add_found': False, 'num_load': 2, 'num_store': 1, 'num_reduction': 0, 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False, 'tiling_scores': {'x': 201326592}},
    min_elem_per_thread=0
)
@triton.jit
def triton_poi_fused_marlin_gemm_mul_silu_slice_1(in_ptr0, out_ptr0, xnumel, XBLOCK : tl.constexpr):
    xoffset = tl.program_id(0) * XBLOCK
    xindex = xoffset + tl.arange(0, XBLOCK)[:]
    xmask = tl.full([XBLOCK], True, tl.int1)[:]
    x0 = (xindex % 12288)
    x1 = xindex // 12288
    x2 = xindex
    tmp0 = tl.load(in_ptr0 + (x0 + 24576*x1), None).to(tl.float32)
    tmp8 = tl.load(in_ptr0 + (12288 + x0 + 24576*x1), None).to(tl.float32)
    tmp1 = tmp0.to(tl.float32)
    tmp2 = -tmp1
    tmp3 = libdevice.exp(tmp2)
    tmp4 = tl.full([1], 1.0, tl.float32)
    tmp5 = tmp3 + tmp4
    tmp6 = (tmp1 / tmp5)
    tmp7 = tmp6.to(tl.float32)
    tmp9 = tmp7 * tmp8
    tl.store(out_ptr0 + (x2), tmp9, None)
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\bc\cbcyf27fwc6mpc4nf6lmeda4s7bpxkpr2klllgrcd5zgv57evu45.py
# Topologically Sorted Source Nodes: [fused_add_rms_norm_maybe_inplace, fused_add_rms_norm_maybe_inplace_1, marlin_gemm_3], Original ATen: [vllm_ir.fused_add_rms_norm, _C.marlin_gemm]
# Source node to ATen node mapping:
#   fused_add_rms_norm_maybe_inplace => add_tensor_4, convert_element_type_default_10, convert_element_type_default_8, convert_element_type_default_9
#   fused_add_rms_norm_maybe_inplace_1 => add_tensor_2, add_tensor_3, convert_element_type_default_4, convert_element_type_default_5, convert_element_type_default_6, convert_element_type_default_7, mean_dim_2, mul_tensor_4, mul_tensor_5, pow_tensor_scalar_2, rsqrt_default_2
#   marlin_gemm_3 => marlin_gemm_3
# Graph fragment:
#   %marlin_gemm_2 : Tensor "f16[s18, 4096][4096, 1]cuda:0" = PlaceHolder[target=marlin_gemm_2]
#   %marlin_gemm : Tensor "f16[s18, 4096][4096, 1]cuda:0" = PlaceHolder[target=marlin_gemm]
#   %arg10_1 : Tensor "f16[s18, 4096][4096, 1]cuda:0" = PlaceHolder[target=arg10_1]
#   %buf10 : Tensor "f32[s18, 1][1, s18]cuda:0" = PlaceHolder[target=buf10]
#   %arg23_1 : Tensor "f16[4096][1]cuda:0" = PlaceHolder[target=arg23_1]
#   %convert_element_type_default_8 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%marlin_gemm, torch.float32), kwargs = {})
#   %convert_element_type_default_9 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%arg10_1, torch.float32), kwargs = {})
#   %add_tensor_4 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=3] = call_function[target=torch.ops.aten.add.Tensor](args = (%convert_element_type_default_8, %convert_element_type_default_9), kwargs = {})
#   %convert_element_type_default_10 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%add_tensor_4, torch.float16), kwargs = {})
#   %convert_element_type_default_4 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%marlin_gemm_2, torch.float32), kwargs = {})
#   %convert_element_type_default_5 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%convert_element_type_default_10, torch.float32), kwargs = {})
#   %add_tensor_2 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=3] = call_function[target=torch.ops.aten.add.Tensor](args = (%convert_element_type_default_4, %convert_element_type_default_5), kwargs = {})
#   %convert_element_type_default_6 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%add_tensor_2, torch.float16), kwargs = {})
#   %pow_tensor_scalar_2 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.pow.Tensor_Scalar](args = (%add_tensor_2, 2), kwargs = {})
#   %mean_dim_2 : Tensor "f32[s18, 1][1, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mean.dim](args = (%pow_tensor_scalar_2, [-1], True), kwargs = {})
#   %add_tensor_3 : Tensor "f32[s18, 1][1, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.add.Tensor](args = (%mean_dim_2, 1e-06), kwargs = {})
#   %rsqrt_default_2 : Tensor "f32[s18, 1][1, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.rsqrt.default](args = (%add_tensor_3,), kwargs = {})
#   %mul_tensor_4 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mul.Tensor](args = (%add_tensor_2, %rsqrt_default_2), kwargs = {})
#   %convert_element_type_default_7 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%mul_tensor_4, torch.float16), kwargs = {})
#   %mul_tensor_5 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mul.Tensor](args = (%convert_element_type_default_7, %arg23_1), kwargs = {})
#   %marlin_gemm_3 : Tensor "f16[s18, 6144][6144, 1]cuda:0"[num_users=1] = call_function[target=torch.ops._C.marlin_gemm.default](args = (%mul_tensor_5, None, %arg24_1, None, %arg25_1, None, None, %arg26_1, %arg27_1, %arg28_1, %arg29_1, 1125899907892224, %arg8_1, 6144, 4096, True, False, True, False), kwargs = {})
#   return %buf10,%convert_element_type_default_6,%buf11
triton_red_fused_fused_add_rms_norm_marlin_gemm_2 = async_compile.triton('triton_red_fused_fused_add_rms_norm_marlin_gemm_2', '''
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
    triton_meta={'signature': {'in_ptr0': '*fp16', 'in_ptr1': '*fp16', 'in_ptr2': '*fp16', 'in_ptr3': '*fp16', 'out_ptr1': '*fp16', 'out_ptr2': '*fp16', 'xnumel': 'i32', 'r0_numel': 'i32', 'XBLOCK': 'constexpr', 'R0_BLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'native_matmul': False, 'enable_fp_fusion': True, 'launch_pdl': False, 'disable_ftz': False, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (3,): [['tt.divisibility', 16]], (4,): [['tt.divisibility', 16]], (5,): [['tt.divisibility', 16]], (7,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'Grid1D', 'autotune_hints': set(), 'kernel_name': 'triton_red_fused_fused_add_rms_norm_marlin_gemm_2', 'mutated_arg_names': [], 'optimize_mem': True, 'no_x_dim': False, 'atomic_add_found': False, 'num_load': 7, 'num_store': 2, 'num_reduction': 1, 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False, 'add_persistent_rblock': True, 'tiling_scores': {'x': 0, 'r0_': 117448704}}
)
@triton.jit
def triton_red_fused_fused_add_rms_norm_marlin_gemm_2(in_ptr0, in_ptr1, in_ptr2, in_ptr3, out_ptr1, out_ptr2, xnumel, r0_numel, XBLOCK : tl.constexpr, R0_BLOCK : tl.constexpr):
    r0_numel = 4096
    rnumel = r0_numel
    RBLOCK: tl.constexpr = R0_BLOCK
    xoffset = tl.program_id(0) * XBLOCK
    xindex = xoffset + tl.arange(0, XBLOCK)[:, None]
    xmask = xindex < xnumel
    r0_base = tl.arange(0, R0_BLOCK)[None, :]
    rbase = r0_base
    x0 = xindex
    _tmp12 = tl.full([XBLOCK, R0_BLOCK], 0, tl.float32)
    for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
        r0_index = r0_offset + r0_base
        r0_mask = r0_index < r0_numel
        roffset = r0_offset
        rindex = r0_index
        r0_1 = r0_index
        tmp0 = tl.load(in_ptr0 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp2 = tl.load(in_ptr1 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp4 = tl.load(in_ptr2 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp1 = tmp0.to(tl.float32)
        tmp3 = tmp2.to(tl.float32)
        tmp5 = tmp4.to(tl.float32)
        tmp6 = tmp3 + tmp5
        tmp7 = tmp6.to(tl.float32)
        tmp8 = tmp7.to(tl.float32)
        tmp9 = tmp1 + tmp8
        tmp10 = tmp9 * tmp9
        tmp11 = tl.broadcast_to(tmp10, [XBLOCK, R0_BLOCK])
        tmp13 = _tmp12 + tmp11
        _tmp12 = tl.where(r0_mask & xmask, tmp13, _tmp12)
        tmp14 = tmp9.to(tl.float32)
        tl.store(out_ptr1 + (r0_1 + 4096*x0), tmp14, r0_mask & xmask)
    tmp12 = tl.sum(_tmp12, 1)[:, None]
    for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
        r0_index = r0_offset + r0_base
        r0_mask = r0_index < r0_numel
        roffset = r0_offset
        rindex = r0_index
        r0_1 = r0_index
        tmp15 = tl.load(in_ptr0 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
        tmp17 = tl.load(in_ptr1 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
        tmp19 = tl.load(in_ptr2 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
        tmp32 = tl.load(in_ptr3 + (r0_1), r0_mask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp16 = tmp15.to(tl.float32)
        tmp18 = tmp17.to(tl.float32)
        tmp20 = tmp19.to(tl.float32)
        tmp21 = tmp18 + tmp20
        tmp22 = tmp21.to(tl.float32)
        tmp23 = tmp22.to(tl.float32)
        tmp24 = tmp16 + tmp23
        tmp25 = tl.full([1, 1], 4096.0, tl.float32)
        tmp26 = (tmp12 / tmp25)
        tmp27 = tl.full([1, 1], 1e-06, tl.float32)
        tmp28 = tmp26 + tmp27
        tmp29 = libdevice.rsqrt(tmp28)
        tmp30 = tmp24 * tmp29
        tmp31 = tmp30.to(tl.float32)
        tmp33 = tmp31 * tmp32
        tl.store(out_ptr2 + (r0_1 + 4096*x0), tmp33, r0_mask & xmask)
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\dr\cdrtmtpsspz767ej2ffk6cbhihtkazuftq4qtyvi5almlfxdkxqo.py
# Unsorted Source Nodes: [], Original ATen: []
# Source node to ATen node mapping:
triton_red_fused_3 = async_compile.triton('triton_red_fused_3', '''
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
    inductor_meta={'grid_type': 'SequentialComboKernelGrid', 'combo_grid_meta': {'num_kernels': 2, 'min_blocks': None, 'default_config': None, 'no_x_dim_0': False, 'xnumel_0': None, 'no_x_dim_1': False, 'xnumel_1': None}, 'kernel_name': 'triton_red_fused_3', 'mutated_arg_names': [], 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False}
)
@triton.jit
def triton_red_fused_3(in_ptr0, out_ptr0, out_ptr1, xnumel_0, xnumel_1, XBLOCK : tl.constexpr, R0_BLOCK : tl.constexpr):
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
        triton_red_fused_3.run(*args, stream=stream0)


def benchmark_all_configs(args):
    with torch.cuda._DeviceGuard(0):
        torch.cuda.set_device(0)
        return triton_red_fused_3.benchmark_all_configs(*args)


if __name__ == '__main__':
    from torch._inductor.runtime.benchmarking import benchmarker

    args = get_args()
    ms = benchmarker.benchmark(call, fn_args=(args,), device=cuda,rep=40)
    num_gb = 0
    gb_per_s = num_gb / (ms / 1e3)
    print(f"{ms:.3f}ms    {num_gb:.3f}GB    {gb_per_s:.2f}GB/s")
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\47\c47x3zz7ulolv6xmljvo72tsp4hqzux5chxcjtuhrzov3peubpje.py
# Topologically Sorted Source Nodes: [getitem_9, chunk, getitem_12, clone, setitem, getitem_13, setitem_1, getitem_14, getitem_15, clone_1, setitem_2, getitem_16, setitem_3, getitem_17], Original ATen: [aten.index, aten.split, aten.select, aten.clone, aten.slice, aten.copy]
# Source node to ATen node mapping:
#   chunk => split
#   clone => clone
#   clone_1 => clone_1
#   getitem_12 => select
#   getitem_13 => select_1, slice_3
#   getitem_14 => select_2, slice_6
#   getitem_15 => select_3
#   getitem_16 => select_4, slice_10
#   getitem_17 => select_5, slice_13
#   getitem_9 => index
#   setitem => copy, slice_4
#   setitem_1 => copy_1, slice_8
#   setitem_2 => copy_2, slice_11
#   setitem_3 => copy_3, slice_15
# Graph fragment:
#   %arg33_1 : Tensor "i64[3, s18][s7, 1]cuda:0" = PlaceHolder[target=arg33_1]
#   %arg32_1 : Tensor "f16[1048576, 128][128, 1]cuda:0" = PlaceHolder[target=arg32_1]
#   %index : Tensor "f16[3, s18, 128][128*s18, 128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.index.Tensor](args = (%arg32_1, [%arg33_1]), kwargs = {})
#   %split : [num_users=2] = call_function[target=torch.ops.aten.split.Tensor](args = (%index, 64, -1), kwargs = {})
#   %select : Tensor "f16[s18, 64][128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.select.int](args = (%getitem_7, 0, 0), kwargs = {})
#   %clone : Tensor "f16[s18, 64][64, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.aten.clone.default](args = (%select,), kwargs = {})
#   %slice_4 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%clone, 1, 1, 60, 3), kwargs = {})
#   %select_1 : Tensor "f16[s18, 64][128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.select.int](args = (%getitem_7, 0, 1), kwargs = {})
#   %slice_3 : Tensor "f16[s18, 20][128, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%select_1, 1, 1, 60, 3), kwargs = {})
#   %copy : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.copy.default](args = (%slice_4, %slice_3), kwargs = {})
#   %slice_scatter_default : Tensor "f16[s18, 64][64, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.aten.slice_scatter.default](args = (%clone, %copy, 1, 1, 60, 3), kwargs = {})
#   %slice_8 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%slice_scatter_default, 1, 2, 60, 3), kwargs = {})
#   %select_2 : Tensor "f16[s18, 64][128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.select.int](args = (%getitem_7, 0, 2), kwargs = {})
#   %slice_6 : Tensor "f16[s18, 20][128, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%select_2, 1, 2, 60, 3), kwargs = {})
#   %copy_1 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.copy.default](args = (%slice_8, %slice_6), kwargs = {})
#   %slice_scatter_default_1 : Tensor "f16[s18, 64][64, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.aten.slice_scatter.default](args = (%slice_scatter_default, %copy_1, 1, 2, 60, 3), kwargs = {})
#   %select_3 : Tensor "f16[s18, 64][128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.select.int](args = (%getitem_8, 0, 0), kwargs = {})
#   %clone_1 : Tensor "f16[s18, 64][64, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.aten.clone.default](args = (%select_3,), kwargs = {})
#   %slice_11 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%clone_1, 1, 1, 60, 3), kwargs = {})
#   %select_4 : Tensor "f16[s18, 64][128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.select.int](args = (%getitem_8, 0, 1), kwargs = {})
#   %slice_10 : Tensor "f16[s18, 20][128, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%select_4, 1, 1, 60, 3), kwargs = {})
#   %copy_2 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.copy.default](args = (%slice_11, %slice_10), kwargs = {})
#   %slice_scatter_default_2 : Tensor "f16[s18, 64][64, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.aten.slice_scatter.default](args = (%clone_1, %copy_2, 1, 1, 60, 3), kwargs = {})
#   %slice_15 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%slice_scatter_default_2, 1, 2, 60, 3), kwargs = {})
#   %select_5 : Tensor "f16[s18, 64][128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.select.int](args = (%getitem_8, 0, 2), kwargs = {})
#   %slice_13 : Tensor "f16[s18, 20][128, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%select_5, 1, 2, 60, 3), kwargs = {})
#   %copy_3 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.copy.default](args = (%slice_15, %slice_13), kwargs = {})
#   %slice_scatter_default_3 : Tensor "f16[s18, 64][64, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.aten.slice_scatter.default](args = (%slice_scatter_default_2, %copy_3, 1, 2, 60, 3), kwargs = {})
#   return %slice_scatter_default_1,%slice_scatter_default_3
triton_poi_fused_clone_copy_index_select_slice_split_4 = async_compile.triton('triton_poi_fused_clone_copy_index_select_slice_split_4', '''
import triton
import triton.language as tl

from torch._inductor.runtime import triton_helpers, triton_heuristics
from torch._inductor.runtime.triton_helpers import libdevice, math as tl_math
from torch._inductor.runtime.hints import AutotuneHint, ReductionHint, TileHint, DeviceProperties
triton_helpers.set_driver_to_gpu()

@triton_heuristics.pointwise(
    size_hints={'y': 2048, 'x': 64}, tile_hint=TileHint.DEFAULT,
    filename=__file__,
    triton_meta={'signature': {'in_ptr0': '*i64', 'in_ptr1': '*fp16', 'out_ptr0': '*fp16', 'out_ptr1': '*fp16', 'ks0': 'i64', 'ynumel': 'i32', 'xnumel': 'i32', 'YBLOCK': 'constexpr', 'XBLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'native_matmul': False, 'enable_fp_fusion': True, 'launch_pdl': False, 'disable_ftz': False, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (3,): [['tt.divisibility', 16]], (6,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'Grid2DWithYZOverflow', 'autotune_hints': set(), 'kernel_name': 'triton_poi_fused_clone_copy_index_select_slice_split_4', 'mutated_arg_names': [], 'optimize_mem': True, 'no_x_dim': False, 'atomic_add_found': False, 'num_load': 3, 'num_store': 2, 'num_reduction': 0, 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False, 'tiling_scores': {'y': 49152, 'x': 1048576}},
    min_elem_per_thread=0
)
@triton.jit
def triton_poi_fused_clone_copy_index_select_slice_split_4(in_ptr0, in_ptr1, out_ptr0, out_ptr1, ks0, ynumel, xnumel, YBLOCK : tl.constexpr, XBLOCK : tl.constexpr):
    xnumel = 64
    yoffset = (tl.program_id(1) + tl.program_id(2) * tl.num_programs(1)) * YBLOCK
    yindex = yoffset + tl.arange(0, YBLOCK)[:, None]
    ymask = yindex < ynumel
    xoffset = tl.program_id(0) * XBLOCK
    xindex = xoffset + tl.arange(0, XBLOCK)[None, :]
    xmask = xindex < xnumel
    x1 = xindex
    y0 = yindex
    tmp30 = tl.load(in_ptr0 + (y0), ymask, eviction_policy='evict_last')
    tmp0 = x1
    tmp1 = tl.full([1, 1], 2, tl.int64)
    tmp2 = tmp0 >= tmp1
    tmp3 = tl.full([1, 1], 60, tl.int64)
    tmp4 = tmp0 < tmp3
    tmp5 = (((-2) + x1) % 3)
    tmp6 = tl.full([1, 1], 0, tl.int64)
    tmp7 = tmp5 == tmp6
    tmp8 = tmp2 & tmp4
    tmp9 = tmp8 & tmp7
    tmp10 = tl.load(in_ptr0 + (tl.broadcast_to(y0 + 2*ks0, [YBLOCK, XBLOCK])), tmp9 & xmask & ymask, eviction_policy='evict_last', other=0.0)
    tmp11 = tl.full([1, 1], 1048576, tl.int32)
    tmp12 = tmp10 + tmp11
    tmp13 = tmp10 < 0
    tmp14 = tl.where(tmp13, tmp12, tmp10)
    tl.device_assert(((0 <= tl.broadcast_to(tmp14, [YBLOCK, XBLOCK])) & (tl.broadcast_to(tmp14, [YBLOCK, XBLOCK]) < 1048576)) | ~(tmp9 & xmask & ymask), "index out of bounds: 0 <= tl.broadcast_to(tmp14, [YBLOCK, XBLOCK]) < 1048576")
    tmp16 = tl.load(in_ptr1 + (tl.broadcast_to(2 + 3*(triton_helpers.div_floor_integer((-2) + x1,  3)) + 128*tmp14, [YBLOCK, XBLOCK])), tmp9 & xmask & ymask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp17 = tl.full([1, 1], 1, tl.int64)
    tmp18 = tmp0 >= tmp17
    tmp19 = (((-1) + x1) % 3)
    tmp20 = tmp19 == tmp6
    tmp21 = tmp18 & tmp4
    tmp22 = tmp21 & tmp20
    tmp23 = tl.load(in_ptr0 + (tl.broadcast_to(ks0 + y0, [YBLOCK, XBLOCK])), tmp22 & xmask & ymask, eviction_policy='evict_last', other=0.0)
    tmp24 = tl.full([1, 1], 1048576, tl.int32)
    tmp25 = tmp23 + tmp24
    tmp26 = tmp23 < 0
    tmp27 = tl.where(tmp26, tmp25, tmp23)
    tl.device_assert(((0 <= tl.broadcast_to(tmp27, [YBLOCK, XBLOCK])) & (tl.broadcast_to(tmp27, [YBLOCK, XBLOCK]) < 1048576)) | ~(tmp22 & xmask & ymask), "index out of bounds: 0 <= tl.broadcast_to(tmp27, [YBLOCK, XBLOCK]) < 1048576")
    tmp29 = tl.load(in_ptr1 + (tl.broadcast_to(1 + 3*(triton_helpers.div_floor_integer((-1) + x1,  3)) + 128*tmp27, [YBLOCK, XBLOCK])), tmp22 & xmask & ymask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp31 = tl.full([1, 1], 1048576, tl.int32)
    tmp32 = tmp30 + tmp31
    tmp33 = tmp30 < 0
    tmp34 = tl.where(tmp33, tmp32, tmp30)
    tl.device_assert(((0 <= tmp34) & (tmp34 < 1048576)) | ~(ymask), "index out of bounds: 0 <= tmp34 < 1048576")
    tmp36 = tl.load(in_ptr1 + (x1 + 128*tmp34), xmask & ymask).to(tl.float32)
    tmp37 = tl.where(tmp22, tmp29, tmp36)
    tmp38 = tl.where(tmp9, tmp16, tmp37)
    tmp39 = tl.load(in_ptr1 + (tl.broadcast_to(66 + 3*(triton_helpers.div_floor_integer((-2) + x1,  3)) + 128*tmp14, [YBLOCK, XBLOCK])), tmp9 & xmask & ymask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp40 = tl.load(in_ptr1 + (tl.broadcast_to(65 + 3*(triton_helpers.div_floor_integer((-1) + x1,  3)) + 128*tmp27, [YBLOCK, XBLOCK])), tmp22 & xmask & ymask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp41 = tl.load(in_ptr1 + (64 + x1 + 128*tmp34), xmask & ymask).to(tl.float32)
    tmp42 = tl.where(tmp22, tmp40, tmp41)
    tmp43 = tl.where(tmp9, tmp39, tmp42)
    tl.store(out_ptr0 + (x1 + 64*y0), tmp38, xmask & ymask)
    tl.store(out_ptr1 + (x1 + 64*y0), tmp43, xmask & ymask)
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\el\celjvhvi6dygyyytsps34bnjmcycnzgmev2uevuuln53mnwc2tjq.py
# Unsorted Source Nodes: [], Original ATen: []
# Source node to ATen node mapping:
triton_poi_fused_5 = async_compile.triton('triton_poi_fused_5', '''
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
buf1 = generate_example_value((2048, 4096), (4096, 1), 'cuda:0', torch.float16, 0, (2048, 4096))
arg10_1 = generate_example_value((2048, 4096), (4096, 1), 'cuda:0', torch.float16, 0, (2048, 4096))
arg9_1 = generate_example_value((4096,), (1,), 'cuda:0', torch.float16, 0, (4096,))
buf3 = generate_example_value((2048, 4096), (4096, 1), 'cuda:0', torch.float16, 0, (2048, 4096))
with torch.cuda._DeviceGuard(0):
    triton_red_fused_fused_add_rms_norm_marlin_gemm_0.run(buf1, arg10_1, arg9_1, buf3, 2048, 4096, stream=stream0)
del arg9_1, buf3

stream0 = get_raw_stream(0)
buf5 = generate_example_value((2048, 24576), (24576, 1), 'cuda:0', torch.float16, 0, (2048, 24576))
buf6 = generate_example_value((2048, 12288), (12288, 1), 'cuda:0', torch.float16, 0, (2048, 12288))
with torch.cuda._DeviceGuard(0):
    triton_poi_fused_marlin_gemm_mul_silu_slice_1.run(buf5, buf6, 25165824, stream=stream0)
del buf5, buf6

stream0 = get_raw_stream(0)
buf8 = generate_example_value((2048, 4096), (4096, 1), 'cuda:0', torch.float16, 0, (2048, 4096))
arg23_1 = generate_example_value((4096,), (1,), 'cuda:0', torch.float16, 0, (4096,))
buf9 = generate_example_value((2048, 4096), (4096, 1), 'cuda:0', torch.float16, 0, (2048, 4096))
buf11 = generate_example_value((2048, 4096), (4096, 1), 'cuda:0', torch.float16, 0, (2048, 4096))
with torch.cuda._DeviceGuard(0):
    triton_red_fused_fused_add_rms_norm_marlin_gemm_2.run(buf8, buf1, arg10_1, arg23_1, buf9, buf11, 2048, 4096, stream=stream0)
del buf1, arg10_1, buf8, arg23_1, buf9, buf11

stream0 = get_raw_stream(0)
buf13 = generate_example_value((2048, 6144), (6144, 1), 'cuda:0', torch.float16, 0, (2048, 6144))
buf14 = generate_example_value((2048, 32, 1), (32, 1, 65536), 'cuda:0', torch.float32, 0, (2048, 32, 1))
buf15 = generate_example_value((2048, 8, 1), (8, 1, 16384), 'cuda:0', torch.float32, 0, (2048, 8, 1))
with torch.cuda._DeviceGuard(0):
    triton_red_fused_3.run(buf13, buf14, buf15, 65536, 16384, stream=stream0)

stream0 = get_raw_stream(0)
arg33_1 = generate_example_value((3, 2048), (2049, 1), 'cuda:0', torch.int64, 0, (3, 2048))
arg32_1 = generate_example_value((1048576, 128), (128, 1), 'cuda:0', torch.float16, 0, (1048576, 128))
buf16 = generate_example_value((2048, 64), (64, 1), 'cuda:0', torch.float16, 0, (2048, 64))
buf17 = generate_example_value((2048, 64), (64, 1), 'cuda:0', torch.float16, 0, (2048, 64))
with torch.cuda._DeviceGuard(0):
    triton_poi_fused_clone_copy_index_select_slice_split_4.run(arg33_1, arg32_1, buf16, buf17, 2049, 2048, 64, stream=stream0)
del arg33_1, arg32_1

stream0 = get_raw_stream(0)
arg31_1 = generate_example_value((128,), (1,), 'cuda:0', torch.float16, 0, (128,))
arg30_1 = generate_example_value((128,), (1,), 'cuda:0', torch.float16, 0, (128,))
buf18 = generate_example_value((2048, 8, 64), (1024, 128, 1), 'cuda:0', torch.float16, 0, (2048, 8, 64))
buf19 = generate_example_value((2048, 8, 64), (1024, 128, 1), 'cuda:0', torch.float16, 0, (2048, 8, 64))
buf21 = generate_example_value((2048, 32, 64), (4096, 128, 1), 'cuda:0', torch.float16, 0, (2048, 32, 64))
buf22 = generate_example_value((2048, 32, 64), (4096, 128, 1), 'cuda:0', torch.float16, 0, (2048, 32, 64))
with torch.cuda._DeviceGuard(0):
    triton_poi_fused_5.run(buf13, buf15, arg31_1, buf16, buf17, buf14, arg30_1, buf18, buf19, buf21, buf22, 1048576, 4194304, stream=stream0)
del buf13, buf14, buf15, buf16, buf17, arg31_1, arg30_1, buf18, buf19, buf21, buf22

"""
# AOT ID: ['4_inference']
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


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\je\cjeptvlndpg77xcchd73ilr7rywzkyzgqnsuxyjbdb64zkyoye6m.py
# Topologically Sorted Source Nodes: [fused_add_rms_norm_maybe_inplace, marlin_gemm_1], Original ATen: [vllm_ir.fused_add_rms_norm, _C.marlin_gemm]
# Source node to ATen node mapping:
#   fused_add_rms_norm_maybe_inplace => add_tensor_4, add_tensor_5, convert_element_type_default_11, convert_element_type_default_8, convert_element_type_default_9, mean_dim_3, mul_tensor_6, mul_tensor_7, pow_tensor_scalar_3, rsqrt_default_3
#   marlin_gemm_1 => marlin_gemm_1
# Graph fragment:
#   %marlin_gemm : Tensor "f16[s18, 4096][4096, 1]cuda:0" = PlaceHolder[target=marlin_gemm]
#   %arg10_1 : Tensor "f16[s18, 4096][4096, 1]cuda:0" = PlaceHolder[target=arg10_1]
#   %buf2 : Tensor "f32[s18, 1][1, s18]cuda:0" = PlaceHolder[target=buf2]
#   %arg9_1 : Tensor "f16[4096][1]cuda:0" = PlaceHolder[target=arg9_1]
#   %convert_element_type_default_8 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%marlin_gemm, torch.float32), kwargs = {})
#   %convert_element_type_default_9 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%arg10_1, torch.float32), kwargs = {})
#   %add_tensor_4 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=3] = call_function[target=torch.ops.aten.add.Tensor](args = (%convert_element_type_default_8, %convert_element_type_default_9), kwargs = {})
#   %pow_tensor_scalar_3 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.pow.Tensor_Scalar](args = (%add_tensor_4, 2), kwargs = {})
#   %mean_dim_3 : Tensor "f32[s18, 1][1, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mean.dim](args = (%pow_tensor_scalar_3, [-1], True), kwargs = {})
#   %add_tensor_5 : Tensor "f32[s18, 1][1, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.add.Tensor](args = (%mean_dim_3, 1e-06), kwargs = {})
#   %rsqrt_default_3 : Tensor "f32[s18, 1][1, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.rsqrt.default](args = (%add_tensor_5,), kwargs = {})
#   %mul_tensor_6 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mul.Tensor](args = (%add_tensor_4, %rsqrt_default_3), kwargs = {})
#   %convert_element_type_default_11 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%mul_tensor_6, torch.float16), kwargs = {})
#   %mul_tensor_7 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mul.Tensor](args = (%convert_element_type_default_11, %arg9_1), kwargs = {})
#   %marlin_gemm_1 : Tensor "f16[s18, 24576][24576, 1]cuda:0"[num_users=2] = call_function[target=torch.ops._C.marlin_gemm.default](args = (%mul_tensor_7, None, %arg11_1, None, %arg12_1, None, None, %arg13_1, %arg14_1, %arg15_1, %arg16_1, 1125899907892224, %arg8_1, 24576, 4096, True, False, True, False), kwargs = {})
#   return %buf2,%buf3
triton_red_fused_fused_add_rms_norm_marlin_gemm_0 = async_compile.triton('triton_red_fused_fused_add_rms_norm_marlin_gemm_0', '''
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
    triton_meta={'signature': {'in_ptr0': '*fp16', 'in_ptr1': '*fp16', 'in_ptr2': '*fp16', 'out_ptr1': '*fp16', 'xnumel': 'i32', 'r0_numel': 'i32', 'XBLOCK': 'constexpr', 'R0_BLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'native_matmul': False, 'enable_fp_fusion': True, 'launch_pdl': False, 'disable_ftz': False, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (3,): [['tt.divisibility', 16]], (5,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'Grid1D', 'autotune_hints': set(), 'kernel_name': 'triton_red_fused_fused_add_rms_norm_marlin_gemm_0', 'mutated_arg_names': [], 'optimize_mem': True, 'no_x_dim': False, 'atomic_add_found': False, 'num_load': 5, 'num_store': 1, 'num_reduction': 1, 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False, 'add_persistent_rblock': True, 'tiling_scores': {'x': 0, 'r0_': 67117056}}
)
@triton.jit
def triton_red_fused_fused_add_rms_norm_marlin_gemm_0(in_ptr0, in_ptr1, in_ptr2, out_ptr1, xnumel, r0_numel, XBLOCK : tl.constexpr, R0_BLOCK : tl.constexpr):
    r0_numel = 4096
    rnumel = r0_numel
    RBLOCK: tl.constexpr = R0_BLOCK
    xoffset = tl.program_id(0) * XBLOCK
    xindex = xoffset + tl.arange(0, XBLOCK)[:, None]
    xmask = xindex < xnumel
    r0_base = tl.arange(0, R0_BLOCK)[None, :]
    rbase = r0_base
    x0 = xindex
    _tmp7 = tl.full([XBLOCK, R0_BLOCK], 0, tl.float32)
    for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
        r0_index = r0_offset + r0_base
        r0_mask = r0_index < r0_numel
        roffset = r0_offset
        rindex = r0_index
        r0_1 = r0_index
        tmp0 = tl.load(in_ptr0 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp2 = tl.load(in_ptr1 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp1 = tmp0.to(tl.float32)
        tmp3 = tmp2.to(tl.float32)
        tmp4 = tmp1 + tmp3
        tmp5 = tmp4 * tmp4
        tmp6 = tl.broadcast_to(tmp5, [XBLOCK, R0_BLOCK])
        tmp8 = _tmp7 + tmp6
        _tmp7 = tl.where(r0_mask & xmask, tmp8, _tmp7)
    tmp7 = tl.sum(_tmp7, 1)[:, None]
    for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
        r0_index = r0_offset + r0_base
        r0_mask = r0_index < r0_numel
        roffset = r0_offset
        rindex = r0_index
        r0_1 = r0_index
        tmp9 = tl.load(in_ptr0 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
        tmp11 = tl.load(in_ptr1 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
        tmp21 = tl.load(in_ptr2 + (r0_1), r0_mask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp10 = tmp9.to(tl.float32)
        tmp12 = tmp11.to(tl.float32)
        tmp13 = tmp10 + tmp12
        tmp14 = tl.full([1, 1], 4096.0, tl.float32)
        tmp15 = (tmp7 / tmp14)
        tmp16 = tl.full([1, 1], 1e-06, tl.float32)
        tmp17 = tmp15 + tmp16
        tmp18 = libdevice.rsqrt(tmp17)
        tmp19 = tmp13 * tmp18
        tmp20 = tmp19.to(tl.float32)
        tmp22 = tmp20 * tmp21
        tl.store(out_ptr1 + (r0_1 + 4096*x0), tmp22, r0_mask & xmask)
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\x5\cx5ds5nwuvqfkdewj4nbuiiqzhsrewc6zyowysopskecfzi2lemy.py
# Topologically Sorted Source Nodes: [getitem_2, silu, getitem_3, mul, marlin_gemm_2], Original ATen: [aten.slice, aten.silu, aten.mul, _C.marlin_gemm]
# Source node to ATen node mapping:
#   getitem_2 => slice_1
#   getitem_3 => slice_2
#   marlin_gemm_2 => marlin_gemm_2
#   mul => mul_36
#   silu => add_30, convert_element_type, convert_element_type_1, div, exp, neg
# Graph fragment:
#   %marlin_gemm_1 : Tensor "f16[s18, 24576][24576, 1]cuda:0" = PlaceHolder[target=marlin_gemm_1]
#   %slice_1 : Tensor "f16[s18, 12288][24576, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%marlin_gemm_1, 1, 0, 12288), kwargs = {})
#   %convert_element_type : Tensor "f32[s18, 12288][12288, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%slice_1, torch.float32), kwargs = {})
#   %neg : Tensor "f32[s18, 12288][12288, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.neg.default](args = (%convert_element_type,), kwargs = {})
#   %exp : Tensor "f32[s18, 12288][12288, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.exp.default](args = (%neg,), kwargs = {})
#   %add_30 : Tensor "f32[s18, 12288][12288, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.add.Tensor](args = (%exp, 1), kwargs = {})
#   %div : Tensor "f32[s18, 12288][12288, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.div.Tensor](args = (%convert_element_type, %add_30), kwargs = {})
#   %convert_element_type_1 : Tensor "f16[s18, 12288][12288, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%div, torch.float16), kwargs = {})
#   %slice_2 : Tensor "f16[s18, 12288][24576, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%marlin_gemm_1, 1, 12288, 9223372036854775807), kwargs = {})
#   %mul_36 : Tensor "f16[s18, 12288][12288, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mul.Tensor](args = (%convert_element_type_1, %slice_2), kwargs = {})
#   %marlin_gemm_2 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops._C.marlin_gemm.default](args = (%mul_36, None, %arg17_1, None, %arg18_1, None, None, %arg19_1, %arg20_1, %arg21_1, %arg22_1, 1125899907892224, %arg8_1, 4096, 12288, True, False, True, False), kwargs = {})
#   return %buf6
triton_poi_fused_marlin_gemm_mul_silu_slice_1 = async_compile.triton('triton_poi_fused_marlin_gemm_mul_silu_slice_1', '''
import triton
import triton.language as tl

from torch._inductor.runtime import triton_helpers, triton_heuristics
from torch._inductor.runtime.triton_helpers import libdevice, math as tl_math
from torch._inductor.runtime.hints import AutotuneHint, ReductionHint, TileHint, DeviceProperties
triton_helpers.set_driver_to_gpu()

@triton_heuristics.pointwise(
    size_hints={'x': 33554432}, 
    filename=__file__,
    triton_meta={'signature': {'in_ptr0': '*fp16', 'out_ptr0': '*fp16', 'xnumel': 'i32', 'XBLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'native_matmul': False, 'enable_fp_fusion': True, 'launch_pdl': False, 'disable_ftz': False, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'Grid1D', 'autotune_hints': set(), 'kernel_name': 'triton_poi_fused_marlin_gemm_mul_silu_slice_1', 'mutated_arg_names': [], 'optimize_mem': True, 'no_x_dim': False, 'atomic_add_found': False, 'num_load': 2, 'num_store': 1, 'num_reduction': 0, 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False, 'tiling_scores': {'x': 201326592}},
    min_elem_per_thread=0
)
@triton.jit
def triton_poi_fused_marlin_gemm_mul_silu_slice_1(in_ptr0, out_ptr0, xnumel, XBLOCK : tl.constexpr):
    xoffset = tl.program_id(0) * XBLOCK
    xindex = xoffset + tl.arange(0, XBLOCK)[:]
    xmask = tl.full([XBLOCK], True, tl.int1)[:]
    x0 = (xindex % 12288)
    x1 = xindex // 12288
    x2 = xindex
    tmp0 = tl.load(in_ptr0 + (x0 + 24576*x1), None).to(tl.float32)
    tmp8 = tl.load(in_ptr0 + (12288 + x0 + 24576*x1), None).to(tl.float32)
    tmp1 = tmp0.to(tl.float32)
    tmp2 = -tmp1
    tmp3 = libdevice.exp(tmp2)
    tmp4 = tl.full([1], 1.0, tl.float32)
    tmp5 = tmp3 + tmp4
    tmp6 = (tmp1 / tmp5)
    tmp7 = tmp6.to(tl.float32)
    tmp9 = tmp7 * tmp8
    tl.store(out_ptr0 + (x2), tmp9, None)
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\bc\cbcyf27fwc6mpc4nf6lmeda4s7bpxkpr2klllgrcd5zgv57evu45.py
# Topologically Sorted Source Nodes: [fused_add_rms_norm_maybe_inplace, fused_add_rms_norm_maybe_inplace_1, marlin_gemm_3], Original ATen: [vllm_ir.fused_add_rms_norm, _C.marlin_gemm]
# Source node to ATen node mapping:
#   fused_add_rms_norm_maybe_inplace => add_tensor_4, convert_element_type_default_10, convert_element_type_default_8, convert_element_type_default_9
#   fused_add_rms_norm_maybe_inplace_1 => add_tensor_2, add_tensor_3, convert_element_type_default_4, convert_element_type_default_5, convert_element_type_default_6, convert_element_type_default_7, mean_dim_2, mul_tensor_4, mul_tensor_5, pow_tensor_scalar_2, rsqrt_default_2
#   marlin_gemm_3 => marlin_gemm_3
# Graph fragment:
#   %marlin_gemm_2 : Tensor "f16[s18, 4096][4096, 1]cuda:0" = PlaceHolder[target=marlin_gemm_2]
#   %marlin_gemm : Tensor "f16[s18, 4096][4096, 1]cuda:0" = PlaceHolder[target=marlin_gemm]
#   %arg10_1 : Tensor "f16[s18, 4096][4096, 1]cuda:0" = PlaceHolder[target=arg10_1]
#   %buf10 : Tensor "f32[s18, 1][1, s18]cuda:0" = PlaceHolder[target=buf10]
#   %arg23_1 : Tensor "f16[4096][1]cuda:0" = PlaceHolder[target=arg23_1]
#   %convert_element_type_default_8 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%marlin_gemm, torch.float32), kwargs = {})
#   %convert_element_type_default_9 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%arg10_1, torch.float32), kwargs = {})
#   %add_tensor_4 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=3] = call_function[target=torch.ops.aten.add.Tensor](args = (%convert_element_type_default_8, %convert_element_type_default_9), kwargs = {})
#   %convert_element_type_default_10 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%add_tensor_4, torch.float16), kwargs = {})
#   %convert_element_type_default_4 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%marlin_gemm_2, torch.float32), kwargs = {})
#   %convert_element_type_default_5 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%convert_element_type_default_10, torch.float32), kwargs = {})
#   %add_tensor_2 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=3] = call_function[target=torch.ops.aten.add.Tensor](args = (%convert_element_type_default_4, %convert_element_type_default_5), kwargs = {})
#   %convert_element_type_default_6 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%add_tensor_2, torch.float16), kwargs = {})
#   %pow_tensor_scalar_2 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.pow.Tensor_Scalar](args = (%add_tensor_2, 2), kwargs = {})
#   %mean_dim_2 : Tensor "f32[s18, 1][1, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mean.dim](args = (%pow_tensor_scalar_2, [-1], True), kwargs = {})
#   %add_tensor_3 : Tensor "f32[s18, 1][1, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.add.Tensor](args = (%mean_dim_2, 1e-06), kwargs = {})
#   %rsqrt_default_2 : Tensor "f32[s18, 1][1, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.rsqrt.default](args = (%add_tensor_3,), kwargs = {})
#   %mul_tensor_4 : Tensor "f32[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mul.Tensor](args = (%add_tensor_2, %rsqrt_default_2), kwargs = {})
#   %convert_element_type_default_7 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.prims.convert_element_type.default](args = (%mul_tensor_4, torch.float16), kwargs = {})
#   %mul_tensor_5 : Tensor "f16[s18, 4096][4096, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.mul.Tensor](args = (%convert_element_type_default_7, %arg23_1), kwargs = {})
#   %marlin_gemm_3 : Tensor "f16[s18, 6144][6144, 1]cuda:0"[num_users=1] = call_function[target=torch.ops._C.marlin_gemm.default](args = (%mul_tensor_5, None, %arg24_1, None, %arg25_1, None, None, %arg26_1, %arg27_1, %arg28_1, %arg29_1, 1125899907892224, %arg8_1, 6144, 4096, True, False, True, False), kwargs = {})
#   return %buf10,%convert_element_type_default_6,%buf11
triton_red_fused_fused_add_rms_norm_marlin_gemm_2 = async_compile.triton('triton_red_fused_fused_add_rms_norm_marlin_gemm_2', '''
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
    triton_meta={'signature': {'in_ptr0': '*fp16', 'in_ptr1': '*fp16', 'in_ptr2': '*fp16', 'in_ptr3': '*fp16', 'out_ptr1': '*fp16', 'out_ptr2': '*fp16', 'xnumel': 'i32', 'r0_numel': 'i32', 'XBLOCK': 'constexpr', 'R0_BLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'native_matmul': False, 'enable_fp_fusion': True, 'launch_pdl': False, 'disable_ftz': False, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (3,): [['tt.divisibility', 16]], (4,): [['tt.divisibility', 16]], (5,): [['tt.divisibility', 16]], (7,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'Grid1D', 'autotune_hints': set(), 'kernel_name': 'triton_red_fused_fused_add_rms_norm_marlin_gemm_2', 'mutated_arg_names': [], 'optimize_mem': True, 'no_x_dim': False, 'atomic_add_found': False, 'num_load': 7, 'num_store': 2, 'num_reduction': 1, 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False, 'add_persistent_rblock': True, 'tiling_scores': {'x': 0, 'r0_': 117448704}}
)
@triton.jit
def triton_red_fused_fused_add_rms_norm_marlin_gemm_2(in_ptr0, in_ptr1, in_ptr2, in_ptr3, out_ptr1, out_ptr2, xnumel, r0_numel, XBLOCK : tl.constexpr, R0_BLOCK : tl.constexpr):
    r0_numel = 4096
    rnumel = r0_numel
    RBLOCK: tl.constexpr = R0_BLOCK
    xoffset = tl.program_id(0) * XBLOCK
    xindex = xoffset + tl.arange(0, XBLOCK)[:, None]
    xmask = xindex < xnumel
    r0_base = tl.arange(0, R0_BLOCK)[None, :]
    rbase = r0_base
    x0 = xindex
    _tmp12 = tl.full([XBLOCK, R0_BLOCK], 0, tl.float32)
    for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
        r0_index = r0_offset + r0_base
        r0_mask = r0_index < r0_numel
        roffset = r0_offset
        rindex = r0_index
        r0_1 = r0_index
        tmp0 = tl.load(in_ptr0 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp2 = tl.load(in_ptr1 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp4 = tl.load(in_ptr2 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp1 = tmp0.to(tl.float32)
        tmp3 = tmp2.to(tl.float32)
        tmp5 = tmp4.to(tl.float32)
        tmp6 = tmp3 + tmp5
        tmp7 = tmp6.to(tl.float32)
        tmp8 = tmp7.to(tl.float32)
        tmp9 = tmp1 + tmp8
        tmp10 = tmp9 * tmp9
        tmp11 = tl.broadcast_to(tmp10, [XBLOCK, R0_BLOCK])
        tmp13 = _tmp12 + tmp11
        _tmp12 = tl.where(r0_mask & xmask, tmp13, _tmp12)
        tmp14 = tmp9.to(tl.float32)
        tl.store(out_ptr1 + (r0_1 + 4096*x0), tmp14, r0_mask & xmask)
    tmp12 = tl.sum(_tmp12, 1)[:, None]
    for r0_offset in tl.range(0, r0_numel, R0_BLOCK):
        r0_index = r0_offset + r0_base
        r0_mask = r0_index < r0_numel
        roffset = r0_offset
        rindex = r0_index
        r0_1 = r0_index
        tmp15 = tl.load(in_ptr0 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
        tmp17 = tl.load(in_ptr1 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
        tmp19 = tl.load(in_ptr2 + (r0_1 + 4096*x0), r0_mask & xmask, eviction_policy='evict_first', other=0.0).to(tl.float32)
        tmp32 = tl.load(in_ptr3 + (r0_1), r0_mask, eviction_policy='evict_last', other=0.0).to(tl.float32)
        tmp16 = tmp15.to(tl.float32)
        tmp18 = tmp17.to(tl.float32)
        tmp20 = tmp19.to(tl.float32)
        tmp21 = tmp18 + tmp20
        tmp22 = tmp21.to(tl.float32)
        tmp23 = tmp22.to(tl.float32)
        tmp24 = tmp16 + tmp23
        tmp25 = tl.full([1, 1], 4096.0, tl.float32)
        tmp26 = (tmp12 / tmp25)
        tmp27 = tl.full([1, 1], 1e-06, tl.float32)
        tmp28 = tmp26 + tmp27
        tmp29 = libdevice.rsqrt(tmp28)
        tmp30 = tmp24 * tmp29
        tmp31 = tmp30.to(tl.float32)
        tmp33 = tmp31 * tmp32
        tl.store(out_ptr2 + (r0_1 + 4096*x0), tmp33, r0_mask & xmask)
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\dr\cdrtmtpsspz767ej2ffk6cbhihtkazuftq4qtyvi5almlfxdkxqo.py
# Unsorted Source Nodes: [], Original ATen: []
# Source node to ATen node mapping:
triton_red_fused_3 = async_compile.triton('triton_red_fused_3', '''
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
    inductor_meta={'grid_type': 'SequentialComboKernelGrid', 'combo_grid_meta': {'num_kernels': 2, 'min_blocks': None, 'default_config': None, 'no_x_dim_0': False, 'xnumel_0': None, 'no_x_dim_1': False, 'xnumel_1': None}, 'kernel_name': 'triton_red_fused_3', 'mutated_arg_names': [], 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False}
)
@triton.jit
def triton_red_fused_3(in_ptr0, out_ptr0, out_ptr1, xnumel_0, xnumel_1, XBLOCK : tl.constexpr, R0_BLOCK : tl.constexpr):
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
        triton_red_fused_3.run(*args, stream=stream0)


def benchmark_all_configs(args):
    with torch.cuda._DeviceGuard(0):
        torch.cuda.set_device(0)
        return triton_red_fused_3.benchmark_all_configs(*args)


if __name__ == '__main__':
    from torch._inductor.runtime.benchmarking import benchmarker

    args = get_args()
    ms = benchmarker.benchmark(call, fn_args=(args,), device=cuda,rep=40)
    num_gb = 0
    gb_per_s = num_gb / (ms / 1e3)
    print(f"{ms:.3f}ms    {num_gb:.3f}GB    {gb_per_s:.2f}GB/s")
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\47\c47x3zz7ulolv6xmljvo72tsp4hqzux5chxcjtuhrzov3peubpje.py
# Topologically Sorted Source Nodes: [getitem_9, chunk, getitem_12, clone, setitem, getitem_13, setitem_1, getitem_14, getitem_15, clone_1, setitem_2, getitem_16, setitem_3, getitem_17], Original ATen: [aten.index, aten.split, aten.select, aten.clone, aten.slice, aten.copy]
# Source node to ATen node mapping:
#   chunk => split
#   clone => clone
#   clone_1 => clone_1
#   getitem_12 => select
#   getitem_13 => select_1, slice_3
#   getitem_14 => select_2, slice_6
#   getitem_15 => select_3
#   getitem_16 => select_4, slice_10
#   getitem_17 => select_5, slice_13
#   getitem_9 => index
#   setitem => copy, slice_4
#   setitem_1 => copy_1, slice_8
#   setitem_2 => copy_2, slice_11
#   setitem_3 => copy_3, slice_15
# Graph fragment:
#   %arg33_1 : Tensor "i64[3, s18][s7, 1]cuda:0" = PlaceHolder[target=arg33_1]
#   %arg32_1 : Tensor "f16[1048576, 128][128, 1]cuda:0" = PlaceHolder[target=arg32_1]
#   %index : Tensor "f16[3, s18, 128][128*s18, 128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.index.Tensor](args = (%arg32_1, [%arg33_1]), kwargs = {})
#   %split : [num_users=2] = call_function[target=torch.ops.aten.split.Tensor](args = (%index, 64, -1), kwargs = {})
#   %select : Tensor "f16[s18, 64][128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.select.int](args = (%getitem_7, 0, 0), kwargs = {})
#   %clone : Tensor "f16[s18, 64][64, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.aten.clone.default](args = (%select,), kwargs = {})
#   %slice_4 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%clone, 1, 1, 60, 3), kwargs = {})
#   %select_1 : Tensor "f16[s18, 64][128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.select.int](args = (%getitem_7, 0, 1), kwargs = {})
#   %slice_3 : Tensor "f16[s18, 20][128, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%select_1, 1, 1, 60, 3), kwargs = {})
#   %copy : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.copy.default](args = (%slice_4, %slice_3), kwargs = {})
#   %slice_scatter_default : Tensor "f16[s18, 64][64, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.aten.slice_scatter.default](args = (%clone, %copy, 1, 1, 60, 3), kwargs = {})
#   %slice_8 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%slice_scatter_default, 1, 2, 60, 3), kwargs = {})
#   %select_2 : Tensor "f16[s18, 64][128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.select.int](args = (%getitem_7, 0, 2), kwargs = {})
#   %slice_6 : Tensor "f16[s18, 20][128, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%select_2, 1, 2, 60, 3), kwargs = {})
#   %copy_1 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.copy.default](args = (%slice_8, %slice_6), kwargs = {})
#   %slice_scatter_default_1 : Tensor "f16[s18, 64][64, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.aten.slice_scatter.default](args = (%slice_scatter_default, %copy_1, 1, 2, 60, 3), kwargs = {})
#   %select_3 : Tensor "f16[s18, 64][128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.select.int](args = (%getitem_8, 0, 0), kwargs = {})
#   %clone_1 : Tensor "f16[s18, 64][64, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.aten.clone.default](args = (%select_3,), kwargs = {})
#   %slice_11 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%clone_1, 1, 1, 60, 3), kwargs = {})
#   %select_4 : Tensor "f16[s18, 64][128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.select.int](args = (%getitem_8, 0, 1), kwargs = {})
#   %slice_10 : Tensor "f16[s18, 20][128, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%select_4, 1, 1, 60, 3), kwargs = {})
#   %copy_2 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.copy.default](args = (%slice_11, %slice_10), kwargs = {})
#   %slice_scatter_default_2 : Tensor "f16[s18, 64][64, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.aten.slice_scatter.default](args = (%clone_1, %copy_2, 1, 1, 60, 3), kwargs = {})
#   %slice_15 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%slice_scatter_default_2, 1, 2, 60, 3), kwargs = {})
#   %select_5 : Tensor "f16[s18, 64][128, 1]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.select.int](args = (%getitem_8, 0, 2), kwargs = {})
#   %slice_13 : Tensor "f16[s18, 20][128, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.slice.Tensor](args = (%select_5, 1, 2, 60, 3), kwargs = {})
#   %copy_3 : Tensor "f16[s18, 20][64, 3]cuda:0"[num_users=1] = call_function[target=torch.ops.aten.copy.default](args = (%slice_15, %slice_13), kwargs = {})
#   %slice_scatter_default_3 : Tensor "f16[s18, 64][64, 1]cuda:0"[num_users=2] = call_function[target=torch.ops.aten.slice_scatter.default](args = (%slice_scatter_default_2, %copy_3, 1, 2, 60, 3), kwargs = {})
#   return %slice_scatter_default_1,%slice_scatter_default_3
triton_poi_fused_clone_copy_index_select_slice_split_4 = async_compile.triton('triton_poi_fused_clone_copy_index_select_slice_split_4', '''
import triton
import triton.language as tl

from torch._inductor.runtime import triton_helpers, triton_heuristics
from torch._inductor.runtime.triton_helpers import libdevice, math as tl_math
from torch._inductor.runtime.hints import AutotuneHint, ReductionHint, TileHint, DeviceProperties
triton_helpers.set_driver_to_gpu()

@triton_heuristics.pointwise(
    size_hints={'y': 2048, 'x': 64}, tile_hint=TileHint.DEFAULT,
    filename=__file__,
    triton_meta={'signature': {'in_ptr0': '*i64', 'in_ptr1': '*fp16', 'out_ptr0': '*fp16', 'out_ptr1': '*fp16', 'ks0': 'i64', 'ynumel': 'i32', 'xnumel': 'i32', 'YBLOCK': 'constexpr', 'XBLOCK': 'constexpr'}, 'device': DeviceProperties(type='cuda', index=0, multi_processor_count=70, cc=120, major=12, regs_per_multiprocessor=65536, max_threads_per_multi_processor=1536, max_threads_per_block=1024, warp_size=32), 'constants': {}, 'native_matmul': False, 'enable_fp_fusion': True, 'launch_pdl': False, 'disable_ftz': False, 'configs': [{(0,): [['tt.divisibility', 16]], (1,): [['tt.divisibility', 16]], (2,): [['tt.divisibility', 16]], (3,): [['tt.divisibility', 16]], (6,): [['tt.divisibility', 16]]}]},
    inductor_meta={'grid_type': 'Grid2DWithYZOverflow', 'autotune_hints': set(), 'kernel_name': 'triton_poi_fused_clone_copy_index_select_slice_split_4', 'mutated_arg_names': [], 'optimize_mem': True, 'no_x_dim': False, 'atomic_add_found': False, 'num_load': 3, 'num_store': 2, 'num_reduction': 0, 'backend_hash': '0FAAA06D6C84A67843966AD4F2C5298301B3561B4A3D42D12213CFCD68515431', 'assert_indirect_indexing': True, 'autotune_local_cache': True, 'autotune_pointwise': True, 'autotune_remote_cache': None, 'force_disable_caches': False, 'dynamic_scale_rblock': True, 'max_autotune': False, 'max_autotune_pointwise': False, 'min_split_scan_rblock': 256, 'spill_threshold': 16, 'store_cubin': False, 'deterministic': False, 'force_filter_reduction_configs': False, 'mix_order_reduction_allow_multi_stages': False, 'are_deterministic_algorithms_enabled': False, 'tiling_scores': {'y': 49152, 'x': 1048576}},
    min_elem_per_thread=0
)
@triton.jit
def triton_poi_fused_clone_copy_index_select_slice_split_4(in_ptr0, in_ptr1, out_ptr0, out_ptr1, ks0, ynumel, xnumel, YBLOCK : tl.constexpr, XBLOCK : tl.constexpr):
    xnumel = 64
    yoffset = (tl.program_id(1) + tl.program_id(2) * tl.num_programs(1)) * YBLOCK
    yindex = yoffset + tl.arange(0, YBLOCK)[:, None]
    ymask = yindex < ynumel
    xoffset = tl.program_id(0) * XBLOCK
    xindex = xoffset + tl.arange(0, XBLOCK)[None, :]
    xmask = xindex < xnumel
    x1 = xindex
    y0 = yindex
    tmp30 = tl.load(in_ptr0 + (y0), ymask, eviction_policy='evict_last')
    tmp0 = x1
    tmp1 = tl.full([1, 1], 2, tl.int64)
    tmp2 = tmp0 >= tmp1
    tmp3 = tl.full([1, 1], 60, tl.int64)
    tmp4 = tmp0 < tmp3
    tmp5 = (((-2) + x1) % 3)
    tmp6 = tl.full([1, 1], 0, tl.int64)
    tmp7 = tmp5 == tmp6
    tmp8 = tmp2 & tmp4
    tmp9 = tmp8 & tmp7
    tmp10 = tl.load(in_ptr0 + (tl.broadcast_to(y0 + 2*ks0, [YBLOCK, XBLOCK])), tmp9 & xmask & ymask, eviction_policy='evict_last', other=0.0)
    tmp11 = tl.full([1, 1], 1048576, tl.int32)
    tmp12 = tmp10 + tmp11
    tmp13 = tmp10 < 0
    tmp14 = tl.where(tmp13, tmp12, tmp10)
    tl.device_assert(((0 <= tl.broadcast_to(tmp14, [YBLOCK, XBLOCK])) & (tl.broadcast_to(tmp14, [YBLOCK, XBLOCK]) < 1048576)) | ~(tmp9 & xmask & ymask), "index out of bounds: 0 <= tl.broadcast_to(tmp14, [YBLOCK, XBLOCK]) < 1048576")
    tmp16 = tl.load(in_ptr1 + (tl.broadcast_to(2 + 3*(triton_helpers.div_floor_integer((-2) + x1,  3)) + 128*tmp14, [YBLOCK, XBLOCK])), tmp9 & xmask & ymask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp17 = tl.full([1, 1], 1, tl.int64)
    tmp18 = tmp0 >= tmp17
    tmp19 = (((-1) + x1) % 3)
    tmp20 = tmp19 == tmp6
    tmp21 = tmp18 & tmp4
    tmp22 = tmp21 & tmp20
    tmp23 = tl.load(in_ptr0 + (tl.broadcast_to(ks0 + y0, [YBLOCK, XBLOCK])), tmp22 & xmask & ymask, eviction_policy='evict_last', other=0.0)
    tmp24 = tl.full([1, 1], 1048576, tl.int32)
    tmp25 = tmp23 + tmp24
    tmp26 = tmp23 < 0
    tmp27 = tl.where(tmp26, tmp25, tmp23)
    tl.device_assert(((0 <= tl.broadcast_to(tmp27, [YBLOCK, XBLOCK])) & (tl.broadcast_to(tmp27, [YBLOCK, XBLOCK]) < 1048576)) | ~(tmp22 & xmask & ymask), "index out of bounds: 0 <= tl.broadcast_to(tmp27, [YBLOCK, XBLOCK]) < 1048576")
    tmp29 = tl.load(in_ptr1 + (tl.broadcast_to(1 + 3*(triton_helpers.div_floor_integer((-1) + x1,  3)) + 128*tmp27, [YBLOCK, XBLOCK])), tmp22 & xmask & ymask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp31 = tl.full([1, 1], 1048576, tl.int32)
    tmp32 = tmp30 + tmp31
    tmp33 = tmp30 < 0
    tmp34 = tl.where(tmp33, tmp32, tmp30)
    tl.device_assert(((0 <= tmp34) & (tmp34 < 1048576)) | ~(ymask), "index out of bounds: 0 <= tmp34 < 1048576")
    tmp36 = tl.load(in_ptr1 + (x1 + 128*tmp34), xmask & ymask).to(tl.float32)
    tmp37 = tl.where(tmp22, tmp29, tmp36)
    tmp38 = tl.where(tmp9, tmp16, tmp37)
    tmp39 = tl.load(in_ptr1 + (tl.broadcast_to(66 + 3*(triton_helpers.div_floor_integer((-2) + x1,  3)) + 128*tmp14, [YBLOCK, XBLOCK])), tmp9 & xmask & ymask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp40 = tl.load(in_ptr1 + (tl.broadcast_to(65 + 3*(triton_helpers.div_floor_integer((-1) + x1,  3)) + 128*tmp27, [YBLOCK, XBLOCK])), tmp22 & xmask & ymask, eviction_policy='evict_last', other=0.0).to(tl.float32)
    tmp41 = tl.load(in_ptr1 + (64 + x1 + 128*tmp34), xmask & ymask).to(tl.float32)
    tmp42 = tl.where(tmp22, tmp40, tmp41)
    tmp43 = tl.where(tmp9, tmp39, tmp42)
    tl.store(out_ptr0 + (x1 + 64*y0), tmp38, xmask & ymask)
    tl.store(out_ptr1 + (x1 + 64*y0), tmp43, xmask & ymask)
''', device_str='cuda')


# kernel path: E:\OmniSpace\.cache\vllm\torch_compile_cache\torch_aot_compile\3a5e8194ce08dc51757264ef4cf333753a7197d9fb56126c4d9ccc94066a3037\inductor_cache\el\celjvhvi6dygyyytsps34bnjmcycnzgmev2uevuuln53mnwc2tjq.py
# Unsorted Source Nodes: [], Original ATen: []
# Source node to ATen node mapping:
triton_poi_fused_5 = async_compile.triton('triton_poi_fused_5', '''
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
        arg0_1, arg1_1, arg2_1, arg3_1, arg4_1, arg5_1, arg6_1, arg7_1, arg8_1, arg9_1, arg10_1, arg11_1, arg12_1, arg13_1, arg14_1, arg15_1, arg16_1, arg17_1, arg18_1, arg19_1, arg20_1, arg21_1, arg22_1, arg23_1, arg24_1, arg25_1, arg26_1, arg27_1, arg28_1, arg29_1, arg30_1, arg31_1, arg32_1, arg33_1, arg34_1 = args
        args.clear()
        s59 = arg1_1
        s18 = arg8_1
        s7 = arg34_1
        with torch.cuda._DeviceGuard(0):
            torch.cuda.set_device(0)
            # Topologically Sorted Source Nodes: [view, marlin_gemm], Original ATen: [aten.view, _C.marlin_gemm]
            buf0 = torch.ops._C.marlin_gemm.default(reinterpret_tensor(arg0_1, (s18, 4096), (4096, 1), 0), None, arg2_1, None, arg3_1, None, None, arg4_1, arg5_1, arg6_1, arg7_1, 1125899907892224, s18, 4096, 4096, True, False, True, False)
            del arg0_1
            del arg2_1
            del arg3_1
            del arg4_1
            del arg5_1
            del arg6_1
            del arg7_1
            buf1 = buf0
            del buf0
            buf3 = empty_strided_cuda((s18, 4096), (4096, 1), torch.float16)
            # Topologically Sorted Source Nodes: [fused_add_rms_norm_maybe_inplace, marlin_gemm_1], Original ATen: [vllm_ir.fused_add_rms_norm, _C.marlin_gemm]
            stream0 = get_raw_stream(0)
            triton_red_fused_fused_add_rms_norm_marlin_gemm_0.run(buf1, arg10_1, arg9_1, buf3, s18, 4096, stream=stream0)
            del arg9_1
            # Topologically Sorted Source Nodes: [fused_add_rms_norm_maybe_inplace, marlin_gemm_1], Original ATen: [vllm_ir.fused_add_rms_norm, _C.marlin_gemm]
            buf4 = torch.ops._C.marlin_gemm.default(buf3, None, arg11_1, None, arg12_1, None, None, arg13_1, arg14_1, arg15_1, arg16_1, 1125899907892224, s18, 24576, 4096, True, False, True, False)
            del arg11_1
            del arg12_1
            del arg13_1
            del arg14_1
            del arg15_1
            del arg16_1
            del buf3
            buf5 = buf4
            del buf4
            buf6 = empty_strided_cuda((s18, 12288), (12288, 1), torch.float16)
            # Topologically Sorted Source Nodes: [getitem_2, silu, getitem_3, mul, marlin_gemm_2], Original ATen: [aten.slice, aten.silu, aten.mul, _C.marlin_gemm]
            triton_poi_fused_marlin_gemm_mul_silu_slice_1_xnumel = 12288*s18
            stream0 = get_raw_stream(0)
            triton_poi_fused_marlin_gemm_mul_silu_slice_1.run(buf5, buf6, triton_poi_fused_marlin_gemm_mul_silu_slice_1_xnumel, stream=stream0)
            del buf5
            # Topologically Sorted Source Nodes: [getitem_2, silu, getitem_3, mul, marlin_gemm_2], Original ATen: [aten.slice, aten.silu, aten.mul, _C.marlin_gemm]
            buf7 = torch.ops._C.marlin_gemm.default(buf6, None, arg17_1, None, arg18_1, None, None, arg19_1, arg20_1, arg21_1, arg22_1, 1125899907892224, s18, 4096, 12288, True, False, True, False)
            del arg17_1
            del arg18_1
            del arg19_1
            del arg20_1
            del arg21_1
            del arg22_1
            del buf6
            buf8 = buf7
            del buf7
            buf9 = empty_strided_cuda((s18, 4096), (4096, 1), torch.float16)
            buf11 = empty_strided_cuda((s18, 4096), (4096, 1), torch.float16)
            # Topologically Sorted Source Nodes: [fused_add_rms_norm_maybe_inplace, fused_add_rms_norm_maybe_inplace_1, marlin_gemm_3], Original ATen: [vllm_ir.fused_add_rms_norm, _C.marlin_gemm]
            stream0 = get_raw_stream(0)
            triton_red_fused_fused_add_rms_norm_marlin_gemm_2.run(buf8, buf1, arg10_1, arg23_1, buf9, buf11, s18, 4096, stream=stream0)
            del arg10_1
            del arg23_1
            del buf1
            # Topologically Sorted Source Nodes: [fused_add_rms_norm_maybe_inplace, fused_add_rms_norm_maybe_inplace_1, marlin_gemm_3], Original ATen: [vllm_ir.fused_add_rms_norm, _C.marlin_gemm]
            buf12 = torch.ops._C.marlin_gemm.default(buf11, None, arg24_1, None, arg25_1, None, None, arg26_1, arg27_1, arg28_1, arg29_1, 1125899907892224, s18, 6144, 4096, True, False, True, False)
            del arg24_1
            del arg25_1
            del arg26_1
            del arg27_1
            del arg28_1
            del arg29_1
            buf13 = buf12
            del buf12
            buf14 = empty_strided_cuda((s18, 32, 1), (32, 1, 32*s18), torch.float32)
            buf15 = empty_strided_cuda((s18, 8, 1), (8, 1, 8*s18), torch.float32)
            # Topologically Sorted Source Nodes: [split, view_1, rms_norm_default, view_3, rms_norm_default_1], Original ATen: [aten.split_with_sizes, aten.view, vllm_ir.rms_norm]
            triton_red_fused_3_xnumel_0 = 32*s18
            triton_red_fused_3_xnumel_1 = 8*s18
            stream0 = get_raw_stream(0)
            triton_red_fused_3.run(buf13, buf14, buf15, triton_red_fused_3_xnumel_0, triton_red_fused_3_xnumel_1, stream=stream0)
            buf16 = empty_strided_cuda((s18, 64), (64, 1), torch.float16)
            buf17 = empty_strided_cuda((s18, 64), (64, 1), torch.float16)
            # Topologically Sorted Source Nodes: [getitem_9, chunk, getitem_12, clone, setitem, getitem_13, setitem_1, getitem_14, getitem_15, clone_1, setitem_2, getitem_16, setitem_3, getitem_17], Original ATen: [aten.index, aten.split, aten.select, aten.clone, aten.slice, aten.copy]
            stream0 = get_raw_stream(0)
            triton_poi_fused_clone_copy_index_select_slice_split_4.run(arg33_1, arg32_1, buf16, buf17, s7, s18, 64, stream=stream0)
            del arg32_1
            del arg33_1
            buf20 = empty_strided_cuda((s18, 8, 128), (1024, 128, 1), torch.float16)
            buf18 = reinterpret_tensor(buf20, (s18, 8, 64), (1024, 128, 1), 0)  # alias
            buf19 = reinterpret_tensor(buf20, (s18, 8, 64), (1024, 128, 1), 64)  # alias
            buf23 = reinterpret_tensor(buf11, (s18, 32, 128), (4096, 128, 1), 0); del buf11  # reuse
            buf21 = reinterpret_tensor(buf23, (s18, 32, 64), (4096, 128, 1), 0)  # alias
            buf22 = reinterpret_tensor(buf23, (s18, 32, 64), (4096, 128, 1), 64)  # alias
            # Unsorted Source Nodes: [], Original ATen: []
            triton_poi_fused_5_xnumel_0 = 512*s18
            triton_poi_fused_5_xnumel_1 = 2048*s18
            stream0 = get_raw_stream(0)
            triton_poi_fused_5.run(buf13, buf15, arg31_1, buf16, buf17, buf14, arg30_1, buf18, buf19, buf21, buf22, triton_poi_fused_5_xnumel_0, triton_poi_fused_5_xnumel_1, stream=stream0)
            del arg30_1
            del arg31_1
            del buf14
            del buf15
            del buf16
            del buf17
            buf24 = buf8; del buf8  # reuse
        return (buf20, reinterpret_tensor(buf13, (s18, 8, 128), (6144, 128, 1), 5120), buf23, reinterpret_tensor(buf24, (s18, 32, 128), (4096, 128, 1), 0), buf9, )

runner = Runner(partitions=[])
call = runner.call
recursively_apply_fns = runner.recursively_apply_fns


def get_args():
    from torch._dynamo.testing import rand_strided
    arg0_1 = rand_strided((2048, 32, 128), (4096, 128, 1), device='cuda:0', dtype=torch.float16)
    arg1_1 = 2048
    arg2_1 = rand_strided((256, 8192), (8192, 1), device='cuda:0', dtype=torch.int32)
    arg3_1 = rand_strided((128, 4096), (4096, 1), device='cuda:0', dtype=torch.float16)
    arg4_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg5_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg6_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg7_1 = rand_strided((70, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg8_1 = 2048
    arg9_1 = rand_strided((4096, ), (1, ), device='cuda:0', dtype=torch.float16)
    arg10_1 = rand_strided((2048, 4096), (4096, 1), device='cuda:0', dtype=torch.float16)
    arg11_1 = rand_strided((256, 49152), (49152, 1), device='cuda:0', dtype=torch.int32)
    arg12_1 = rand_strided((128, 24576), (24576, 1), device='cuda:0', dtype=torch.float16)
    arg13_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg14_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg15_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg16_1 = rand_strided((70, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg17_1 = rand_strided((768, 8192), (8192, 1), device='cuda:0', dtype=torch.int32)
    arg18_1 = rand_strided((384, 4096), (4096, 1), device='cuda:0', dtype=torch.float16)
    arg19_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg20_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg21_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg22_1 = rand_strided((70, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg23_1 = rand_strided((4096, ), (1, ), device='cuda:0', dtype=torch.float16)
    arg24_1 = rand_strided((256, 12288), (12288, 1), device='cuda:0', dtype=torch.int32)
    arg25_1 = rand_strided((128, 6144), (6144, 1), device='cuda:0', dtype=torch.float16)
    arg26_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg27_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg28_1 = rand_strided((0, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg29_1 = rand_strided((70, ), (1, ), device='cuda:0', dtype=torch.int32)
    arg30_1 = rand_strided((128, ), (1, ), device='cuda:0', dtype=torch.float16)
    arg31_1 = rand_strided((128, ), (1, ), device='cuda:0', dtype=torch.float16)
    arg32_1 = rand_strided((1048576, 128), (128, 1), device='cuda:0', dtype=torch.float16)
    arg33_1 = rand_strided((3, 2048), (2049, 1), device='cuda:0', dtype=torch.int64)
    arg34_1 = 2049
    return [arg0_1, arg1_1, arg2_1, arg3_1, arg4_1, arg5_1, arg6_1, arg7_1, arg8_1, arg9_1, arg10_1, arg11_1, arg12_1, arg13_1, arg14_1, arg15_1, arg16_1, arg17_1, arg18_1, arg19_1, arg20_1, arg21_1, arg22_1, arg23_1, arg24_1, arg25_1, arg26_1, arg27_1, arg28_1, arg29_1, arg30_1, arg31_1, arg32_1, arg33_1, arg34_1]


def benchmark_compiled_module(args, times=10, repeat=10):
    from torch._inductor.utils import print_performance
    fn = lambda: call(list(args))
    return print_performance(fn, times=times, repeat=repeat)


if __name__ == "__main__":
    from torch._inductor.wrapper_benchmark import compiled_module_main
    args = get_args()
    compiled_module_main('None', lambda times, repeat: benchmark_compiled_module(args, times=times, repeat=repeat))
