"""vLLM 思考协议接入单测（2026-09-22 用户报「8B 开深度思考没起效」）。

根因三重：qwen3-vl-8b-awq 是 Instruct 版（无原生思考）+ vLLM 链路
2026-09-10 一刀切旁路 + reasoning parser 白名单无 8B。本批修复 =
旁路收窄到原生思考型号（deepseek-r1 / qwen35，配对表单源），其余
vLLM 模型接入【最终回答】标记协议（同 4B 的 transformers 链路）。

dialog_engine 模块导入重——沿袭 house 惯例（test_dialog_idle_watchdog）
源码断言锁结构；vllm_service 导入轻，真导入验配对表。
"""
from __future__ import annotations

from pathlib import Path

from src.engines.vllm_service import VLLM_REASONING_PARSERS

ENGINE_PY = (Path(__file__).resolve().parents[2]
             / "src" / "services" / "inference" / "dialog_engine.py")
SRC = ENGINE_PY.read_text(encoding="utf-8")
# 2026-09-22 架构闸拆分：思考策略面（判定函数/解析器）在 thinking_policy
POLICY_PY = (Path(__file__).resolve().parents[2]
             / "src" / "services" / "inference" / "thinking_policy.py")
POLICY = POLICY_PY.read_text(encoding="utf-8")
DIALOG_PY = (Path(__file__).resolve().parents[2] / "src" / "api" / "dialog.py")
API = DIALOG_PY.read_text(encoding="utf-8")


def test_parser_table_covers_native_families():
    """配对表=启动注入与引擎判定的单一真源，覆盖两族原生思考模型。"""
    assert ("deepseek-r1", "deepseek_r1") in VLLM_REASONING_PARSERS
    assert ("qwen35", "qwen3") in VLLM_REASONING_PARSERS


def test_bypass_narrowed_to_native_models_only():
    """旁路必须按原生思考型号收窄——普通 vLLM 模型（8B）不再被一刀切。"""
    assert "def is_native_reasoning_model(" in POLICY, "判定函数被删"
    assert 'enable_thinking and _bn == "vllm" \\' in SRC, (
        "旁路条件未按原生思考型号收窄（8B 思考开关会继续无效）")
    assert "and is_native_reasoning_model(self._model_id):" in SRC, (
        "旁路条件未按原生思考型号收窄（8B 思考开关会继续无效）")
    assert "VLLM_REASONING_PARSERS" in POLICY, "判定未与启动注入同源"


def test_native_bypass_still_strips_prompt_suffix():
    """原生思考型号旁路时必须剥离残留思考框架（时序差防线）。"""
    marker = 'THINKING_SYSTEM_SUFFIX in m["content"]'
    assert marker in SRC, "旁路分支未剥离残留思考框架"
    assert "m[\"content\"].replace(" in SRC, "剥离实现被删"


def test_marker_parser_path_intact_for_vllm():
    """标记协议路径保留（vLLM 普通模型由此接入思考展示）。"""
    assert "parser = _ThinkingStreamParser()" in SRC, "标记协议路径被删"
    assert "深度思考流式完成[" in SRC, "思考链路完成日志被删"


def test_api_both_paths_use_single_source_gate():
    """WS/REST 两处注入条件同步收窄，旧一刀切旁路不得残留。"""
    assert API.count("is_native_reasoning_model(model_req)") == 2, (
        "WS/REST 注入条件未同步收窄")
    assert 'not in ("vllm", "gguf", "llama")' not in API, (
        "旧一刀切旁路残留")
