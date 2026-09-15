"""vLLM 后端：独立子进程服务（runtime/py313，OpenAI 兼容 API）。

架构（2026-08-21 vLLM 集成裁定）：
  - 模型权重不进主进程——显存由 py313 子进程整卡持有（预算制
    gpu_memory_utilization），主进程仅 HTTP 代理推理；
  - 卸载 = 杀整棵进程树（taskkill /T），显存随进程销毁瞬时回收；
  - PagedAttention / continuous batching / prefix caching / AWQ int4
    kernel 全部由 vLLM 提供（不重复造轮子）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ....engines.vllm_service import VLLMService

import logging
from collections.abc import Iterator
from pathlib import Path

from ...vram_policy import (
    VLLM_DFLASH_EXTRA_GB,
    VLLM_FLOOR_OVERHEAD_GB,
    VLLM_MTP_EXTRA_GB,
    VLLM_UTIL_DEFAULT,
    VLLM_UTIL_LARGE_WEIGHTS,
    VLLM_UTIL_MTP,
)
from .base import DialogBackend, estimate_tokens

logger = logging.getLogger("omnispace.inference.backends.vllm")


class VLLMBackend(DialogBackend):
    """AWQ 量化模型后端（compressed-tensors / awq 量化格式路由至此）。"""

    name = "vllm"
    supports_images = True  # Qwen3-VL 经 image_url base64 透传

    @classmethod
    def reattach(cls, model_id: str,
                 model_dir: Path | None) -> VLLMBackend:
        """为已健康运行的 vLLM 服务重挂零成本客户端壳（V9-γ 2026-09-09）。

        绘画让渡后的 wake 后台重启只复活服务（vllm_service 内部线程），
        引擎/台账不知情——本方法不发装载请求、以服务事实为准直接绑定
        （卡 90% 事故根治件：引擎重挂后状态回 ready、台账由
        note_external_load 补记）。
        """
        b = cls()
        b.model_id = model_id
        b.model_dir = model_dir
        b._ready = True
        return b

    def _service(self) -> VLLMService:
        from ....engines.vllm_service import get_vllm_service
        return get_vllm_service()

    def load(self, model_id: str, model_dir: Path | None,
             required_gb: float) -> bool:
        """启动 vLLM 子进程服务（阻塞至健康就绪，权重加载 + 图编译
        可达数分钟；调用方 API 线程应有超时保护）。"""
        svc = self._service()
        if model_dir is None:
            # remote 后端场景外的防御：vLLM 本地装载必须有真实模型目录
            self._last_error = f"模型 {model_id} 缺少本地目录，无法启动 vLLM"
            logger.warning(self._last_error)
            return False
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
            # 长上下文档位（V3 2026-09-08）：config.yaml vllm.max_model_len
            # （0=默认；16384/32768 仅对非大权重模型生效——大权重 4K
            # 让档是显存安全钳制，FP8 KV 实测数据齐后再评估放开）。
            if not big:
                try:
                    from backend.config import get_config as _get_cfg
                    _cfg_len = int((_get_cfg().get("vllm") or {}).get(
                        "max_model_len", 0) or 0)
                    if _cfg_len >= 1024:
                        max_len = _cfg_len
                except Exception:  # noqa: BLE001 - 配置异常保持默认档
                    pass
            util = VLLM_UTIL_LARGE_WEIGHTS if big else VLLM_UTIL_DEFAULT
            # MTP/DFlash 开启时大权重 util 提至 0.92（V6 2026-09-09 冒烟
            # 实测：9B+MTP+前缀缓存 util 0.86 时 KV=-0.81 起不来、0.92
            # 过线；DFlash 草稿更重，同走轻载窗口档）
            _mtp_on = False
            _dflash_on = False
            try:
                from ....engines.vllm_service import (
                    dflash_spec_enabled,
                    mtp_spec_enabled,
                )
                _dflash_on = dflash_spec_enabled(Path(model_dir))
                _mtp_on = (not _dflash_on
                           and mtp_spec_enabled(Path(model_dir)))
            except Exception:  # noqa: BLE001 - 探测失败按 MTP 关
                pass
            if (_mtp_on or _dflash_on) and big:
                util = max(util, VLLM_UTIL_MTP)
                logger.info("%s 开启：大权重 util 提至 %.2f（轻载窗口档）",
                            "DFlash" if _dflash_on else "MTP", util)
            # 显存自适应让档（2026-09-02 漫剧描述词自动加载实测）：静态
            # util 按「模块释放后的空卡」标定，桌面/浏览器常态占 2GB+
            # 时 0.2GB 级差距即被准入闸门拒绝（16GB 卡：需 13.6 空闲
            # 13.4）。装载前实测空闲：不足目标但差口可让时，把 util 降
            # 到「空闲装得下」的档位（KV 预算同步缩，4K 上下文仅需
            # ~0.75GB）；低于可行下限（权重+开销3.4+KV0.8，08-25 实测
            # 口径）→ 诚实报空闲不足。
            if big:
                try:
                    import torch
                    if torch.cuda.is_available():
                        # 多卡绑卡（批1 多卡地基 2026-09-05）：主进程
                        # 可见全部卡，按对话资源域探测指定卡（单卡恒 0）
                        try:
                            from ....engines.gpu_domains import resolve_feature_device
                            _dev_idx = resolve_feature_device("dialog")
                        except Exception:  # noqa: BLE001
                            _dev_idx = 0
                        _free_b, _total_b = torch.cuda.mem_get_info(_dev_idx)
                        free_gb, total_gb = _free_b / 2**30, _total_b / 2**30
                        floor_budget = weight_gb + VLLM_FLOOR_OVERHEAD_GB
                        # MTP 开启时草稿层+图画像多占 ~1.3GB（V6 冒烟
                        # 实测 9B+MTP util 0.86 时 KV=-0.81 起不来）；
                        # DFlash 草稿 2.58GB+开销 ~4GB（P-5 估算口径）——
                        # 准入线同步抬高，宁可早拒不让 vLLM 装到一半才死
                        try:
                            from ....engines.vllm_service import (
                                dflash_spec_enabled,
                                mtp_spec_enabled,
                            )
                            if dflash_spec_enabled(Path(model_dir)):
                                floor_budget += VLLM_DFLASH_EXTRA_GB
                            elif mtp_spec_enabled(Path(model_dir)):
                                floor_budget += VLLM_MTP_EXTRA_GB
                        except Exception:  # noqa: BLE001 - 探测失败按原线
                            pass
                        if free_gb < util * total_gb:
                            fit_util = (free_gb - 0.2) / total_gb
                            if fit_util >= floor_budget / total_gb:
                                logger.info(
                                    "vLLM util 自适应让档: %.2f → %.2f"
                                    "（实测空闲 %.1fGB / 整卡 %.1fGB）",
                                    util, fit_util, free_gb, total_gb)
                                util = fit_util
                            else:
                                self._last_error = (
                                    f"设备空闲显存 {free_gb:.1f}GB 低于该模型"
                                    f"可行下限 ~{floor_budget:.1f}GB（权重 "
                                    f"{weight_gb:.1f}GB + 运行开销与 4K KV "
                                    "预算）；请关闭占用显存的应用后重试")
                                logger.warning("vLLM 装载让档不可行: %s",
                                               self._last_error)
                                return False
                except Exception:  # noqa: BLE001 - 探测失败保持静态档
                    pass
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
        extra_params: dict | None = None,
    ) -> Iterator[str]:
        from ....engines.vllm_service import pil_images_to_b64
        yield from self._service().chat_stream(
            messages,
            images_b64=pil_images_to_b64(images or []),
            temperature=temperature,
            max_tokens=max_new_tokens,
            extra_params=extra_params,
        )

    def count_tokens(self, text: str) -> int:
        # 子进程持有 tokenizer，主进程 HTTP 计数代价不值；粗估即可
        # （build_context 预算截断本就以保守为准）。
        return estimate_tokens(text)
# 本项目仅供学习使用，商业授权请+Q 3559331368
