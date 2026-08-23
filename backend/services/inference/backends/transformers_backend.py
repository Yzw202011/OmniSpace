"""transformers 后端：VL 多模态 / 纯文本 LLM 本进程直载。

覆盖 Qwen3-VL（AutoModelForImageTextToText）与 Qwen3/Llama/GLM 等
AutoModelForCausalLM 系；承载知识 LoRA 挂载与热更新（R2-B04）。
推理串行化：单 GPU 单模型实例，并发 generate 叠加 KV 与 logits
显存导致分配器颠簸（实测 16GB 两路并发 prefill 卡死），故持锁。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

from .base import DialogBackend, _try_import, preferred_load_dtype

logger = logging.getLogger("omnispace.inference.backends.transformers")


def _heal_torch_dynamo() -> bool:
    """自愈 torch 编译缓存注册表损坏（进程级偶发，2026-08-22 实锤）。

    根因：launcher 白名单环境曾缺失 USERNAME 等用户身份变量，torch._dynamo
    导入链（package.py 模块级构造 DynamoCache → getpass.getuser()）在无任何
    身份变量时 fallback 到 Unix-only 的 pwd 模块，Windows 上
    ModuleNotFoundError → Python 清理半初始化的 dynamo/transformers 模块，
    但已完整执行的 torch.compiler._cache 保留注册条目（pgo/precompile）；
    此后任何触发 ``import torch._dynamo`` 的路径（transformers 4.5x 的
    flex_attention 集成在 AutoProcessor 懒加载时必经）都会在重复注册处
    抛 ``AssertionError: Artifact of type=... already registered in
    mega-cache artifact factory``，且永远无法自愈。

    修复：按「注册类定义模块是否仍存活」清理注册表残留条目，删除
    dynamo 半成品模块后重导入完整链条。幂等，失败返回 False。
    """
    import os
    import sys
    try:
        # 环境兜底：dynamo 缓存目录解析经 getpass.getuser()，无任何身份
        # 变量时 fallback 到 Unix-only 的 pwd 模块（Windows 必炸，即根因）
        os.environ.setdefault("USERNAME", "omnispace")
        for name in [n for n in sys.modules
                     if n == "torch._dynamo" or n.startswith("torch._dynamo.")]:
            del sys.modules[name]
        from torch.compiler._cache import CacheArtifactFactory
        for art_type, cls in list(CacheArtifactFactory._artifact_types.items()):
            if cls.__module__ not in sys.modules:
                del CacheArtifactFactory._artifact_types[art_type]
        import torch._dynamo  # noqa: F401  重导入（重新注册被清理的类型）
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("torch dynamo 注册表自愈失败: %s", exc)
        return False


class TransformersBackend(DialogBackend):
    """transformers 直载后端（kind: vl=视觉语言 / text=纯文本）。"""

    name = "transformers"

    def __init__(self, kind: Literal["vl", "text"]) -> None:
        super().__init__()
        if kind not in ("vl", "text"):
            raise ValueError(f"未知的 transformers 后端类型: {kind}")
        self.kind = kind
        self.supports_images = kind == "vl"
        self._model: Any = None
        self._processor: Any = None
        self._lora_version: str = ""
        # 推理串行锁：lock 在生成器生命周期内持有（close 时自动释放）
        self._infer_lock = threading.Lock()

    # ── 加载 ────────────────────────────────────────────────────

    def load(self, model_id: str, model_dir: Path,
             required_gb: float) -> bool:
        torch = _try_import("torch")
        transformers = _try_import("transformers")
        if torch is None or transformers is None:
            self._last_error = "torch/transformers 依赖不可用"
            logger.warning("transformers 后端不可用: %s", self._last_error)
            return False

        try:
            logger.info("开始加载对话模型 %s <- %s（transformers/%s）",
                        model_id, model_dir, self.kind)
            if self.kind == "text":
                model, processor = self._load_text(torch, transformers,
                                                   model_dir)
            else:
                model, processor = self._load_vl(torch, transformers,
                                                 model_dir)

            # R2-B04 自主进化闭环：基座加载成功后挂载当前生效的知识
            # LoRA adapter（训练成果影响推理）；失败回退基座，不崩溃。
            model, lora_version = self._attach_knowledge_lora(model,
                                                              model_dir)
            self._model = model
            self._processor = processor
            self._lora_version = lora_version
            self.model_id = model_id
            self.model_dir = model_dir
            self._ready = True
            self._last_error = ""
            logger.info("对话模型加载成功: %s（transformers/%s，知识 LoRA: %s）",
                        model_id, self.kind, lora_version or "无")
            return True
        except Exception as exc:  # noqa: BLE001
            # torch 编译缓存注册表损坏（进程级偶发）：自愈后重试一次
            if ("already registered in mega-cache" in str(exc)
                    and not getattr(self, "_healed", False)):
                self._healed = True
                if _heal_torch_dynamo():
                    logger.warning("检测到 torch dynamo 注册表损坏，已自愈，重试加载")
                    return self.load(model_id, model_dir, required_gb)
            self._last_error = f"对话模型加载失败: {exc}"
            self._model = None
            self._processor = None
            logger.exception("对话模型加载失败")
            return False

    def _load_vl(self, torch, transformers, path: Path):
        """VL 多模态加载路径（Qwen-VL / LLaVA / InternVL 等）。"""
        try:
            processor = transformers.AutoProcessor.from_pretrained(
                str(path), trust_remote_code=True)
        except TypeError:  # 旧版 transformers 无 trust_remote_code 参数
            processor = transformers.AutoProcessor.from_pretrained(str(path))

        model_cls = getattr(transformers, "AutoModelForImageTextToText", None)
        if model_cls is None:
            model_cls = getattr(transformers, "AutoModelForVision2Seq", None)
        if model_cls is None:
            raise RuntimeError("当前 transformers 版本不支持视觉对话模型")

        dtype, extra = preferred_load_dtype(torch)
        try:
            model = model_cls.from_pretrained(
                str(path),
                torch_dtype=dtype,
                device_map="cuda",
                trust_remote_code=True,
                **extra,
            )
        except Exception as exc:
            # Qwen3-VL 类不可用时回退 Vision2Seq
            logger.warning("主加载路径失败(%s)，尝试回退加载", exc)
            fallback_cls = transformers.AutoModelForVision2Seq
            model = fallback_cls.from_pretrained(
                str(path),
                torch_dtype=dtype,
                device_map="cuda",
                trust_remote_code=True,
                **extra,
            )
        return model, processor

    def _load_text(self, torch, transformers, path: Path):
        """纯文本 LLM 加载路径（Qwen3 / Llama / GLM-4 / DeepSeek / Mistral /
        Yi / Phi / 代码模型等，trust_remote_code 覆盖 GLM/MiniCPM 自定义架构）。"""
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            str(path), trust_remote_code=True)
        dtype, extra = preferred_load_dtype(torch)
        model = transformers.AutoModelForCausalLM.from_pretrained(
            str(path),
            torch_dtype=dtype,
            device_map="cuda",
            trust_remote_code=True,
            **extra,
        )
        return model, tokenizer

    # ── 卸载 ────────────────────────────────────────────────────

    def unload(self) -> bool:
        had = self._ready
        self._model = None
        self._processor = None
        self._ready = False
        self.model_id = ""
        self.model_dir = None
        self._lora_version = ""
        return had

    # ── 知识 LoRA（R2-B04）─────────────────────────────────────

    def _attach_knowledge_lora(self, model: Any,
                               model_dir: Path | None) -> tuple[Any, str]:
        """基座加载后挂载当前生效的知识 LoRA adapter。

        查询 lora_training_service 的 current 版本（models/lora/vN，
        adapter_model.bin/safetensors + adapter_config.json），基座匹配时
        用 peft.PeftModel 包装（QLoRA adapter 仅数十 MB，不破坏现有
        generate 调用）。无版本/基座不匹配/peft 缺失/挂载异常 →
        log warning 并回退基座推理，绝不崩溃。

        Returns:
            (模型（可能为 PeftModel 包装）, 已挂载版本号（"" 表示基座）)
        """
        try:
            from ...lora_training_service import (
                LORA_DIR, get_lora_training_service)
            svc = get_lora_training_service()
            version = svc.get_current()
            if not version:
                return model, ""
            adapter_dir = LORA_DIR / version
            if not (adapter_dir / "adapter_config.json").is_file():
                logger.warning("知识 LoRA %s 缺 adapter_config.json，"
                               "回退基座推理", version)
                return model, ""
            # 基座匹配校验：adapter 训练基座须与当前加载模型目录一致
            meta = svc._read_meta(adapter_dir) or {}
            base_name = Path(str(meta.get("base_model") or "")).name
            if (base_name and model_dir is not None
                    and base_name != model_dir.name):
                logger.warning("知识 LoRA %s 训练基座(%s)与当前模型(%s)不匹配，"
                               "跳过挂载（回退基座推理）",
                               version, base_name, model_dir.name)
                return model, ""
            peft = _try_import("peft")
            if peft is None:
                logger.warning("peft 不可用，知识 LoRA %s 跳过挂载"
                               "（回退基座推理）", version)
                return model, ""
            wrapped = peft.PeftModel.from_pretrained(model, str(adapter_dir))
            wrapped.eval()
            logger.info("知识 LoRA 已挂载: %s → %s", version,
                        self.model_id or model_dir)
            return wrapped, version
        except Exception as exc:  # noqa: BLE001 - 挂载失败回退基座
            logger.warning("知识 LoRA 挂载失败，回退基座推理: %s", exc)
            return model, ""

    @property
    def lora_version(self) -> str:
        return self._lora_version

    def refresh_lora(self) -> dict:
        """热更新知识 LoRA（R2-B04）：current 版本变化时卸载旧挂新版。

        低成本切换路径：未 ready 时无需动作（下次 load 自动挂载最新版）；
        已 ready 时经 PeftModel.unload() 退回基座后重新挂载。
        训练完成（lora_training_service._run_task）经引擎调用本方法。
        """
        if not self._ready or self._model is None:
            return {"changed": False, "version": self._lora_version,
                    "reason": "engine_not_ready"}
        try:
            from ...lora_training_service import get_lora_training_service
            target = get_lora_training_service().get_current()
        except Exception:  # noqa: BLE001
            target = ""
        if target == self._lora_version:
            return {"changed": False, "version": self._lora_version}
        base = self._model
        if self._lora_version and hasattr(base, "unload"):
            try:
                base = base.unload()      # 退回基座（卸载旧 adapter）
            except Exception as exc:  # noqa: BLE001
                logger.warning("知识 LoRA 旧版本卸载失败，保持现状: %s", exc)
                return {"changed": False, "version": self._lora_version}
        self._model, self._lora_version = self._attach_knowledge_lora(
            base, self.model_dir)
        logger.info("知识 LoRA 热更新完成: → %s",
                    self._lora_version or "基座")
        return {"changed": True, "version": self._lora_version}

    # ── 推理 ────────────────────────────────────────────────────

    def count_tokens(self, text: str) -> int:
        """token 计数：processor/tokenizer 真实计数，失败按字符估算。"""
        tokenizer = self._processor
        if tokenizer is not None:
            try:
                tok = getattr(tokenizer, "tokenizer", tokenizer)
                return len(tok(text, add_special_tokens=False).input_ids)
            except Exception:
                pass
        # 粗估：中英文混合约 1 token / 1.5 字符
        return max(1, int(len(text) / 1.5))

    def _prepare_inputs(self, messages: list[dict], images: list | None):
        """应用 chat template 并编码输入（vl / text 两类 processor）。"""
        processor = self._processor
        text = processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        if self.kind == "text":
            inputs = processor(text, return_tensors="pt")
        else:
            kwargs: dict[str, Any] = {"text": [text], "return_tensors": "pt"}
            if images:
                kwargs["images"] = images
            inputs = processor(**kwargs)
        try:
            inputs = inputs.to("cuda")
        except AttributeError:
            inputs = {k: (v.cuda() if hasattr(v, "cuda") else v)
                      for k, v in inputs.items()}
        return inputs

    def chat_stream(
        self,
        messages: list[dict],
        images: list | None = None,
        temperature: float = 0.7,
        max_new_tokens: int = 1024,
    ) -> Iterator[str]:
        transformers = _try_import("transformers")

        with self._infer_lock:
            inputs = self._prepare_inputs(messages, images)
            streamer = transformers.TextIteratorStreamer(
                getattr(self._processor, "tokenizer", self._processor),
                skip_prompt=True,
                skip_special_tokens=True,
            )

            gen_kwargs: dict[str, Any] = dict(
                **inputs,
                max_new_tokens=max_new_tokens,
                streamer=streamer,
            )
            if temperature and temperature > 0:
                gen_kwargs.update(do_sample=True, temperature=temperature,
                                  top_p=0.9)
            else:
                gen_kwargs.update(do_sample=False)

            def _generate_worker() -> None:
                """后台生成线程：异常须显式记录并收尾 streamer，
                否则 daemon 线程静默死亡导致主流 0 产出且无法定位。"""
                try:
                    self._model.generate(**gen_kwargs)
                except Exception:  # noqa: BLE001
                    logger.exception("对话生成线程异常")
                    try:
                        streamer.end()
                    except Exception:  # noqa: BLE001
                        pass

            thread = threading.Thread(target=_generate_worker, daemon=True)
            thread.start()

            try:
                for text in streamer:
                    if text:
                        yield text
            finally:
                # 等待生成线程结束，避免后台残留写 streamer
                thread.join(timeout=5.0)
