"""GPU 资源域单源（批1 多卡地基，2026-09-05）。

「功能 → 显卡」分配的唯一裁决入口（实施计划 §1.1，
docs/新模块与多显卡实施计划-2026-09-05.md）：

- 单卡机器（可用设备 ≤1）恒返主卡——旧行为逐比特不变（本批安全网铁律）；
- 多卡机器按 config.yaml gpu.feature_devices 分派；配置指向不存在的卡
  回落主卡并告警，绝不允许因配置错误起不来；
- paint/video_gen 共用同一个 ComfyUI 单实例（comfy_proc 端口单例），
  二者必须同卡：配置分置时告警并收敛到 paint 的卡；
- 预留 REMOTE_DOMAIN="remote" 伪域（批3 远程引擎）：远程对话不占本地
  卡，与本地功能天然并行（feature_lock 按域互斥时远程=独立域）。

消费方：feature_lock（按域互斥）、vram_manager（按卡记账）、
vllm_service / comfy_proc（子进程绑卡 CUDA_VISIBLE_DEVICES）、
model_manager.release_for_module（按卡释放）。
"""
# 本项目仅供学习使用，商业授权请+Q 3559331368
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("omnispace.engines.gpu_domains")

# 远程推理伪域（批3 远程引擎预留）：不对应本地 CUDA 设备
REMOTE_DOMAIN = "remote"

# 与 feature_lock._VALID_FEATURES 对齐（重量级 AI 功能四锁）
_VALID_FEATURES = ("dialog", "paint", "video_gen", "training")

# 设备枚举缓存（pynvml/torch 枚举有开销；acquire 路径非热频，TTL 从宽）
_CACHE_TTL_S = 30.0
_cache_lock = threading.Lock()
_cached_indexes: list[int] | None = None
_cached_at = 0.0


def primary_device() -> int:
    """主计算卡索引（config gpu.primary_device；异常回落 0）。"""
    try:
        from ..config import GPU_PRIMARY_DEVICE
        return int(GPU_PRIMARY_DEVICE)
    except Exception:  # noqa: BLE001 - 配置异常按单卡 0 语义
        return 0


def feature_devices_config() -> dict[str, int]:
    """读 config 的功能→卡分配表（非法项在 config.py 已过滤）。"""
    try:
        from ..config import GPU_FEATURE_DEVICES
        return dict(GPU_FEATURE_DEVICES)
    except Exception:  # noqa: BLE001
        return {}


def _device_indexes() -> list[int]:
    """枚举可用 CUDA 设备索引（TTL 缓存；全失败回落 [0] 保底单卡语义）。"""
    global _cached_indexes, _cached_at
    with _cache_lock:
        if _cached_indexes is not None and (
                time.monotonic() - _cached_at < _CACHE_TTL_S):
            return list(_cached_indexes)
    idxs: list[int] = []
    try:
        import torch
        if torch.cuda.is_available():
            idxs = list(range(torch.cuda.device_count()))
    except Exception:  # noqa: BLE001 - torch 缺失/异常降级 pynvml
        idxs = []
    if not idxs:
        try:
            from .gpu_backend import enumerate_gpus
            idxs = [int(d.get("index", i))
                    for i, d in enumerate(enumerate_gpus())]
        except Exception:  # noqa: BLE001
            idxs = []
    if not idxs:
        # 无 CUDA（纯记账/CI/降级环境）：按「主卡 0」单卡语义运行
        idxs = [0]
    with _cache_lock:
        _cached_indexes, _cached_at = list(idxs), time.monotonic()
    return list(idxs)


def invalidate_cache() -> None:
    """清设备枚举缓存（设备热插拔/测试注入 mock 后调用）。"""
    global _cached_indexes
    with _cache_lock:
        _cached_indexes = None


def resolve_assignments(device_indexes: list[int] | None = None) -> dict[str, int]:
    """归一化「功能→卡」分配表（校验 + 收敛）。

    单卡机器（可用设备 ≤1）全部收敛到主卡——这是「单卡行为逐比特
    不变」铁律的落点；多卡按 feature_devices 配置，坏索引回落主卡。
    """
    idxs = device_indexes if device_indexes is not None else _device_indexes()
    primary = primary_device()
    if primary not in idxs:
        primary = idxs[0] if idxs else 0
    if len(idxs) <= 1:
        return {f: primary for f in _VALID_FEATURES}

    cfg = feature_devices_config()
    out: dict[str, int] = {}
    for feat in _VALID_FEATURES:
        dev = int(cfg.get(feat, primary))
        if dev not in idxs:
            logger.warning(
                "gpu.feature_devices.%s=%s 指向不存在的卡（可用=%s），"
                "回落主卡 %d", feat, dev, idxs, primary)
            dev = primary
        out[feat] = dev
    # paint/video_gen 必须同卡（共用同一 ComfyUI 实例，端口单例）
    if out["paint"] != out["video_gen"]:
        logger.warning(
            "paint(%d) 与 video_gen(%d) 配置异卡，但二者共用同一 "
            "ComfyUI 实例，自动收敛到 paint 的卡", out["paint"],
            out["video_gen"])
        out["video_gen"] = out["paint"]
    return out


def resolve_feature_device(feature: str) -> int:
    """feature → CUDA 设备索引。

    单卡恒返主卡（逐比特旧行为）；未知 feature 按主卡（安全缺省，
    如 embedding/voice 等小模型类别跟随主卡）。
    """
    if feature not in _VALID_FEATURES:
        return primary_device()
    return resolve_assignments().get(feature, primary_device())


def resolve_feature_domain(feature: str) -> str:
    """feature → 资源域标识（本地域=卡号字符串）。

    批3 远程引擎（2026-09-05）：对话由远端专业卡服务器承载时归入
    REMOTE_DOMAIN 伪域——远程对话不占本地卡，与本地绘画天然并行。
    """
    if feature == "dialog":
        try:
            from ..services.inference.backends.remote_backend import is_remote_dialog_enabled
            if is_remote_dialog_enabled():
                return REMOTE_DOMAIN
        except Exception:  # noqa: BLE001 - 探测失败按本地域
            pass
    return str(resolve_feature_device(feature))
