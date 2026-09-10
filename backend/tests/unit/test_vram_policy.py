"""V9 尾款① 阈值单源对拍单测（2026-09-09）。

锁定四个历史实测标定值的字面量——首轮搬家不改值，任何人动值必先
过本测试（想清楚是不是真要改实测标定）。同时锁消费方符号别名仍指
向单源（防有人回写魔法数绕过单源）。
"""

from __future__ import annotations


def test_policy_values_locked_to_history() -> None:
    """四阈值=历史字面量（22.0/15.5/3.0/4.0——各值依据见 vram_policy 注释）。"""
    from backend.services import vram_policy as vp

    assert vp.PAINT_QWEN_RAM_FLOOR_GB == 22.0
    assert vp.QWEN_GGUF_RAM_FLOOR_GB == 15.5
    assert vp.VLLM_RAM_HEADROOM_GB == 3.0
    assert vp.PAINT_LOW_VRAM_FALLBACK_GB == 4.0


def test_gpu_policy_values_locked_to_history() -> None:
    """GPU 侧九常量=历史字面量（显存调度机制批1 搬家，依据见各常量注）。"""
    from backend.services import vram_policy as vp

    assert vp.VLLM_UTIL_DEFAULT == 0.85
    assert vp.VLLM_UTIL_LARGE_WEIGHTS == 0.87
    assert vp.VLLM_UTIL_MTP == 0.92
    assert vp.VLLM_FLOOR_OVERHEAD_GB == 4.2
    assert vp.VLLM_MTP_EXTRA_GB == 1.3
    assert vp.VLLM_ADMISSION_FACTOR == 0.98
    assert vp.TRAINING_MIN_FREE_GB == 10.0
    assert vp.COMFY_IDLE_SHUTDOWN_S == 300.0
    assert vp.WAKE_DEBOUNCE_S == 10.0


def test_gpu_consumers_alias_single_source() -> None:
    """有模块级名的 GPU 侧消费方=单源对象（别名未断链，无本地回写）。

    vllm_backend / vllm_service 的值为内联表达式使用（无模块级名可
    锁 is），由上测试的值锁 + import 单源静态保证。
    """
    from backend.services import (
        image_queue,
        lora_training_service,
    )
    from backend.services import vram_policy as vp
    from backend.services.inference import comfy_proc

    assert image_queue._WAKE_DEBOUNCE_S is vp.WAKE_DEBOUNCE_S
    assert lora_training_service.MIN_FREE_VRAM_GB is vp.TRAINING_MIN_FREE_GB
    assert comfy_proc.DEFAULT_IDLE_SHUTDOWN_S is vp.COMFY_IDLE_SHUTDOWN_S


def test_vllm_ram_needed_formula() -> None:
    from backend.services.vram_policy import vllm_ram_needed_gb

    assert vllm_ram_needed_gb(10.7) == 13.7
    assert vllm_ram_needed_gb(0.0) == 3.0


def test_consumers_alias_single_source() -> None:
    """消费方符号=单源对象（别名未断链，无本地回写）。"""
    from backend.services import vram_policy as vp
    from backend.services.inference import gen_router, paint_engine

    assert gen_router._QWEN_RAM_FLOOR_GB is vp.QWEN_GGUF_RAM_FLOOR_GB
    assert paint_engine.LOW_VRAM_FALLBACK_GB \
        is vp.PAINT_LOW_VRAM_FALLBACK_GB


def test_dialog_tiers_locked_to_evidence() -> None:
    """对话档位表=实测口径（批4 D2=A；依据见 vram_policy 注释与方案 §1.3）。

    2026-09-10 更新：用户令删除 qwen35-9b-gguf-q4km（Q4 量化思考退化，
    权重已清 5.5G）——llama 共存档随之移除，12GB 基线机 9B 解暂缺
    （回退链 4B 兜底）。"""
    from backend.services import vram_policy as vp

    assert [t.model_id for t in vp.DIALOG_TIERS] == [
        "qwen35-9b-w4a16", "qwen3-vl-8b-awq", "qwen3-vl-4b"]
    assert [t.vram_gb for t in vp.DIALOG_TIERS] == [14.9, 14.8, 9.0]
    assert [t.mode for t in vp.DIALOG_TIERS] == ["vllm", "vllm", "inproc"]
