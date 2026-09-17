"""外挂 LLM 提供者插件 — 符合 .CuteMamen 插件标准 (v0.9.1)

把"外挂本地 OpenAI 兼容 LLM 作代码生成后端"封装成标准 CuteMamen 插件
(生命周期 on_load/on_think/on_unload、路由路由、三级记忆、可存档包、可注册
发现交付 .CuteMamen), 供 openai_server 等宿主按插件机制消费 ——
取代早前服务器内硬编码的 urllib 转发。

规范 §: 思考插件按 base_model 注册表解码, 宿主无需 import 本模块。

route: "llm"  —— 向内核 think({"topic":"llm","data":<OpenAI payload>})
即可驱动一次真实代码生成。未配置后端 (base=None) 时 available=False,
宿主据此回退纯 CubeGPT 本地模态。生成失败返回 None, 不抛错。

后端取值优先级: 构造参数 > 环境变量
    CUBEGPT_LM_BASE   baseURL, 例 http://127.0.0.1:11434/v1 (Ollama)
    CUBEGPT_LM_KEY    后端 Bearer key (Ollama 常为 "ollama")
    CUBEGPT_LM_MODEL  后端模型名, 例 qwen2.5-coder:7b
"""

import json
import os
import urllib.error
import urllib.request
from typing import Any

import numpy as np

from .plugin import ExpertPlugin, PluginContext

BASE_MODEL = "llm.provider"

ENV_BASE = "CUBEGPT_LM_BASE"
ENV_KEY = "CUBEGPT_LM_KEY"
ENV_MODEL = "CUBEGPT_LM_MODEL"

_DEFAULT_KEY = "ollama"  # Ollama 兼容网关的无鉴权约定


class LLMProviderPlugin(ExpertPlugin):
    CAPABILITY = "外挂 OpenAI 兼容 LLM 后端 (Ollama/llama.cpp/...) · 大模型落地代码生成"
    BASE_MODEL = BASE_MODEL

    def __init__(self, name: str = "llm-provider", *, route: str | None = None,
                 base: str | None = None, key: str | None = None,
                 model: str | None = None, **kw: Any):
        super().__init__(name, route=route or "llm", capability=kw.pop(
            "capability", self.CAPABILITY), version=kw.pop("version", "0.1.0"),
            author=kw.pop("author", "DistributedFormer"))
        self.base = base if base is not None else os.environ.get(ENV_BASE)
        self.key = key if key is not None else os.environ.get(ENV_KEY, _DEFAULT_KEY)
        self.model = model if model is not None else os.environ.get(ENV_MODEL)
        self.memory.remember("provider", "openai-compatible")
        self.memory.remember("llm_base", self.base or "-")

    # ── 生命周期 ────────────────────────────────────────────
    def on_load(self, ctx: PluginContext) -> None:
        super().on_load(ctx)
        self.memory.remember("llm_base", self.base or "-")
        self.memory.remember("provider", "openai-compatible")

    def on_think(self, event: dict[str, Any],
                 ctx: PluginContext) -> dict[str, Any] | None:
        """路由主题 llm: 转发真实请求 → OpenAI 兼容后端 → 返回生成文本"""
        super().on_think(event, ctx)  # 记录情景记忆
        data = event.get("data", {})
        payload = data if isinstance(data, dict) and "messages" in data \
            else {"messages": [{"role": "user", "content": str(data)}]}
        content = self.generate(payload)
        if content is None:
            if ctx is not None:
                ctx.emit("llm.failed",
                         {"reason": "backend-unavailable",
                          "base": self.base or "-"})
            return None
        if ctx is not None:
            ctx.emit("llm.generated",
                     {"length": len(content), "model": self.model or "<req>"})
        return {"content": content, "provider": "openai-compatible",
                "model": self.model}

    # ── 生成 (宿主 / 测试直接调用) ──────────────────────────
    @property
    def available(self) -> bool:
        return bool(self.base)

    def generate(self, payload: dict[str, Any]) -> str | None:
        """把完整 OpenAI payload 转发到后端, 返回 choices[0] 文本。

        后端没起 / 非200 / 字段缺失 → None (不回退、不抛错)。
        """
        if not self.available:
            return None
        body = dict(payload)
        body.pop("stream", None)
        if self.model:
            body["model"] = self.model
        base = self.base or ""
        req = urllib.request.Request(
            base.rstrip("/") + "/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return content if isinstance(content, str) else None
        except (urllib.error.URLError, OSError, KeyError, ValueError,
                TypeError):
            return None

    # ── 权重 / 清单 ─────────────────────────────────────────
    def save_weights(self) -> dict[str, np.ndarray]:
        # 代码生成端点信息持久化到权重 (key 属敏感信息, 只进记忆不进包)
        return {
            "llm_base": np.asarray(self.base or ""),
            "llm_model": np.asarray(self.model or ""),
            "provider": np.asarray("openai-compatible"),
        }

    def load_weights(self, weights: dict[str, np.ndarray],
                     manifest: dict[str, Any]) -> None:
        def _s(k: str) -> str | None:
            arr = weights.get(k)
            if arr is None:
                return None
            s = str(np.asarray(arr))
            s = s.strip("[]' ")
            return s or None
        base = _s("llm_base") or manifest.get("llm_base")
        model = _s("llm_model") or manifest.get("llm_model")
        if base:
            self.base = base
        if model:
            self.model = model

    @classmethod
    def from_pkg(cls, manifest: dict[str, Any]) -> "LLMProviderPlugin":
        # 基类签名返回 ExpertPlugin；cls 即本类，实例必为 LLMProviderPlugin
        inst: LLMProviderPlugin = super().from_pkg(  # type: ignore[assignment]
            manifest)
        inst.base = manifest.get("llm_base") or inst.base
        inst.model = manifest.get("llm_model") or inst.model
        inst.key = os.environ.get(ENV_KEY, _DEFAULT_KEY)
        return inst

    def build_manifest(self, **extra: Any) -> dict[str, Any]:
        return super().build_manifest(
            provider="openai-compatible",
            llm_base=self.base or "",
            llm_model=self.model or "",
            **extra)

    # 便捷: 单位化统计行 (宿主打印用)
    def describe(self) -> str:
        state = {True: "已配置", False: "未配置"}[self.available]
        return (f"LLMProvider({self.route}) 后端{state}"
                f"{(' → ' + self.base) if self.base else ''}"
                f", model={self.model or '<由请求指定>'}")
