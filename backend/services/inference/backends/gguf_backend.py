"""GGUF 后端：llama.cpp（llama-cpp-python）本地推理。

文档《显存阶梯参考》主力量化格式；显存不足时由 n_gpu_layers 自动
截断 GPU offload 层数（部分层落 CPU），故编排层无须硬闸显存。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .base import DialogBackend, _try_import, estimate_tokens

logger = logging.getLogger("omnispace.inference.backends.gguf")


class GGUFBackend(DialogBackend):
    """GGUF 量化模型后端（纯文本，不支持图片输入）。"""

    name = "gguf"
    supports_images = False

    def __init__(self) -> None:
        super().__init__()
        self._llm: Any = None
        # 推理串行锁：llama.cpp 单实例并发 create_chat_completion 会
        # 叠加 KV 缓存显存；lock 在生成器生命周期内持有。
        self._infer_lock = __import__("threading").Lock()

    def load(self, model_id: str, model_dir: Path,
             required_gb: float) -> bool:
        llama_cpp = _try_import("llama_cpp")
        if llama_cpp is None:
            self._last_error = (
                f"模型 {model_id} 为 GGUF 格式，需要 llama.cpp 推理后端；"
                "请执行: pip install llama-cpp-python 后重启服务"
            )
            logger.warning("GGUF 加载门控: llama-cpp-python 不可用")
            return False

        torch = _try_import("torch")
        cuda_ok = bool(torch is not None and torch.cuda.is_available())

        try:
            logger.info("开始加载 GGUF 对话模型 %s <- %s", model_id, model_dir)
            self._llm = llama_cpp.Llama(
                model_path=str(model_dir),
                n_ctx=8192,
                n_gpu_layers=-1 if cuda_ok else 0,  # -1=尽可能全量 offload
                verbose=False,
            )
            self.model_id = model_id
            self.model_dir = model_dir
            self._ready = True
            self._last_error = ""
            logger.info("GGUF 对话模型加载成功: %s（GPU offload: %s）",
                        model_id, "全量尝试" if cuda_ok else "纯 CPU")
            return True
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"GGUF 模型加载失败: {exc}"
            self._llm = None
            logger.exception("GGUF 模型加载失败")
            return False

    def unload(self) -> bool:
        had = self._ready
        self._llm = None  # 交由 gc 回收（llama.cpp 句柄随对象释放）
        self._ready = False
        self.model_id = ""
        self.model_dir = None
        return had

    @staticmethod
    def _flatten_messages(messages: list[dict]) -> list[dict]:
        """把 chat messages 压平为 llama.cpp 兼容格式（content 必须为 str）。"""
        flat: list[dict] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                content = "".join(
                    str(p.get("text", "")) for p in content
                    if isinstance(p, dict) and p.get("type") == "text")
            flat.append({"role": m.get("role", "user"),
                         "content": content or ""})
        return flat

    def chat_stream(
        self,
        messages: list[dict],
        images: list | None = None,
        temperature: float = 0.7,
        max_new_tokens: int = 1024,
    ) -> Iterator[str]:
        # images 由编排层已按 supports_images 放行/丢弃，此处不再处理
        with self._infer_lock:
            kwargs: dict[str, Any] = dict(
                messages=self._flatten_messages(messages),
                stream=True,
                max_tokens=max_new_tokens,
            )
            if temperature and temperature > 0:
                kwargs.update(temperature=temperature, top_p=0.9)
            else:
                kwargs.update(temperature=0.0)
            for chunk in self._llm.create_chat_completion(**kwargs):
                text = ""
                try:
                    text = (chunk["choices"][0].get("delta") or {}) \
                        .get("content") or ""
                except Exception:  # noqa: BLE001
                    continue
                if text:
                    yield text

    def count_tokens(self, text: str) -> int:
        # llama.cpp 分词经 C API 逐次调用有锁开销；粗估满足预算截断需求
        return estimate_tokens(text)
