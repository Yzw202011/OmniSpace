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
            # KV cache 预算自适应（2026-08-25 W4A16 14B 实测）：权重
            # ≥8GB 时 util 0.85 装载后 KV 仅剩 ~1.0GB，8K 上下文需
            # 1.5GB 启动失败（vLLM 报 estimated maximum model length
            # is 5632）→ 该档位降 4K 上下文 + util 提至 0.87（16GB 卡
            # 装载后 KV 可用 ~1.5GB，4K 需 0.75GB，余量充足）。
            # 小权重（如 4B AWQ ~4GB）保持 8192/0.85 不变。
            weight_gb = self._weights_size_gb(model_dir)
            big = weight_gb >= 8.0
            max_len = 4096 if big else 8192
            util = 0.87 if big else 0.85
            logger.info("vLLM 启动参数: weights=%.1fGB → max_len=%d util=%.2f",
                        weight_gb, max_len, util)
            if not svc.start(model_dir=str(model_dir),
                             gpu_memory_utilization=util,
                             max_model_len=max_len):
                self._last_error = svc._last_error or "vLLM 服务启动失败"
                logger.warning("vLLM 启动失败: %s", self._last_error)
                return False
            self.model_id = model_id
            self.model_dir = model_dir
            self._ready = True
            self._last_error = ""
            # 多模态能力按模型实测架构判定（类级 True 是为 Qwen3-VL：
            # DeepSeek-R1 等 CausalLM 纯文本模型发图会 vLLM 400）
            self.supports_images = self._model_supports_images(model_dir)
            logger.info("vLLM 对话服务就绪: %s（独立子进程推理，视觉=%s）",
                        model_id, self.supports_images)
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
        self.supports_images = True  # 恢复类级默认（下次 load 重新判定）
        return had

    @staticmethod
    def _model_supports_images(model_dir: Path) -> bool:
        """config.json 的 model_type 是否为视觉架构（qwen*_vl 系）。"""
        try:
            import json
            with open(Path(model_dir) / "config.json",
                      encoding="utf-8") as f:
                raw = json.load(f)
            return str(raw.get("model_type") or "").endswith("_vl")
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _weights_size_gb(model_dir: Path) -> float:
        """权重文件（.safetensors/.bin）总大小 GB——KV 预算分档依据。"""
        try:
            total = sum(
                p.stat().st_size for p in Path(model_dir).iterdir()
                if p.suffix in (".safetensors", ".bin"))
            return total / (1024 ** 3)
        except Exception:  # noqa: BLE001
            return 0.0

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
