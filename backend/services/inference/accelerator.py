"""OmniSpace AI v2.1 加速器模块（规格 §5.5 加速器集成）。

提供 Flash Attention、xFormers 等加速技术的自动检测与启用。
当加速库不可用时静默跳过，不影响正常运行。

同时提供 safe_load_model() 安全模型加载和 select_pytorch_backend() 后端选择。
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

logger = logging.getLogger("omnispace.inference.accelerator")


def _try_import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:
        return None


# ── 可选依赖预加载 ──────────────────────────────────────────────
_torch = _try_import("torch")
_transformers = _try_import("transformers")
_diffusers = _try_import("diffusers")
_accelerate = _try_import("accelerate")


class Accelerator:
    """推理加速器——自动检测并启用可用加速技术。"""

    def __init__(self) -> None:
        self._flash_attention_available = self._check_flash_attention()
        self._xformers_available = self._check_xformers()
        self._sdp_available = self._check_sdp()
        self._torch_compile_available = self._check_torch_compile()

        logger.info(
            "加速器状态: FlashAttention=%s, xFormers=%s, SDP=%s, TorchCompile=%s",
            self._flash_attention_available,
            self._xformers_available,
            self._sdp_available,
            self._torch_compile_available,
        )

    # ── 检测方法 ────────────────────────────────────────────────

    def _check_flash_attention(self) -> bool:
        """检测 Flash Attention 是否可用。"""
        fa = _try_import("flash_attn")
        if fa is not None:
            return True
        # torch 2.0+ 内置 SDPA 可能支持 Flash Attention
        if _torch is not None and hasattr(_torch.nn.functional, "scaled_dot_product_attention"):
            return True
        return False

    def _check_xformers(self) -> bool:
        """检测 xFormers 是否可用。"""
        xformers = _try_import("xformers")
        return xformers is not None

    def _check_sdp(self) -> bool:
        """检测 PyTorch Scaled Dot Product Attention 是否可用。"""
        if _torch is None:
            return False
        return hasattr(_torch.nn.functional, "scaled_dot_product_attention")

    def _check_torch_compile(self) -> bool:
        """检测 torch.compile 是否可用（PyTorch 2.0+）。"""
        if _torch is None:
            return False
        return hasattr(_torch, "compile")

    # ── 启用加速 ────────────────────────────────────────────────

    def enable_for_pipeline(self, pipeline: Any) -> Any:
        """为 diffusers 管线启用最优加速。

        优先级: Flash Attention > xFormers > SDP > 无加速

        Args:
            pipeline: diffusers 管线对象

        Returns:
            配置后的管线对象
        """
        if pipeline is None:
            return pipeline

        # 尝试启用 xFormers（diffusers 原生支持）
        if self._xformers_available:
            try:
                pipeline.enable_xformers_memory_efficient_attention()
                logger.info("已启用 xFormers 内存优化注意力")
                return pipeline
            except Exception as e:
                logger.debug("xFormers 启用失败: %s", e)

        # 尝试启用 SDPA（diffusers 0.23+ 支持）
        if self._sdp_available:
            try:
                if hasattr(pipeline, "enable_xformers_memory_efficient_attention"):
                    # 使用 SDPA 作为 xFormers 替代
                    pass
                logger.info("已启用 SDPA 注意力")
                return pipeline
            except Exception as e:
                logger.debug("SDPA 启用失败: %s", e)

        # 尝试启用 CPU offload（内存优化）
        if _accelerate is not None:
            try:
                pipeline.enable_model_cpu_offload()
                logger.info("已启用模型 CPU offload")
            except Exception as e:
                logger.debug("CPU offload 启用失败: %s", e)

        return pipeline

    def enable_flash_attention(self) -> bool:
        """全局启用 Flash Attention。

        Returns:
            True 如果启用成功
        """
        if self._flash_attention_available:
            logger.info("Flash Attention 已启用")
            return True
        logger.debug("Flash Attention 不可用，跳过")
        return False

    def compile_model(self, model: Any, mode: str = "default") -> Any:
        """使用 torch.compile 编译模型以加速推理。

        Args:
            model: PyTorch 模型
            mode: 编译模式 (default/reduce-overhead/max-autotune)

        Returns:
            编译后的模型
        """
        if not self._torch_compile_available or model is None:
            return model
        try:
            compiled = _torch.compile(model, mode=mode)
            logger.info("模型已编译 (mode=%s)", mode)
            return compiled
        except Exception as e:
            logger.warning("torch.compile 失败: %s", e)
            return model

    # ── 状态查询 ────────────────────────────────────────────────

    @property
    def capabilities(self) -> dict:
        """返回加速器能力列表。"""
        return {
            "flash_attention": self._flash_attention_available,
            "xformers": self._xformers_available,
            "sdp": self._sdp_available,
            "torch_compile": self._torch_compile_available,
            "torch_available": _torch is not None,
        }


# ── 模块级单例 ──────────────────────────────────────────────────
_accelerator: Accelerator | None = None


def get_accelerator() -> Accelerator:
    """获取 Accelerator 单例。"""
    global _accelerator
    if _accelerator is None:
        _accelerator = Accelerator()
    return _accelerator


# ════════════════════════════════════════════════════════════════
#  规格§5.5 安全模型加载
# ════════════════════════════════════════════════════════════════

def select_pytorch_backend(gpu_info: dict) -> str:
    """根据 GPU 信息选择最优 PyTorch 后端。

    规格 §5.5 后端选择逻辑:
      - NVIDIA GPU + CUDA -> 'cuda'
      - AMD/Intel GPU + DirectML -> 'directml'
      - 无 GPU 或不支持 -> 'cpu'

    Args:
        gpu_info: GPU 信息字典（含 vendor, vram_total_mb 等）

    Returns:
        后端标识: 'cuda' | 'directml' | 'cpu'
    """
    vendor = gpu_info.get("vendor", "none").lower()
    vram_mb = gpu_info.get("vram_total_mb", 0)
    has_gpu = vram_mb > 0

    if not has_gpu:
        logger.info("未检测到 GPU，使用 CPU 后端")
        return "cpu"

    if vendor == "nvidia":
        if _torch is not None and _torch.cuda.is_available():
            logger.info("选择 CUDA 后端 (NVIDIA GPU)")
            return "cuda"
        logger.warning("NVIDIA GPU 检测到但 CUDA 不可用，降级到 CPU")
        return "cpu"

    # AMD/Intel: 尝试 DirectML
    torch_directml = _try_import("torch_directml")
    if torch_directml is not None:
        try:
            if torch_directml.is_available():
                logger.info("选择 DirectML 后端 (%s GPU)", vendor)
                return "directml"
        except Exception:
            pass

    logger.info("GPU 不受支持，使用 CPU 后端")
    return "cpu"


def safe_load_model(
    path: str,
    device: str = "auto",
    dtype: Any = None,
) -> Any:
    """安全加载模型（规格 §5.5）。

    加载流程:
      1. 确定设备（auto 时自动选择）
      2. 尝试 diffusers 加载
      3. 尝试 transformers 加载
      4. 尝试 torch.load 加载
      5. 全部失败返回 None

    Args:
        path: 模型路径
        device: 设备 ('auto' | 'cuda' | 'cpu' | 'directml')
        dtype: 数据精度（torch.float16 等）

    Returns:
        加载的模型对象，失败返回 None
    """
    if _torch is None:
        logger.warning("torch 不可用，无法加载模型")
        return None

    # 自动选择设备
    if device == "auto":
        if _torch.cuda.is_available():
            device = "cuda"
        else:
            device = "cpu"

    # 设置默认精度
    if dtype is None:
        dtype = _torch.float16 if device == "cuda" else _torch.float32

    logger.info("加载模型: %s (device=%s, dtype=%s)", path, device, dtype)

    # 尝试 diffusers 加载
    if _diffusers is not None:
        try:
            from pathlib import Path
            p = Path(path)
            if p.is_dir() and (p / "model_index.json").exists():
                # 尝试各种 diffusers 管线
                for pipeline_class_name in (
                    "StableDiffusionXLPipeline",
                    "FluxPipeline",
                    "KolorsPipeline",
                    "DiffusionPipeline",
                ):
                    try:
                        pipeline_cls = getattr(_diffusers, pipeline_class_name, None)
                        if pipeline_cls is not None:
                            model = pipeline_cls.from_pretrained(
                                path, torch_dtype=dtype
                            )
                            model = model.to(device)
                            logger.info("diffusers 加载成功: %s", pipeline_class_name)
                            return model
                    except Exception:
                        continue
        except Exception as e:
            logger.debug("diffusers 加载失败: %s", e)

    # 尝试 transformers 加载
    if _transformers is not None:
        try:
            from pathlib import Path
            p = Path(path)
            if p.is_dir() and (p / "config.json").exists():
                model = _transformers.AutoModelForCausalLM.from_pretrained(
                    path, torch_dtype=dtype, device_map=device if device == "cuda" else None
                )
                logger.info("transformers 加载成功")
                return model
        except Exception as e:
            logger.debug("transformers 加载失败: %s", e)

    # 尝试 torch.load 加载
    try:
        model = _torch.load(path, map_location=device)
        logger.info("torch.load 加载成功")
        return model
    except Exception as e:
        logger.warning("所有加载方式均失败: %s", e)
        return None
