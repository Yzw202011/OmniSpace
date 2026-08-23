"""vLLM 多模态对话链路测试（2026-08-21 vLLM 集成）。

分层设计：
- 量化格式检测（纯逻辑，随默认套件跑）：拦截 2026-08-21 事故——
  Qwen3-VL-8B AWQ 官方包实为 compressed-tensors 格式，
  _is_awq_model 只认 awq 导致误走 transformers 路径加载失败
- 多模态图片理解（e2e，显式 opt-in）：加载模型→带图流式对话→
  断言回复命中视觉特征→卸载回收。图片未真正送达模型时（纯文本
  应答"看不到图片"）无法通过断言。

e2e 运行方式（需 GPU + 模型 + py313 运行时）:
  set OMNISPACE_VLLM_E2E=1
  runtime\\py310\\python.exe -m pytest backend/tests/test_vllm_multimodal.py -v
默认套件（tools/run_tests.py）自动跳过 e2e，不装载 GPU 模型。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models" / "qwen3-vl-8b-awq"
PY313_EXE = ROOT / "runtime" / "py313" / "python.exe"


def _write_model_config(tmp_path: Path, quant_method: str) -> Path:
    """构造最小对话模型目录（config.json + tokenizer_config.json）。"""
    (tmp_path / "config.json").write_text(json.dumps({
        "model_type": "qwen3_vl",
        "architectures": ["Qwen3VLForConditionalGeneration"],
        "quantization_config": {"quant_method": quant_method},
    }), encoding="utf-8")
    (tmp_path / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    return tmp_path


# ── 量化格式检测（纯逻辑，离线可跑） ─────────────────────────

@pytest.mark.parametrize("quant_method", ["awq", "compressed-tensors"])
def test_vllm_only_quant_formats_routed_to_vllm(tmp_path, quant_method):
    """awq / compressed-tensors 量化目录必须路由到 vllm 后端。

    py310 主进程无 autoawq / compressed_tensors 包，transformers
    加载必失败；只有 vLLM 子进程（py313）能推理这两种格式。
    """
    from backend.services.inference.dialog_engine import _detect_backend
    d = _write_model_config(tmp_path, quant_method)
    assert _detect_backend(d) == "vllm", (
        f"quant_method={quant_method} 应路由 vllm 后端")


def test_bf16_vl_model_not_routed_to_vllm(tmp_path):
    """未量化 VL 目录保持 vl 后端（transformers 直载，不绕道子进程）。"""
    from backend.services.inference.dialog_engine import _detect_backend
    d = _write_model_config(tmp_path, quant_method="")
    # 空串等价于无量化声明：剔除 quantization_config 再判定
    cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))
    cfg.pop("quantization_config")
    (d / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    assert _detect_backend(d) == "vl"


# ── 多模态 e2e（opt-in，需 GPU + 模型 + py313 运行时） ────────

_assets_ready = (MODEL_DIR / "config.json").is_file() and PY313_EXE.is_file()
_optin = os.environ.get("OMNISPACE_VLLM_E2E") == "1"


@pytest.mark.skipif(
    not _optin, reason="vLLM GPU 集成测试需显式开启: OMNISPACE_VLLM_E2E=1")
@pytest.mark.skipif(
    _optin and not _assets_ready,
    reason="qwen3-vl-8b-awq 模型或 runtime/py313 运行时未安装")
def test_multimodal_image_understanding():
    """端到端：PIL 图片 → build_context → vLLM image_url → 视觉理解。

    测试图 = 白底 + 中央红色实心大圆 + 右下蓝色方块；附带 1 轮文字
    历史。断言回复命中「红」+「圆」（附加「蓝」「方」不计失败）——
    只有图片真正送达并被视觉编码才可能命中。
    """
    from PIL import Image, ImageDraw

    from backend.services.inference.dialog_engine import get_dialog_engine

    img = Image.new("RGB", (448, 448), "white")
    d = ImageDraw.Draw(img)
    d.ellipse([84, 84, 364, 364], fill=(220, 30, 30))
    d.rectangle([316, 316, 420, 420], fill=(30, 60, 220))

    eng = get_dialog_engine()
    try:
        assert eng.load_model("qwen3-vl-8b-awq"), eng._last_error
        assert eng.get_status()["backend"] == "vllm", (
            f"后端应为 vllm，实际 {eng.get_status()['backend']}")

        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮你？"},
        ]
        messages = eng.build_context(
            "这张图片里有什么？请用一句话描述其中的形状和颜色。",
            history=history, images=[img])
        # build_context 多模态 content 应为列表格式（图片占位 + 文本）
        assert isinstance(messages[-1]["content"], list), (
            f"user content 应为列表格式，实际 {type(messages[-1]['content'])}")

        t0 = time.time()
        first_ms = None
        reply = ""
        chunks = 0
        for text in eng.chat_stream(messages, images=[img],
                                    temperature=0.1, max_new_tokens=128):
            if first_ms is None and text:
                first_ms = (time.time() - t0) * 1000
            if text:
                chunks += 1
                reply += text
        print(f"\n回复: {reply}")
        print(f"首token {first_ms:.0f}ms, {chunks} 片段, "
              f"总 {(time.time()-t0)*1000:.0f}ms")

        assert reply.strip(), "回复为空"
        assert "红" in reply and "圆" in reply, (
            f"未同时识别红色+圆形，多模态链路疑似断开: {reply[:200]}")
    finally:
        assert eng.unload_model(), "卸载失败（显存未回收）"


# ── 多模型热切换 e2e（opt-in，2026-08-21 按需换载） ──────────

_M4B_DIR = ROOT / "models" / "qwen3-vl-4b"
_hot_assets_ready = (
    _assets_ready and (_M4B_DIR / "config.json").is_file())


@pytest.mark.skipif(
    not _optin, reason="vLLM GPU 集成测试需显式开启: OMNISPACE_VLLM_E2E=1")
@pytest.mark.skipif(
    _optin and not _hot_assets_ready,
    reason="qwen3-vl-4b 或 qwen3-vl-8b-awq 模型未就绪")
def test_model_hot_switch_vllm_transformers():
    """多模型热切换：8b-awq(vllm) ⇄ 4b(transformers) 双向换载。

    覆盖 2026-08-21 热切换链路：
    1. 首载 8b-awq → vllm 后端 ready + 推理有产出
    2. 切 4b → vllm 子进程被杀（显存回收）+ transformers 就绪 + 推理
    3. 切回 8b-awq → vllm 重启 + served_name 动态正确 + 推理
       （served_name 若未随模型更新，chat 请求会 404 / 模型不匹配）
    4. 卸载收尾（显存回基线）
    """
    from backend.engines.vllm_service import get_vllm_service
    from backend.services.inference.dialog_engine import get_dialog_engine

    eng = get_dialog_engine()
    svc = get_vllm_service()

    def _chat_once(label: str) -> str:
        messages = eng.build_context("用一句话自我介绍你是谁。")
        reply = "".join(eng.chat_stream(messages, temperature=0.1,
                                        max_new_tokens=64))
        assert reply.strip(), f"{label} 回复为空（推理链路断开）"
        return reply

    try:
        # ── 1. 首载 8b-awq（vllm）──
        t0 = time.time()
        assert eng.load_model("qwen3-vl-8b-awq"), eng._last_error
        st = eng.get_status()
        assert st["backend"] == "vllm", f"应为 vllm，实际 {st['backend']}"
        assert svc.is_healthy(), "vLLM 服务应健康"
        vst = svc.status()
        assert vst["served_name"] == "qwen3-vl-8b-awq", (
            f"served_name 应为模型目录名，实际 {vst['served_name']}")
        print(f"\n[热切换] 1) 8b-awq vllm 就绪 ({time.time()-t0:.0f}s)")
        _chat_once("8b-awq(vllm)")

        # ── 2. 热切换到 4b（transformers）──
        t0 = time.time()
        assert eng.load_model("qwen3-vl-4b"), eng._last_error
        st = eng.get_status()
        assert st["backend"] == "vl", f"应为 vl，实际 {st['backend']}"
        assert not svc.is_running(), (
            "切走后 vLLM 子进程应被杀（显存须回收，否则 4b 加载必 OOM）")
        print(f"[热切换] 2) → 4b transformers 就绪 ({time.time()-t0:.0f}s, "
              f"vllm 进程已终止)")
        _chat_once("4b(transformers)")

        # ── 3. 热切换回 8b-awq（vllm 重启 + served_name 动态） ──
        t0 = time.time()
        assert eng.load_model("qwen3-vl-8b-awq"), eng._last_error
        st = eng.get_status()
        assert st["backend"] == "vllm", f"应为 vllm，实际 {st['backend']}"
        assert svc.is_healthy(), "vLLM 服务应重启健康"
        vst = svc.status()
        assert vst["served_name"] == "qwen3-vl-8b-awq", (
            f"二次换载后 served_name 应正确，实际 {vst['served_name']}")
        print(f"[热切换] 3) → 8b-awq vllm 重新就绪 ({time.time()-t0:.0f}s)")
        _chat_once("8b-awq(vllm 二次)")
    finally:
        assert eng.unload_model(), "卸载失败（显存未回收）"
        assert not svc.is_running(), "卸载后 vLLM 子进程应已终止"
