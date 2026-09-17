"""外挂 LLM 提供者插件测试（DF v0.9.1 跟版 2026-09-17）。

不依赖真后端：验证未配置时 fail-safe（available=False → on_think 返回
None 并广播 llm.failed）、权重不含 key（敏感信息只进记忆不进包）、
不可达后端返回 None 不抛错。上游对应测试长在 test_openai_server.py
（咱未跟版该服务，此为适配版）。
"""
from __future__ import annotations

from src.cutemamen.llm_provider import LLMProviderPlugin
from src.services.plugin_runtime.base import PluginContext


def test_unconfigured_plugin_unavailable():
    p = LLMProviderPlugin("llm-provider")
    assert p.available is False
    assert p.generate({"messages": []}) is None


def test_on_think_without_backend_emits_failure():
    p = LLMProviderPlugin("llm-provider")
    captured: list[tuple[str, object]] = []
    ctx = PluginContext(emit_fn=lambda t, pl: captured.append((t, pl)))
    result = p.on_think({"topic": "llm", "data": {"messages": [
        {"role": "user", "content": "hi"}]}}, ctx)
    assert result is None
    assert captured and captured[0][0] == "llm.failed"
    assert captured[0][1]["reason"] == "backend-unavailable"


def test_save_weights_excludes_key():
    """key 属敏感信息：只进记忆不进包（权重/清单均无 key）。"""
    p = LLMProviderPlugin("llm-provider", base="http://127.0.0.1:11434/v1",
                          model="qwen2.5-coder:7b")
    weights = p.save_weights()
    assert set(weights) == {"llm_base", "llm_model", "provider"}
    manifest = p.build_manifest()
    assert "llm_base" in manifest and "key" not in manifest


def test_unreachable_backend_returns_none():
    p = LLMProviderPlugin("llm-provider")
    p.base = "http://127.0.0.1:9"  # 保留端口，本机必拒绝
    assert p.available is True
    assert p.generate({"messages": [{"role": "user", "content": "x"}]}) is None
