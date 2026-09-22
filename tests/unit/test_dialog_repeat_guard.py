"""对话防复读 A+B 案单测（2026-09-22 用户报 8B 写小说复读循环）。

实弹事故：12:18 8B(vllm)+深度思考写 3000 字小说，~26 字对白块连抄
35 次烧光 2048 预算。修复 = A 案本地 vLLM 默认 repetition_penalty
（对话 WS/REST 两路）+ B 案流式复读循环检测截断（引擎三路径）。

dialog_engine 实测导入 0.1s（轻），检测器用真行为断言；接线用源码
断言（house 惯例防静默删改）。
"""
from __future__ import annotations

from pathlib import Path

from src.services.inference.dialog_engine import detect_repeat_loop

ENGINE_PY = (Path(__file__).resolve().parents[2]
             / "src" / "services" / "inference" / "dialog_engine.py")
SRC = ENGINE_PY.read_text(encoding="utf-8")
# 2026-09-22 架构闸拆分：检测器在 thinking_policy，接线在引擎
POLICY_PY = (Path(__file__).resolve().parents[2]
             / "src" / "services" / "inference" / "thinking_policy.py")
POLICY = POLICY_PY.read_text(encoding="utf-8")
DIALOG_PY = (Path(__file__).resolve().parents[2] / "src" / "api" / "dialog.py")
API = DIALOG_PY.read_text(encoding="utf-8")

# 用户 12:18 实弹事故的循环形态（对白块无限循环）
_LOOP_CYCLE = '可你等他回来，他却没回来。"林晚"说。那……你打算等多久？店长问。'


def test_incident_sample_detected():
    """实弹事故形态必须命中（同块连抄 35 次）。"""
    assert detect_repeat_loop("第一章 雨夜的守夜人。" + _LOOP_CYCLE * 35) is True
    assert "def detect_repeat_loop(" in POLICY, "检测器被删（架构拆分后应在 thinking_policy）"


def test_normal_dialogue_not_flagged():
    """正常对白/排比不误伤。"""
    normal = (
        '"你好。"他说。"你好。"她答。'
        "然后他们聊起了天气和各自的旅行计划，聊了很久很久。")
    assert detect_repeat_loop(normal) is False


def test_threshold_is_four_cycles():
    """阈值=同一 ≥8 字块连续 4 次：3 次放过、4 次命中。"""
    assert detect_repeat_loop("ABCDEFGH" * 3) is False
    assert detect_repeat_loop("ABCDEFGH" * 4) is True


def test_short_and_empty_safe():
    assert detect_repeat_loop("") is False
    assert detect_repeat_loop("短文本") is False


def test_watchdog_wired_in_all_three_paths():
    """流式三条路径（gguf/直通/标记协议）都要接检测，防漏网。"""
    assert SRC.count("detect_repeat_loop(seen[-4096:])") == 3, (
        "复读检测未覆盖全部三条流式路径")
    assert SRC.count("extra_params=extra_params") >= 3, (
        "extra_params 管道未贯通三条路径")
    assert "REPEAT_CUT_NOTICE" in SRC, "截断提示文案被删"
    assert "self.last_repeat_cut = True" in SRC, "截断旗标被删"


def test_api_passes_penalty_and_meta():
    """对话 WS/REST 两路都带 repetition_penalty（A 案）+ meta 截断旗标。"""
    assert API.count("extra_params=extra") == 2, "WS/REST 未传防复读参数"
    assert '"repetition_penalty": 1.05' in API, "温和惩罚参数被改"
    assert 'meta["repeat_cut"] = True' in API, "meta 截断旗标被删"
