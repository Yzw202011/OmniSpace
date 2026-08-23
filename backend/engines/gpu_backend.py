"""OmniSpace AI v2.1 GPU 后端选择器（规格 §5.5 后端选择）。

根据 GPU 硬件信息选择最优的 PyTorch 计算后端:
  - NVIDIA GPU -> CUDA
  - AMD/Intel GPU -> DirectML
  - 无 GPU 或不支持 -> CPU
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

logger = logging.getLogger("omnispace.engines.gpu_backend")


def _try_import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_torch = _try_import("torch")
_torch_directml = _try_import("torch_directml")


# ── 后端标识 ────────────────────────────────────────────────────
BACKEND_CUDA = "cuda"
BACKEND_DIRECTML = "directml"
BACKEND_CPU = "cpu"


def select_backend(gpu_info: dict) -> str:
    """根据 GPU 信息选择最优后端。

    规格 §5.5 选择逻辑:
      1. 检查是否有 NVIDIA GPU 且 CUDA 可用 -> 'cuda'
      2. 检查是否有 AMD/Intel GPU 且 DirectML 可用 -> 'directml'
      3. 以上均不可用 -> 'cpu'

    Args:
        gpu_info: GPU 信息字典，应包含:
            - vendor: 'nvidia' | 'amd' | 'intel' | 'none'
            - vram_total_mb: 显存总量（MB）
            - compute_capability: 计算能力（如 "8.6"）

    Returns:
        后端标识: 'cuda' | 'directml' | 'cpu'
    """
    vendor = str(gpu_info.get("vendor", "none")).lower()
    vram_mb = gpu_info.get("vram_total_mb", 0)
    has_gpu = vram_mb > 0

    # 无 GPU
    if not has_gpu:
        logger.info("GPU 后端选择: CPU（未检测到 GPU）")
        return BACKEND_CPU

    # NVIDIA + CUDA
    if vendor == "nvidia":
        if _torch is not None:
            try:
                if _torch.cuda.is_available():
                    device_name = _torch.cuda.get_device_name(0)
                    logger.info("GPU 后端选择: CUDA (%s)", device_name)
                    return BACKEND_CUDA
            except Exception as e:
                logger.warning("CUDA 检测失败: %s", e)

        logger.warning("NVIDIA GPU 检测到但 CUDA 不可用，降级到 CPU")
        return BACKEND_CPU

    # AMD/Intel + DirectML
    if vendor in ("amd", "intel", "advanced micro devices"):
        if _torch_directml is not None:
            try:
                if _torch_directml.is_available():
                    device_count = _torch_directml.device_count()
                    device_name = _torch_directml.device_name(0)
                    logger.info(
                        "GPU 后端选择: DirectML (%s, %d 设备)",
                        device_name,
                        device_count,
                    )
                    return BACKEND_DIRECTML
            except Exception as e:
                logger.warning("DirectML 检测失败: %s", e)

        logger.warning("%s GPU 检测到但 DirectML 不可用，降级到 CPU", vendor)
        return BACKEND_CPU

    # 未知 vendor
    logger.info("GPU 后端选择: CPU（未知 GPU vendor: %s）", vendor)
    return BACKEND_CPU


def get_device(backend: str = "auto", gpu_info: dict = None) -> Any:
    """获取 PyTorch 设备对象。

    Args:
        backend: 后端标识 ('auto' | 'cuda' | 'directml' | 'cpu')
        gpu_info: GPU 信息（backend='auto' 时使用）

    Returns:
        torch.device 对象（如果 torch 可用），否则返回字符串
    """
    if _torch is None:
        return backend

    if backend == "auto":
        backend = select_backend(gpu_info or {})

    if backend == BACKEND_CUDA:
        return _torch.device("cuda")
    elif backend == BACKEND_DIRECTML and _torch_directml is not None:
        return _torch_directml.device()
    else:
        return _torch.device("cpu")


def is_gpu_available() -> bool:
    """检查是否有可用的 GPU 后端。"""
    if _torch is not None and _torch.cuda.is_available():
        return True
    if _torch_directml is not None:
        try:
            return _torch_directml.is_available()
        except Exception:
            pass
    return False


def get_backend_info() -> dict:
    """返回当前可用的后端信息。"""
    info = {
        "torch_available": _torch is not None,
        "cuda_available": _torch is not None and _torch.cuda.is_available(),
        "directml_available": _torch_directml is not None,
        "recommended_backend": BACKEND_CPU,
    }

    if info["cuda_available"]:
        info["recommended_backend"] = BACKEND_CUDA
        try:
            info["cuda_device_name"] = _torch.cuda.get_device_name(0)
            info["cuda_version"] = _torch.version.cuda
            props = _torch.cuda.get_device_properties(0)
            info["cuda_compute_capability"] = f"{props.major}.{props.minor}"
            info["cuda_vram_total_mb"] = int(props.total_memory // (1024 * 1024))
        except Exception:
            pass
    elif info["directml_available"]:
        try:
            if _torch_directml.is_available():
                info["recommended_backend"] = BACKEND_DIRECTML
                info["directml_device_name"] = _torch_directml.device_name(0)
        except Exception:
            pass

    return info


# ═══════════════════════════════════════════════════════════════════
#  精度 × 量化兼容矩阵（P3 一次性收敛，规格 §4.6）
# ───────────────────────────────────────────────────────────────────
# 原三处引擎（对话/绘画/视频）各自硬编码精度回退导致口径漂移：例如
# forptunes 无条件按 bf16 加载，在 Pre-Ampere（无硬件 bf16）、DirectML、
# 纯 CPU 上会实际加载失败。本矩阵把「硬件计算档位 → 可用精度/量化」收敛为
# 单一来源，由 resolve_precision() 统一裁决，各引擎只消费裁决结果。
#
# 计算档位（compute_class）：
#   cuda-ampere+  NVIDIA Ampere 及以上（CC≥8）：bf16/fp16/fp32 + bitsandbytes 量化
#   cuda-legacy   NVIDIA Pre-Ampere（CC<8）：无硬件 bf16，回落 fp16；量化可用
#   directml      AMD/Intel via DirectML：fp16/fp32；bitsandbytes 量化不可用
#   cpu           纯 CPU：仅 fp32；半精与量化不可用
PRECISION_QUANT_MATRIX: dict[str, dict] = {
    "cuda-ampere+": {
        "label": "NVIDIA（Ampere 及以上）",
        "precisions": ("bf16", "fp16", "fp32", "int8", "int4"),
        "quant_supported": True,
        "note": "bf16/fp16/fp32 原生；量化经 bitsandbytes",
    },
    "cuda-legacy": {
        "label": "NVIDIA（Pre-Ampere，计算能力 <8）",
        "precisions": ("fp16", "fp32", "int8", "int4"),
        "quant_supported": True,
        "note": "无硬件 bf16，驻留精度回落 fp16；量化经 bitsandbytes",
    },
    "directml": {
        "label": "DirectML（AMD/Intel）",
        "precisions": ("fp16", "fp32"),
        "quant_supported": False,
        "note": "DirectML 后端经 ONNX 转换，torch bitsandbytes 量化与 bf16 不可用",
    },
    "cpu": {
        "label": "纯 CPU",
        "precisions": ("fp32",),
        "quant_supported": False,
        "note": "无 CUDA 加速，仅 fp32；半精与量化不可用",
    },
}

# 请求精度不可用时的回落偏好：优先 fp16（CUDA/DirectML 通用），
# 无则 fp32（纯 CPU）。
_FALLBACK_PREC_PREF = ("fp16", "fp32", "bf16")


def detect_compute_class(backend: str = "", gpu_info: dict | None = None) -> str:
    """解析当前硬件计算档位（cuda-ampere+ / cuda-legacy / directml / cpu）。

    Args:
        backend: 已由 select_backend 得出的后端；空则按 gpu_info 自动选择
        gpu_info: 含 compute_capability / vendor / vram_total_mb 的硬件信息
    """
    if not backend:
        backend = select_backend(gpu_info or {})
    if backend != BACKEND_CUDA:
        return BACKEND_DIRECTML if backend == BACKEND_DIRECTML else "cpu"
    cc = (gpu_info or {}).get("compute_capability") or ""
    if not cc and _torch is not None:
        try:
            if _torch.cuda.is_available():
                props = _torch.cuda.get_device_properties(0)
                cc = f"{props.major}.{props.minor}"
        except Exception:
            cc = ""
    try:
        major = int(str(cc).split(".")[0])
    except Exception:
        major = 7  # 未知计算能力视为 Pre-Ampere，谨慎回落 fp16
    return "cuda-ampere+" if major >= 8 else "cuda-legacy"


def _pick_fallback_precision(requested: str, supported: tuple[str, ...]) -> str:
    """按回落偏好选择该档位真正可用的生效精度。"""
    for cand in _FALLBACK_PREC_PREF:
        if cand in supported:
            return cand
    return supported[0]


def resolve_precision(requested: str = "bf16", backend: str = "",
                      gpu_info: dict | None = None) -> dict:
    """精度×量化兼容矩阵裁决：把请求精度解析为当前硬件可用的生效精度。

    统一引擎精度口径（P3）：返回 {compute_class, label, requested, resolved,
    quant_supported, quant, warned, note, supported_precisions}。
    请求精度不可用时不硬加载（避免 Pre-Ampere/DirectML/CPU 崩溃），
    回落到矩阵允许的档位并置 warned 供前端提示。

    Args:
        requested: 请求精度（bf16/fp16/fp32/int8/int4）
        backend:   后端标识；空则自动选择
        gpu_info:  硬件信息（含 compute_capability）
    """
    requested = str(requested or "bf16").lower()
    cls = detect_compute_class(backend, gpu_info)
    spec = PRECISION_QUANT_MATRIX.get(cls, PRECISION_QUANT_MATRIX["cpu"])
    supported = spec["precisions"]

    if requested in supported:
        resolved, warned = requested, False
    else:
        resolved = _pick_fallback_precision(requested, supported)
        warned = True
        logger.warning(
            "精度兼容矩阵：请求 %s 在档位 %s 不可用，回落 %s（%s）",
            requested, cls, resolved, spec.get("note", ""))

    # 真实量化仅在"量化支持档位 + 请求为 int 且裁决仍为 int"时成立
    quant = (requested in ("int8", "int4") and resolved in ("int8", "int4")
             and spec["quant_supported"])
    return {
        "compute_class": cls,
        "label": spec["label"],
        "requested": requested,
        "resolved": resolved,
        "quant_supported": spec["quant_supported"],
        "quant": quant,
        "warned": warned,
        "note": spec.get("note", ""),
        "supported_precisions": list(supported),
    }


def get_effective_spec(requested: str = "bf16", gpu_info: dict | None = None) -> dict:
    """当前硬件生效精度规格（供 /hardware 与前端档位提示消费，P3-②）。

    返回 resolve_precision 结果 + recommended_backend，前端据此对
    DirectML/CPU 降级链路给出诚实提示。
    """
    info = get_backend_info()
    spec = resolve_precision(requested, info.get("recommended_backend", ""), gpu_info)
    spec["recommended_backend"] = info.get("recommended_backend", BACKEND_CPU)
    return spec


# ── 生成管线的实际运行设备与降级提示（P3-② DirectML/CPU 全链路诚信打通）─
# 现实约束：torch-directml 无法稳定运行 diffusers/vLLM 重管线，因此
# DirectML 机器的生成仍经 CPU 档位（int4-cpu / sdxl-cpu 等量化模型）运行。
# 本函数把「实际运行设备 + 计算档位 + 降级提示」收敛为单一来源：各引擎
# 不再各自硬编码 "cuda if available else cpu"，前端据此对 DirectML/CPU
# 呈现诚实降级说明，避免声称"DirectML 已加速"的虚假承诺。
_DEFAULT_DEVICE = "cpu"


def resolve_device(precision: str = "bf16", gpu_info: dict | None = None) -> dict:
    """解析生成管线的实际运行设备与降级提示（uniform）。

    Returns:
        {device, precision, compute_class, backend, directml_available,
         degraded, reason, resolved}：
        - device:  实际可选 torch 后端（cuda/cpu；DirectML 机器按 cpu 运行）
        - backend: 硬件上最优推荐的加速后端（cuda/directml/cpu）
        - degraded: 是否处于降级档（非 CUDA 全加速）
        - reason:  给前端的中文诚实说明
    """
    spec = get_effective_spec(precision, gpu_info)
    info = get_backend_info()
    backend = info.get("recommended_backend", BACKEND_CPU)
    if backend == BACKEND_CUDA:
        device, degraded, reason = "cuda", False, ""
    elif backend == BACKEND_DIRECTML:
        device = "cpu"  # 见模块 docstring：重管线经 CPU 档运行
        degraded = True
        reason = ("检测到 AMD/Intel GPU（DirectML），但 AI 生成管线（diffusers/"
                  "vLLM）当前经 CPU 档量化模型运行，DirectML 加速未接入。")
    else:
        device = "cpu"
        degraded = True
        reason = ("未检测到可用的 NVIDIA CUDA 加速，当前为纯 CPU 慢速档位，"
                  "采用量化/轻量模型运行，生成速度较慢。")
    spec.update({
        "device": device,
        "backend": backend,
        "directml_available": bool(info.get("directml_available")),
        "degraded": degraded,
        "reason": reason,
    })
    return spec


# ═══════════════════════════════════════════════════════════════════
#  多卡枚举与设备计划（P3 §5.3：默认单卡，双卡仅显式开启并校验）
# ───────────────────────────────────────────────────────────────────
def enumerate_gpus() -> list[dict]:
    """枚举全部 NVIDIA GPU（pynvml 优先，降级 torch.cuda）。

    Returns:
        [{index, name, vram_total_mb, vram_free_mb, compute_capability, available}]，
        无 GPU / 探测失败返回空列表。
    """
    devices: list[dict] = []
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        try:
            for i in range(pynvml.nvmlDeviceGetCount()):
                h = pynvml.nvmlDeviceGetHandleByIndex(i)
                mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                try:
                    cc = ".".join(
                        str(_) for _ in
                        pynvml.nvmlDeviceGetCudaComputeCapability(h))
                except Exception:
                    cc = ""
                devices.append({
                    "index": i,
                    "name": str(pynvml.nvmlDeviceGetName(h)),
                    "vram_total_mb": int(mem.total // (1024 * 1024)),
                    "vram_free_mb": int(mem.free // (1024 * 1024)),
                    "compute_capability": cc,
                    "available": True,
                })
        finally:
            pynvml.nvmlShutdown()
        return devices
    except Exception:  # noqa: BLE001 - pynvml 缺失/失败降级 torch
        pass

    if _torch is not None:
        try:
            if _torch.cuda.is_available():
                for i in range(_torch.cuda.device_count()):
                    props = _torch.cuda.get_device_properties(i)
                    devices.append({
                        "index": i,
                        "name": str(props.name),
                        "vram_total_mb": int(props.total_memory // (1024 * 1024)),
                        "compute_capability": f"{props.major}.{props.minor}",
                        "available": True,
                    })
        except Exception:  # noqa: BLE001
            pass
    return devices


def resolve_device_plan(gpu_info: dict | None = None) -> dict:
    """多卡设备计划（P3 §5.3）：主计算卡 + 辅助卸载卡。

    双卡策略默认关闭；仅当 config `gpu.secondary_offload=true` 且辅助索引
    有效、与主卡不同时才启用辅助卸载卡，否则回退单卡（方案风险控制，
    避免错误路由变慢）。设备平面暴露给 /hardware 与引擎（视频可据此把
    embed/aux 压到辅助卡）。

    Returns:
        {device_count, devices, primary, auxiliary, secondary_offload,
         secondary_active}
    """
    from ..config import GPU_AUXILIARY_DEVICE, GPU_PRIMARY_DEVICE, GPU_SECONDARY_OFFLOAD
    devices = enumerate_gpus()
    primary = next((d for d in devices if d["index"] == GPU_PRIMARY_DEVICE),
                   ({"index": GPU_PRIMARY_DEVICE, "name": "", "available": False}
                    if devices else {"index": 0, "name": "", "available": False}))
    auxiliary: dict = {"index": None}
    secondary_active = False
    if GPU_SECONDARY_OFFLOAD and len(devices) > 1:
        cand = [d for d in devices
                if d["index"] == GPU_AUXILIARY_DEVICE
                and d["index"] != primary["index"]]
        if cand:
            auxiliary, secondary_active = cand[0], True
        else:
            logger.warning(
                "双卡策略已开启但辅助卡索引 %d 无效或不唯一，回退单卡",
                GPU_AUXILIARY_DEVICE)
    return {
        "device_count": len(devices),
        "devices": devices,
        "primary": primary,
        "auxiliary": auxiliary,
        "secondary_offload": GPU_SECONDARY_OFFLOAD,
        "secondary_active": secondary_active,
    }
