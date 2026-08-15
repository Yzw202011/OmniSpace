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
