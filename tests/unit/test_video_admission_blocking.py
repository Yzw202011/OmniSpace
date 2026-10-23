"""视频显存准入硬闸单测（H3 blocking，2026-10-23 用户拍板「开工」）。

覆盖 _budget_admit 批3 翻闸后的全部分支：
- ComfyUI 存活 → 顾问放行（WDDM 按进程显存不可见，看不见不猜）
- 配置关（video_admission_blocking.enabled=false）→ 回退批2 顾问
- blocking GRANTED → 逻辑预留登记，_budget_release 归还
- WAIT → 让渡重试（次数受限）后 GRANTED → 放行
- WAIT 耗尽 → 末轮 DENY → VideoAdmissionDenied（文案带出路）
- 等待重试期间取消 → VideoTaskCancelled 透传（不被异常兜底吞掉）
"""
from __future__ import annotations

import pytest

import src.services.video_queue as vq
from src.services.inference.gpu_budget import (
    BudgetSnapshot,
    RequestResult,
    Verdict,
)
from src.services.video_queue import VideoAdmissionDenied, VideoTaskQueue


def _snap(budget_gb: float = 5.0) -> BudgetSnapshot:
    return BudgetSnapshot(
        device=0, available=True, total_gb=16.0, free_gb=budget_gb,
        used_gb=16.0 - budget_gb, ledger_gb=0.0, reserved_gb=0.0,
        external_gb=0.0, external_anomaly_gb=0.0)


class FakeBudget:
    """按脚本回放判定的假账本：verdicts 逐次消费，调用全记录。"""

    def __init__(self, verdicts: list[str]) -> None:
        self.verdicts = list(verdicts)
        self.calls: list[dict] = []
        self.reservations: list[tuple[int, float]] = []
        self.released_tokens: list[str] = []

    def request(self, feature: str, need_gb: float = 0.0, **kw) -> RequestResult:
        verdict = self.verdicts.pop(0) if self.verdicts else "granted"
        rec = {"feature": feature, "need_gb": need_gb, **kw}
        self.calls.append(rec)
        snap = _snap()
        return RequestResult(
            verdict=Verdict(verdict), feature=feature,
            need_gb=float(need_gb), device=0, advisory=bool(kw.get("advisory")),
            snapshot=snap,
            reason=f"fake-{verdict}", busy_token=f"tok{len(self.calls)}")

    def release(self, result=None, *, token: str = "", **kw) -> None:
        if token:
            self.released_tokens.append(token)

    def release_reservation(self, device: int, gb: float) -> None:
        self.reservations.append((device, gb))


def _task(task_id: str = "t1") -> dict:
    return {"task_id": task_id, "kind": "local"}


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch):
    """归零重试等待 + 关真实副作用，返回 patch 用的命名空间。"""
    monkeypatch.setattr(vq, "_ADMISSION_RETRY_WAIT_S", 0.0)
    monkeypatch.setattr(vq, "_ADMISSION_RETRY_MAX", 2)
    q = VideoTaskQueue()
    monkeypatch.setattr(q, "_sleep_vllm_for_generation", lambda: None)
    monkeypatch.setattr(q, "_unload_paint_pipeline", lambda: None)
    monkeypatch.setattr("src.config.get_config", lambda: {
        "video_admission_blocking": {"enabled": True}})
    return q


def _use_budget(monkeypatch: pytest.MonkeyPatch, fake: FakeBudget) -> None:
    monkeypatch.setattr(
        "src.services.inference.gpu_budget.get_gpu_budget", lambda: fake)


def test_comfy_alive_real_impl_never_started(env):
    """_comfy_alive 真身（不 patch）：本进程从未启动 ComfyUI → 不存活。

    钉住 pid/poll 是普通方法非 @property 的调用形态——此前误写
    proc.pid is not None（bound method 恒真）会把硬闸永久短路。
    """
    assert env._comfy_alive() is False


def test_comfy_alive_advisory_pass(env, monkeypatch):
    """ComfyUI 存活=占用不可见 → 顾问放行，零预留。"""
    monkeypatch.setattr(env, "_comfy_alive", lambda: True)
    fake = FakeBudget(["granted"])
    _use_budget(monkeypatch, fake)
    task = _task()
    env._budget_admit(task)
    assert fake.calls[0]["advisory"] is True
    assert task["_budget_reserve"] is None
    assert fake.reservations == []


def test_config_disabled_falls_back_advisory(env, monkeypatch):
    """video_admission_blocking.enabled=false → 整链回退批2 顾问。"""
    monkeypatch.setattr("src.config.get_config", lambda: {
        "video_admission_blocking": {"enabled": False}})
    monkeypatch.setattr(env, "_comfy_alive", lambda: False)
    fake = FakeBudget(["granted"])
    _use_budget(monkeypatch, fake)
    task = _task()
    env._budget_admit(task)
    assert fake.calls[0]["advisory"] is True
    assert task["_budget_reserve"] is None


def test_blocking_granted_reserves(env, monkeypatch):
    """ComfyUI 不在 + 额度足够 → 硬闸授予 + 逻辑预留登记，release 归还。"""
    monkeypatch.setattr(env, "_comfy_alive", lambda: False)
    fake = FakeBudget(["granted"])
    _use_budget(monkeypatch, fake)
    task = _task()
    env._budget_admit(task)
    assert fake.calls[0]["advisory"] is False
    # MAX=2 → 首两轮可让渡（WAIT 重试），末轮 allow_yield=False 终局
    assert fake.calls[0]["allow_yield"] is True
    device, gb = task["_budget_reserve"]
    assert gb == pytest.approx(13.0, abs=0.01)  # minimax-h3 覆盖表值
    env._budget_release(task)
    assert fake.released_tokens == [task["_budget_token"]]
    assert fake.reservations == [(device, gb)]


def test_blocking_wait_retries_then_granted(env, monkeypatch):
    """WAIT → 让渡重试 → GRANTED：放行且预留生效。"""
    monkeypatch.setattr(env, "_comfy_alive", lambda: False)
    fake = FakeBudget(["wait", "granted"])
    _use_budget(monkeypatch, fake)
    task = _task()
    env._budget_admit(task)
    assert [c["advisory"] for c in fake.calls] == [False, False]
    assert task["_budget_reserve"][1] == pytest.approx(13.0, abs=0.01)


def test_blocking_wait_exhausted_denies(env, monkeypatch):
    """WAIT×2 耗尽 → 末轮 DENY → VideoAdmissionDenied，文案带出路。"""
    monkeypatch.setattr(env, "_comfy_alive", lambda: False)
    fake = FakeBudget(["wait", "wait", "deny"])
    _use_budget(monkeypatch, fake)
    task = _task()
    with pytest.raises(VideoAdmissionDenied) as ei:
        env._budget_admit(task)
    assert "显存不足" in str(ei.value)
    assert "fake-deny" in str(ei.value)  # 末轮 DENY 理由（含出路）随文案透出
    # 拒单路径零登记零预留（无可释放项）
    assert "_budget_token" not in task
    assert fake.reservations == []


def test_cancel_during_wait_propagates(env, monkeypatch):
    """等待重试期间取消 → VideoTaskCancelled 透传（不被异常兜底吞成放行）。"""
    monkeypatch.setattr(env, "_comfy_alive", lambda: False)
    fake = FakeBudget(["wait", "granted"])
    _use_budget(monkeypatch, fake)

    def _cancel_on_yield() -> None:
        # 模拟运行中任务被用户取消（cancel 只对 _current/云端在跑任务置旗标）
        env._core._current = {"task_id": "t1"}
        env.cancel("t1")

    monkeypatch.setattr(env, "_sleep_vllm_for_generation", _cancel_on_yield)
    task = _task("t1")
    with pytest.raises(vq.VideoTaskCancelled):
        env._budget_admit(task)
    assert len(fake.calls) == 1  # 取消后未再发起新一轮判定
