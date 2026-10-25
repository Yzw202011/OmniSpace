"""对话装载失败快速出口（fail-fast）哨兵+行为测（2026-10-25 拍板）。

事故（backend.log 10:47）：9B 被准入闸拒绝（空闲 5.0GB < 下限 16.2GB）
后，_wait_vllm_booting 只认 ready/200s 超时，引擎已 error 仍干等到底，
用户看到两条「冷启动中已自动排队」气泡各挂 200 秒。

契约：
  1) 等待期间引擎进入终态失败（error/unavailable）且 vLLM 无
     booting/运行在途 → 立即返回 False（不许干等超时路径）；
  2) vLLM 在途（booting/运行）或 state 非终态 → 照旧等待
     （防误杀真冷启动/自动重试收养窗口，2026-09-02 P1 语义不回退）；
  3) caller 终态失败给 MODEL_LOAD_FAILED 且文案携带 last_error 原因；
  4) _vram_fallback_model 放弃前须显存释放重探一次
     （同事故：空闲读数未反映回收把 8b/4b 候选全部饿死）。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from src.api import dialog as dialog_api

REPO = Path(__file__).resolve().parents[2]
DIALOG_PY = (REPO / "src" / "api" / "dialog.py").read_text(encoding="utf-8")
ENGINE_PY = (REPO / "src" / "services" / "inference" / "dialog_engine.py") \
    .read_text(encoding="utf-8")


class FakeWS:
    """_wait_vllm_booting 最小 WS 桩：收帧 + 可控连接态。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.client_state = 0  # 0=CONNECTED

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


class FakeEngine:
    """get_status 可编排的假引擎（支持定态/序列两种拧法）。"""

    def __init__(self, *, state: str = "ready", sequence: list[str] | None = None,
                 last_error: str = "") -> None:
        self.sequence = list(sequence or [])
        self.state = state
        self.last_error = last_error

    def get_status(self) -> dict[str, Any]:
        if self.sequence:
            self.state = self.sequence.pop(0)
        return {"state": self.state, "last_error": self.last_error}


# ── 行为测：等待循环快速出口 ──────────────────────────────────────

def test_error_state_exits_without_waiting_full_timeout(monkeypatch) -> None:
    """终态 error + vLLM 无在途 → 数秒内退出（非 200s 超时路径）。"""
    engine = FakeEngine(
        state="error",
        last_error="设备空闲显存 5.0GB 低于该模型可行下限 ~16.2GB")
    monkeypatch.setattr(dialog_api, "_vllm_inflight", lambda: False)
    ws = FakeWS()
    t0 = time.monotonic()
    result = asyncio.run(dialog_api._wait_vllm_booting(
        engine, ws, "sid-test", timeout_s=120.0))
    elapsed = time.monotonic() - t0
    assert result is False
    assert elapsed < 15.0, f"走了超时路径（耗时 {elapsed:.1f}s）"


def test_unavailable_state_also_fails_fast(monkeypatch) -> None:
    """unavailable（模型未找到）同属终态，同样快速退出。"""
    engine = FakeEngine(state="unavailable", last_error="对话模型未找到")
    monkeypatch.setattr(dialog_api, "_vllm_inflight", lambda: False)
    result = asyncio.run(dialog_api._wait_vllm_booting(
        engine, FakeWS(), "sid-test", timeout_s=60.0))
    assert result is False


def test_booting_to_ready_still_waits(monkeypatch) -> None:
    """正常冷启动（loading→ready）不被快速出口误杀。"""
    engine = FakeEngine(
        sequence=["loading", "loading", "ready"])
    monkeypatch.setattr(dialog_api, "_vllm_inflight", lambda: True)
    result = asyncio.run(dialog_api._wait_vllm_booting(
        engine, FakeWS(), "sid-test", timeout_s=60.0))
    assert result is True


def test_error_with_vllm_inflight_keeps_waiting(monkeypatch) -> None:
    """防误杀守卫：state=error 但 vLLM 在途（自动重试/收养窗口）须继续等。

    2026-09-02 P1 竞态语义：booting 预分配拒绝时引擎 state 可能还是
    上次的 error——此时在途 vLLM 仍可能就绪，不许秒退。
    """
    engine = FakeEngine(sequence=["error", "error", "ready"])
    monkeypatch.setattr(dialog_api, "_vllm_inflight", lambda: True)
    result = asyncio.run(dialog_api._wait_vllm_booting(
        engine, FakeWS(), "sid-test", timeout_s=60.0))
    assert result is True


# ── 行为测：回退候选放弃前重探 ───────────────────────────────────

class _FakeMgr:
    """estimate_vram_gb 按调用序回放估值（模拟 mgr 权威口径）。"""

    def __init__(self, estimates: list[float]) -> None:
        self._estimates = list(estimates)
        self.calls: list[str] = []

    def estimate_vram_gb(self, mid: str, category: str) -> float:
        self.calls.append(mid)
        return self._estimates.pop(0) if self._estimates else 99.0


def _bare_engine() -> Any:
    """跳过 __init__ 的裸引擎（_vram_fallback_model 不碰实例态）。"""
    import src.services.inference.dialog_engine as de
    return object.__new__(de.DialogEngine)


def test_fallback_reprobe_recovers_candidate(monkeypatch) -> None:
    """首探空闲饿死全部候选 → 释放后重探命中 → 不许直接放弃。"""
    import src.services.inference.dialog_engine as de
    import src.services.model_manager as mm

    engine = _bare_engine()
    probes = iter([5.0, 13.5])  # 首探 5.0；释放重探 13.5
    released: list[int] = []
    monkeypatch.setattr(de, "_cuda_free_gb", lambda: next(probes))
    monkeypatch.setattr(de, "_release_cuda_memory",
                        lambda: released.append(1))
    monkeypatch.setattr(
        de, "_effective_candidates",
        lambda: [("qwen3-vl-8b-awq", "qwen3-vl-8b-awq", 10.9),
                 ("qwen3-vl-4b", "qwen3-vl-4b", 9.0)])
    monkeypatch.setattr(engine, "_default_model_id",
                        lambda: "qwen35-9b-w4a16")
    monkeypatch.setattr(de, "_resolve_candidate_dir", lambda rel: Path("x"))
    fake_mgr = _FakeMgr([10.9, 9.0, 10.9])  # 首轮两个 + 重探一个
    monkeypatch.setattr(mm, "get_model_manager", lambda: fake_mgr)

    got = engine._vram_fallback_model("qwen35-9b-w4a16")
    assert got == "qwen3-vl-8b-awq", "释放重探后应命中 8b-awq"
    assert released, "放弃前必须执行 _release_cuda_memory"


def test_fallback_reprobe_still_starved_gives_up_honestly(monkeypatch) -> None:
    """重探后仍装不下（显存真被占死）→ 诚实返回空串。"""
    import src.services.inference.dialog_engine as de
    import src.services.model_manager as mm

    engine = _bare_engine()
    probes = iter([5.0, 5.0])
    monkeypatch.setattr(de, "_cuda_free_gb", lambda: next(probes))
    monkeypatch.setattr(de, "_release_cuda_memory", lambda: None)
    monkeypatch.setattr(
        de, "_effective_candidates",
        lambda: [("qwen3-vl-8b-awq", "qwen3-vl-8b-awq", 10.9),
                 ("qwen3-vl-4b", "qwen3-vl-4b", 9.0)])
    monkeypatch.setattr(engine, "_default_model_id",
                        lambda: "qwen35-9b-w4a16")
    monkeypatch.setattr(de, "_resolve_candidate_dir", lambda rel: Path("x"))
    monkeypatch.setattr(mm, "get_model_manager",
                        lambda: _FakeMgr([10.9, 9.0, 10.9, 9.0]))

    got = engine._vram_fallback_model("qwen35-9b-w4a16")
    assert got == ""


# ── 哨兵：caller 映射与文案（源码契约，防回退）─────────────────────

def test_caller_maps_terminal_states_and_carries_reason() -> None:
    assert 'status["state"] in ("error", "unavailable")' in DIALOG_PY, (
        "终态映射被删（unavailable 会被谎报为 MODEL_LOADING）")
    assert "对话模型加载失败：" in DIALOG_PY, "报错文案不再携带前缀"


def test_wait_loop_failfast_and_guard_sentinels() -> None:
    assert "_vllm_inflight" in DIALOG_PY, "在途守卫被删"
    assert "快速退出冷启动等待" in DIALOG_PY, "快速出口日志锚被删"
    # 守卫必须在终态判断处生效（同条件表达式中出现）
    assert '_st in ("error", "unavailable") and not _vllm_inflight()' \
        in DIALOG_PY, "快速出口失去在途守卫"


def test_engine_fallback_reprobe_sentinel() -> None:
    assert "放弃前显存重探（2026-10-25 事故）" in ENGINE_PY, (
        "回退放弃前重探被删")
    assert "显存释放后重探命中回退候选" in ENGINE_PY
