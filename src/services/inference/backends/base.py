"""对话推理后端统一协议（专属推理框架编排层，2026-08-21 架构裁定）。

设计思想（参考 vLLM 的分层解耦）：
  - kernel 层（PagedAttention / 量化 kernel / llama.cpp）不重复造轮子，
    由 vLLM 子进程 / transformers / llama.cpp 各自承载；
  - 编排层（选型、物理显存闸门、腾挪协调、上下文组装、统一指标计时）
    收敛在 DialogEngine；
  - 本包定义 Backend 协议：load/unload/chat_stream/count_tokens 可插拔，
    新增推理引擎（如未来接入 TGI / SGLang）只需实现本协议并注册工厂。

依赖方向（无环）：
  base.py ← vllm_backend / gguf_backend / transformers_backend ← dialog_engine
  本模块禁止 import dialog_engine（其模块级探测函数留在原处，单测契约）。
"""
from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

log = logging.getLogger("omnispace.inference.backends")


def _try_import(name: str) -> Any:
    """容错导入：可选依赖缺失返回 None（调用方自行门控）。"""
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001
        return None


def _precision_pref() -> str:
    """读取 models.config.precision 加载偏好（bf16/fp16/fp32/int8/int4）。

    读取失败一律回退 bf16（历史默认行为）。
    """
    try:
        import json as _json

        from ....data.database import get_db_safe
        db = get_db_safe()
        if db is not None:
            row = db.query_one(
                "SELECT value FROM system_settings WHERE key='models.config'")
            if row:
                return str((_json.loads(row["value"]) or {}).get(
                    "precision", "bf16")).lower()
    except Exception:  # noqa: BLE001
        log.debug("_precision_pref: 降级忽略", exc_info=True)
    return "bf16"


def preferred_load_dtype(torch: ModuleType) -> tuple:
    """读取 models.config.precision 加载偏好（MODEL-034，PUT /models/config）。

    P3 统一口径：先经 gpu_backend 精度×量化兼容矩阵裁决，得到当前硬件
    真正可用的生效精度，再映射 dtype——避免 Pre-Ampere / DirectML / CPU
    上按 bf16 硬加载失败（原各处口径漂移根因）。

    Returns:
        (dtype, extra_kwargs)：bf16/fp16/fp32 直接映射；int8/int4 需
        bitsandbytes（未安装回退并记日志，绝不伪造量化）。
    """
    from ....engines import gpu_backend

    prec = _precision_pref()
    # 2026-08-24 修复：resolve_precision 无参调用时内部 select_backend({})
    # 只看传入 gpu_info（空 dict → vram=0 → 恒判 CPU 档），CUDA 卡上
    # bf16 也被错误回落 fp32——4B 权重 fp32≈16GB 恰好塞满 GPU 且吞吐
    # 减半（实测首 token 131s 根因）。此处主动用 torch 探测构造 gpu_info。
    gpu_info: dict = {}
    if torch is not None:
        try:
            if torch.cuda.is_available():
                props = torch.cuda.get_device_properties(0)
                gpu_info = {
                    "vendor": "nvidia",
                    "vram_total_mb": int(props.total_memory // (1024 * 1024)),
                    "compute_capability": f"{props.major}.{props.minor}",
                }
        except Exception:  # noqa: BLE001 - 探测失败维持空 dict（CPU 档）
            gpu_info = {}
    # 兼容矩阵裁决：请求精度不可用（如 CPU 请求 bf16）时回落到该档位
    # 真实可用的精度，并记录 warned 供前端提示降级链路
    spec = gpu_backend.resolve_precision(prec, gpu_info=gpu_info)
    resolved = spec["resolved"]
    quant = spec["quant"]

    if resolved == "fp16":
        return torch.float16, {}
    if resolved == "fp32":
        return torch.float32, {}
    if resolved in ("int8", "int4") and quant:
        if _try_import("bitsandbytes") is not None:
            log.info("按兼容矩阵/%s 偏好启用 %s 量化加载",
                        spec["compute_class"], resolved)
            return torch.bfloat16, ({"load_in_8bit": True}
                                    if resolved == "int8" else {"load_in_4bit": True})
        log.warning("bitsandbytes 未安装，%s 量化不可用，回退 bf16", resolved)
    return torch.bfloat16, {}


def estimate_tokens(text: str) -> int:
    """无 tokenizer 时的粗估：中英文混合约 1 token / 1.5 字符。"""
    return max(1, int(len(text) / 1.5))


class DialogBackend(ABC):
    """对话推理后端协议：一个后端实例 = 一次已加载的模型会话。

    生命周期：create → load() → chat_stream()*N → unload()。
    load/unload 由 DialogEngine 在引擎锁内串行调用；chat_stream 的并发
    策略（是否串行化）由各后端自治——transformers/gguf 须串行（单模型
    实例叠加 KV 显存颠簸），vLLM 天然并发（子进程 HTTP 层批处理）。

    后端不得直接抛异常给编排层以外的调用方：失败一律收敛为
    load() 返回 False + last_error() 提供原因。
    """

    # 后端标识：transformers / gguf / vllm
    name: ClassVar[str] = ""
    # 是否支持图片输入（多模态）
    supports_images: ClassVar[bool] = False

    def __init__(self) -> None:
        self.model_id: str = ""
        self.model_dir: Path | None = None
        self._ready: bool = False
        self._last_error: str = ""

    # ── 生命周期 ────────────────────────────────────────────────

    @abstractmethod
    def load(self, model_id: str, model_dir: Path | None,
             required_gb: float) -> bool:
        """加载模型。显存腾挪协调由编排层负责（load 前置闸门），
        后端只关注自身介质的加载细节。失败时 last_error() 必有原因。

        model_dir 可为 None（批3 remote 后端：远端服务器承载，无本地
        目录）。"""

    @abstractmethod
    def unload(self) -> bool:
        """卸载模型并释放后端持有的资源（进程/句柄/显存引用）。

        Returns:
            是否有模型被卸载（未加载时返回 False）
        """

    # ── 推理 ────────────────────────────────────────────────────

    @abstractmethod
    def chat_stream(
        self,
        messages: list[dict],
        images: list | None = None,
        temperature: float = 0.7,
        max_new_tokens: int = 1024,
        extra_params: dict | None = None,
    ) -> Iterator[str]:
        """流式推理：逐段产出文本片段。

        中断检查（stop_check）由编排层在外循环统一处理，后端无须关心；
        vLLM 子进程链路如需断连检测可自行实现。

        extra_params: 采样扩展参数透传（如 repetition_penalty，2026-09-05
        小说长文防复读接入）；不支持的后端可忽略。
        """

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """token 计数：有 tokenizer 用真实计数，否则 estimate_tokens 粗估。"""

    # ── 状态 ─────────────────────────────────────────────────────

    def is_ready(self) -> bool:
        return self._ready

    def last_error(self) -> str:
        return self._last_error

    @property
    def lora_version(self) -> str:
        """当前挂载的知识 LoRA 版本（"" 表示基座推理；仅 transformers 支持）。"""
        return ""
# 本项目仅供学习使用，商业授权请+Q 3559331368
