"""对话推理后端工厂（专属推理框架编排层的可插拔注册点）。

接入新推理引擎（如 TGI / SGLang / tensorrt-llm）三步：
  1. 实现 backends.base.DialogBackend 协议
  2. 在 create_backend 注册 kind → 构造器
  3. dialog_engine._pick_model 返回新 kind 即自动路由

kind 命名（历史兼容，get_status().backend 字段对外稳定）：
  vl / text  → TransformersBackend(kind)
  gguf       → GGUFBackend
  vllm       → VLLMBackend
  remote     → RemoteDialogBackend（批3 D3：远端专业卡服务器，零本地显存）
"""
from __future__ import annotations

from .base import DialogBackend
from .gguf_backend import GGUFBackend
from .remote_backend import RemoteDialogBackend
from .transformers_backend import TransformersBackend
from .vllm_backend import VLLMBackend


def create_backend(kind: str) -> DialogBackend:
    """按探测出的后端类型构造后端实例（未加载状态，load 后才 ready）。"""
    if kind == "vllm":
        return VLLMBackend()
    if kind == "gguf":
        return GGUFBackend()
    if kind in ("vl", "text"):
        return TransformersBackend(kind)
    if kind == "remote":
        # 单例：远端健康状态跨调用保持（load 内做有界健康轮询）
        from .remote_backend import get_remote_backend
        return get_remote_backend()
    raise ValueError(f"未知的对话推理后端类型: {kind}")


__all__ = [
    "DialogBackend",
    "TransformersBackend",
    "GGUFBackend",
    "VLLMBackend",
    "RemoteDialogBackend",
    "create_backend",
]
