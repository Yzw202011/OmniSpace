"""分格链模型选型 helper（2026-09-23 方案2 自 keyframe.py 拆出——
巨石只拆不涨：keyframe.py 基线 3134 行已无增长空间）。

漫画页「模型设置」保存的 manga-paint 槽 default 与分格出图的接线
三件套：显式模型读取 / 生成前让渡 / PuLID 软锁降级。
语义 = 用户显式选择 > 自动选路（切换诚实性同语义）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import logging

log = logging.getLogger("omnispace.api.manga.keyframe_model_slot")


def explicit_comfy_model() -> str:
    """读 manga-paint 槽用户显式选择的 comfy 底座。

    返回模型 id（∈ comfy 栈真源）或空串（智能路由/读取失败/名单外
    → 走既有 auto 选路，行为零变化）。
    """
    from ...api.models import module_default_model
    from ...services.inference.comfy_paint_engine import (
        _COMFY_PAINT_MODEL_IDS,
    )
    try:
        mid = str(module_default_model("manga-paint") or "").strip()
    except Exception:  # noqa: BLE001 - 配置读取失败走 auto 旧行为
        log.debug("manga-paint 槽 default 读取失败（走 auto）",
                  exc_info=True)
        return ""
    return mid if mid in _COMFY_PAINT_MODEL_IDS else ""


def yield_for_explicit(model_id: str) -> None:
    """大显存底座（z/qwen21）生成前让渡驻留模型（方案书预警项：
    qwen21 峰值 95.3%，驻留对话模型在场必然撞准入闸）。
    release_for_module 自带功能锁保护（用户对话活跃时 dialog 类别
    跳过不卸）；失败交引擎准入闸兜底，不阻断。
    """
    if model_id.startswith("flux2-klein"):
        return  # klein 家族 dynamic offload 自理（历史行为）
    try:
        from ...services.model_manager import get_model_manager
        rel = get_model_manager().release_for_module("paint")
        log.info("显式底座 %s 生成前让渡: 卸 %s（%.1fGB，completed=%s）",
                 model_id, rel.get("freed_models") or "无",
                 rel.get("freed_vram_gb", 0.0), rel.get("completed"))
    except Exception:  # noqa: BLE001 - 让渡失败交准入闸兜底
        log.debug("生成前让渡失败（准入闸兜底）", exc_info=True)


def is_soft_lock_model(model_id: str) -> bool:
    """z/qwen21 底座无 PuLID 身份硬锁——角色锚走参考软锁。"""
    return bool(model_id) and not model_id.startswith("flux2-klein")


def apply_soft_lock(model_id: str, params: dict) -> None:
    """显式 z/qwen21 时的 PuLID 软处理（params 内强度置零 + 如实日志）。

    能力边界非静默降级：face 条目留在参考条件里（此刻唯一一致性
    通道），一致性弱于 klein 家族的 PuLID attention 硬锁。
    """
    params["pulid_strength"] = 0.0
    params["pulid_strength_b"] = 0.0
    log.info("显式底座 %s 无 PuLID 身份硬锁——角色锚降级参考软锁"
             "（一致性弱于 klein 家族，如实告知）", model_id)
