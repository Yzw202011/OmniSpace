
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
