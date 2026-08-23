"""vLLM 后端：独立子进程服务（runtime/py313，OpenAI 兼容 API）。

架构（2026-08-21 vLLM 集成裁定）：
  - 模型权重不进主进程——显存由 py313 子进程整卡持有（预算制
    gpu_memory_utilization），主进程仅 HTTP 代理推理；
  - 卸载 = 杀整棵进程树（taskkill /T），显存随进程销毁瞬时回收；
  - PagedAttention / continuous batching / prefix caching / AWQ int4
    kernel 全部由 vLLM 提供（不重复造轮子）。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from .base import DialogBackend, estimate_tokens

logger = logging.getLogger("omnispace.inference.backends.vllm")


class VLLMBackend(DialogBackend):
    """AWQ 量化模型后端（compressed-tensors / awq 量化格式路由至此）。"""

    name = "vllm"
    supports_images = True  # Qwen3-VL 经 image_url base64 透传

    def _service(self):
        from ....engines.vllm_service import get_vllm_service
        return get_vllm_service()

    def load(self, model_id: str, model_dir: Path,
             required_gb: float) -> bool:
        """启动 vLLM 子进程服务（阻塞至健康就绪，权重加载 + 图编译
        可达数分钟；调用方 API 线程应有超时保护）。"""
        svc = self._service()
        if not svc.runtime_ready():
            self._last_error = (
                f"模型 {model_id} 为 AWQ 量化格式，需要 vLLM 推理后端；"
                f"运行时未安装: runtime/py313（见 logs/vllm-server.log）")
            logger.warning("vLLM 加载门控: py313 运行时不可用")
            return False

        try:
            logger.info("启动 vLLM 对话服务: %s <- %s", model_id, model_dir)
            if not svc.start(model_dir=str(model_dir)):
                self._last_error = svc._last_error or "vLLM 服务启动失败"
                logger.warning("vLLM 启动失败: %s", self._last_error)
                return False
            self.model_id = model_id
            self.model_dir = model_dir
            self._ready = True
            self._last_error = ""
            logger.info("vLLM 对话服务就绪: %s（独立子进程推理）", model_id)
            return True
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"vLLM 服务启动异常: {exc}"
            logger.exception("vLLM 服务启动异常")
            try:
                svc.stop()
            except Exception:  # noqa: BLE001
                pass
            return False

    def unload(self) -> bool:
        had = self._ready
        if had:
            try:
                self._service().stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("vLLM 子进程停止异常: %s", exc)
        self._ready = False
        self.model_id = ""
        self.model_dir = None
        return had

    def chat_stream(
        self,
        messages: list[dict],
        images: list | None = None,
        temperature: float = 0.7,
        max_new_tokens: int = 1024,
    ) -> Iterator[str]:
        from ....engines.vllm_service import pil_images_to_b64
        yield from self._service().chat_stream(
            messages,
            images_b64=pil_images_to_b64(images or []),
            temperature=temperature,
            max_tokens=max_new_tokens,
        )

    def count_tokens(self, text: str) -> int:
        # 子进程持有 tokenizer，主进程 HTTP 计数代价不值；粗估即可
        # （build_context 预算截断本就以保守为准）。
        return estimate_tokens(text)
