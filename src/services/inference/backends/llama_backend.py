"""llama-server 后端：GGUF 共存档子进程推理（显存调度机制批4，2026-09-10）。

与 backends/gguf_backend（llama-cpp-python 进程内绑定，旧架构 GGUF
兼容保留）的差异：qwen3.5 架构 llama-cpp-python 不认（POC 结论，
2026-09-08）→ 官方 llama-server 子进程（OpenAI 兼容 API）是唯一路。
子进程由 engines/llama_service 统一管理（孤儿收账/收养/taskkill 整树，
对齐 vllm_service 模式）；本后端只做协议适配（load→start、chat→HTTP）。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from .base import DialogBackend, estimate_tokens

logger = logging.getLogger("omnispace.inference.backends.llama")


class LlamaServerBackend(DialogBackend):
    """GGUF 共存档后端（纯文本；qwen35-9b-gguf-q4km 常态档，D2=A）。"""

    name = "llama"
    supports_images = False

    def load(self, model_id: str, model_dir: Path | None,
             required_gb: float) -> bool:
        from ....engines.llama_service import get_llama_service

        if model_dir is None:
            self._last_error = f"模型 {model_id} 路径缺失"
            return False
        # model_dir 可能是目录（候选链解析）或 .gguf 文件本身
        gguf = Path(model_dir)
        if gguf.is_dir():
            files = sorted(gguf.glob("*.gguf"))
            if not files:
                self._last_error = (
                    f"模型 {model_id} 目录下无 .gguf 权重: {gguf}")
                logger.warning("llama 后端加载门控: %s", self._last_error)
                return False
            gguf = files[0]
        logger.info("llama-server 加载: %s <- %s", model_id, gguf.name)
        svc = get_llama_service()
        if not svc.start(gguf):
            self._last_error = svc._last_error or "llama-server 启动失败"
            logger.warning("llama 后端加载失败: %s", self._last_error)
            return False
        self.model_id = model_id
        self.model_dir = Path(model_dir)
        self._ready = True
        self._last_error = ""
        return True

    def unload(self) -> bool:
        from ....engines.llama_service import get_llama_service

        self._ready = False
        try:
            return get_llama_service().stop()
        except Exception as exc:  # noqa: BLE001 - 停止失败不阻断卸载链
            logger.warning("llama-server 停止异常: %s", exc)
            return False

    def chat_stream(
        self,
        messages: list[dict],
        images: list | None = None,
        temperature: float = 0.7,
        max_new_tokens: int = 1024,
        extra_params: dict | None = None,
    ) -> Iterator[str]:
        # images 由编排层已按 supports_images=False 拦截，此处不处理
        from ....engines.llama_service import get_llama_service

        svc = get_llama_service()
        if not svc.is_healthy():
            self._ready = False
            raise RuntimeError(
                svc._last_error or "llama-server 服务未就绪")
        yield from svc.chat_stream(
            messages,
            temperature=temperature,
            max_tokens=max_new_tokens,
            extra_params=extra_params,
        )

    def count_tokens(self, text: str) -> int:
        # 子进程持有 tokenizer，主进程粗估（与 vllm_backend 同策略）
        return estimate_tokens(text)
# 本项目仅供学习使用，商业授权请+Q 3559331368
