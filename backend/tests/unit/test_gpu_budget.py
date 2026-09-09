"""gpu_budget 统一账本单测（显存调度机制批1/批2，2026-09-10）。

全 mock 读数（monkeypatch `read_physical_bytes`）——单测不触真 GPU
（GPU 测试活动门铁律）。批1 覆盖 snapshot 组装/external 对账/预留
表/对账钩子/忙碌登记/单例；批2 新增 request/release 准入决策表、
台账注入形态（复刻 model_manager 接线）、队列顾问接入、CPU_ASSIST
80% 档两态。方案真源=docs/显存调度机制方案-2026-09-10.md。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from backend.services.inference import gpu_budget
from backend.services.inference.gpu_budget import (
    EXTERNAL_ANOMALY_GB,
    BusyRegistry,
    GpuBudget,
    Verdict,
    get_busy_registry,
    get_gpu_budget,
)

_ROOT = Path(__file__).resolve().parents[3]  # 仓库根（unit/ → tests/ → backend/ → 根）

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest import MonkeyPatch


def _mock_read(
    free_gb: float, total_gb: float
) -> Callable[..., tuple[bool, int, int]]:
    def _fake(
        device: int = 0, *, torch_only: bool = False
    ) -> tuple[bool, int, int]:
        return (
            True,
            int(free_gb * 1024 ** 3),
            int(total_gb * 1024 ** 3),
        )

    return _fake


def _dead_read(
    device: int = 0, *, torch_only: bool = False
) -> tuple[bool, int, int]:
    return False, 0, 0


class TestSnapshot:
    def test_external_and_budget_math(self, monkeypatch: MonkeyPatch) -> None:
        """used=total−free；external=used−台账−预留；budget=free−预留。"""
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(13.4, 16.0))
        b.set_ledger_source(lambda device: 1.3)
        b.reserve(0, 0.5)
        snap = b.snapshot(0)
        assert snap.available is True
        assert snap.device == 0
        assert snap.total_gb == 16.0
        assert snap.free_gb == 13.4
        assert snap.used_gb == pytest.approx(2.6, abs=0.01)
        assert snap.ledger_gb == 1.3
        assert snap.reserved_gb == 0.5
        assert snap.external_gb == pytest.approx(0.8, abs=0.01)
        assert snap.budget_gb == pytest.approx(12.9, abs=0.01)

    def test_unavailable_never_raises(self, monkeypatch: MonkeyPatch) -> None:
        """账本失明（双通道读数失败）→ available=False 零值帧，不抛。"""
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _dead_read)
        b.set_ledger_source(lambda device: 5.0)  # 台账源也不参与
        snap = b.snapshot(0)
        assert snap.available is False
        assert snap.free_gb == 0.0
        assert snap.ledger_gb == 0.0
        assert snap.external_gb == 0.0
        assert snap.budget_gb == 0.0

    def test_no_ledger_source_degrades(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        """未注入台账源：ledger=0，external 退化为 used−reserved 仍可用。"""
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(12.0, 16.0))
        b.reserve(0, 1.0)
        snap = b.snapshot(0)
        assert snap.ledger_gb == 0.0
        assert snap.external_gb == pytest.approx(3.0, abs=0.01)

    def test_bad_ledger_source_is_zero(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        """台账源抛异常按 0 处理（账本失明不阻断业务）。"""
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(12.0, 16.0))

        def _boom(device: int) -> float:
            raise RuntimeError("ledger source down")

        b.set_ledger_source(_boom)
        assert b.snapshot(0).ledger_gb == 0.0


class TestReserve:
    def test_reserve_release_clamped_per_device(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(10.0, 16.0))
        b.reserve(0, 3.0)
        b.reserve(0, 2.0)
        assert b.snapshot(0).reserved_gb == 5.0
        b.release_reservation(0, 4.0)
        assert b.snapshot(0).reserved_gb == 1.0
        b.release_reservation(0, 99.0)  # 钳非负（重复/超额释放安全）
        assert b.snapshot(0).reserved_gb == 0.0
        b.reserve(1, 2.0)  # per-device 隔离
        assert b.snapshot(1).reserved_gb == 2.0
        assert b.snapshot(0).reserved_gb == 0.0
        b.clear_reservations(1)
        assert b.snapshot(1).reserved_gb == 0.0

    def test_budget_subtracts_reservation(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(10.0, 16.0))
        before = b.snapshot(0).budget_gb
        b.reserve(0, 4.5)
        assert b.snapshot(0).budget_gb == pytest.approx(before - 4.5, abs=0.01)


class TestReconcile:
    def test_hook_fires_above_anomaly_line(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        """账外占用超线（孤儿 vLLM 13.9GB 级形态）→ 钩子按注册序触发。"""
        b = GpuBudget()
        # free=2.0 → used=14.0，无台账 → external=14.0 ≥ 2.0
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(2.0, 16.0))
        fired: list[gpu_budget.BudgetSnapshot] = []
        b.add_reconcile_hook(fired.append)
        assert b.reconcile(device=0) is True
        assert len(fired) == 1
        assert fired[0].external_gb >= EXTERNAL_ANOMALY_GB

    def test_no_fire_below_line(self, monkeypatch: MonkeyPatch) -> None:
        """账外占用在常态波动内（桌面 ~2GB）→ 不触发。"""
        b = GpuBudget()
        monkeypatch.setattr(
            gpu_budget, "read_physical_bytes", _mock_read(14.5, 16.0))  # used 1.5
        fired: list[gpu_budget.BudgetSnapshot] = []
        b.add_reconcile_hook(fired.append)
        assert b.reconcile(device=0) is False
        assert fired == []

    def test_hook_exception_does_not_block_others(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(2.0, 16.0))
        calls: list[str] = []

        def _boom(snap: gpu_budget.BudgetSnapshot) -> None:
            calls.append("boom")

        def _ok(snap: gpu_budget.BudgetSnapshot) -> None:
            calls.append("ok")

        b.add_reconcile_hook(_boom)
        b.add_reconcile_hook(_ok)
        assert b.reconcile(device=0) is True
        assert calls == ["boom", "ok"]

    def test_unavailable_snapshot_never_reconciles(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        """账本失明不触发对账（无读数即无依据，保持现状行为）。"""
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _dead_read)
        fired: list[gpu_budget.BudgetSnapshot] = []
        b.add_reconcile_hook(fired.append)
        assert b.reconcile(device=0) is False
        assert fired == []


class TestRequest:
    """批2 准入决策表（顾问模式=只判定+登记；硬闸=批3 翻闸）。"""

    def test_granted_registers_busy_no_reservation(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(10.0, 16.0))
        r = b.request("paint", 8.0, advisory=True, note="t1")
        assert r.verdict is Verdict.GRANTED
        assert r.busy_token
        assert get_busy_registry().is_busy("paint") is True
        # 顾问模式不记逻辑预留（不改变 vllm_service 准入闸等现有判定）
        assert b.snapshot(0).reserved_gb == 0.0
        get_busy_registry().unregister(r.busy_token)

    def test_wait_suggests_ladder(self, monkeypatch: MonkeyPatch) -> None:
        """额度不足 → WAIT + 建议阶梯（external 超线提示 L0 对账）。"""
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(2.0, 16.0))
        r = b.request("paint", 8.0, advisory=True)
        assert r.verdict is Verdict.WAIT
        assert "L0" in r.reason  # used 14.0 无台账 → external 14 ≥ 线
        assert not r.busy_token  # 未开跑不登记
        assert get_busy_registry().is_busy("paint") is False

    def test_deny_when_no_yield(self, monkeypatch: MonkeyPatch) -> None:
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(2.0, 16.0))
        r = b.request("paint", 8.0, allow_yield=False)
        assert r.verdict is Verdict.DENY
        assert "出路" in r.reason

    def test_hard_mode_grants_and_reserves(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        """非顾问模式（批3 硬闸预留路径）：GRANTED 记预留，release 归还。"""
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(10.0, 16.0))
        r = b.request("paint", 8.0, advisory=False)
        assert r.verdict is Verdict.GRANTED
        assert b.snapshot(0).reserved_gb == 8.0
        b.release(r)
        assert b.snapshot(0).reserved_gb == 0.0
        assert get_busy_registry().is_busy("paint") is False

    def test_blind_snapshot_grants_like_legacy(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        """读数失明 → 放行（等价现状「直接看物理读数」最坏退路）。"""
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _dead_read)
        r = b.request("paint", 8.0)
        assert r.verdict is Verdict.GRANTED
        assert r.busy_token
        get_busy_registry().unregister(r.busy_token)

    def test_zero_need_registration_call(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        """队列登记式调用（need=0，批2 接入形态）恒 GRANTED。"""
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(0.5, 16.0))
        r = b.request("paint", 0.0, note="queue-task")
        assert r.verdict is Verdict.GRANTED
        get_busy_registry().unregister(r.busy_token)

    def test_release_noop_for_empty(self, monkeypatch: MonkeyPatch) -> None:
        """未走 request 的任务（如云道）release 为 no-op，不抛。"""
        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(10.0, 16.0))
        b.release(None, token="", note="cloud-or-legacy")


class TestLedgerWiring:
    def test_injection_pattern_matches_model_manager(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        """复刻 model_manager._wire_gpu_budget_ledger 注入形态：按卡汇总。"""
        loaded = {
            "m1": {"vram_gb": 1.5, "device": 0},
            "m2": {"vram_gb": 4.0, "device": 1},
        }

        def _ledger(device: int) -> float:
            return sum(
                float(e["vram_gb"])
                for e in loaded.values()
                if int(e.get("device", 0)) == int(device))

        b = GpuBudget()
        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(10.0, 16.0))
        b.set_ledger_source(_ledger)
        assert b.snapshot(0).ledger_gb == 1.5
        assert b.snapshot(1).ledger_gb == 4.0


class TestQueueBudgetAdmit:
    """批2 队列顾问接入（_budget_admit/_budget_release 为 self 无关
    纯函数形态，未绑定调用测试，不构造队列单例/线程）。"""

    def test_image_queue_admit_release(self, monkeypatch: MonkeyPatch) -> None:
        from backend.services.image_queue import ImageTaskQueue

        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(10.0, 16.0))
        task: dict = {"task_id": "t-img"}
        ImageTaskQueue._budget_admit(None, task)  # type: ignore[arg-type]
        assert task.get("_budget_token")
        assert get_busy_registry().is_busy("paint") is True
        ImageTaskQueue._budget_release(None, task)  # type: ignore[arg-type]
        assert get_busy_registry().is_busy("paint") is False

    def test_video_queue_admit_release(self, monkeypatch: MonkeyPatch) -> None:
        from backend.services.video_queue import VideoTaskQueue

        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _mock_read(10.0, 16.0))
        task: dict = {"task_id": "t-vid"}
        VideoTaskQueue._budget_admit(None, task)  # type: ignore[arg-type]
        assert get_busy_registry().is_busy("video_gen") is True
        VideoTaskQueue._budget_release(None, task)  # type: ignore[arg-type]
        assert get_busy_registry().is_busy("video_gen") is False

    def test_admit_survives_budget_failure(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        """账本抛异常 → 顾问失败不阻断任务（token 不落、release no-op）。"""
        from backend.services.image_queue import ImageTaskQueue

        def _boom(*args: object, **kwargs: object) -> tuple[bool, int, int]:
            raise RuntimeError("budget down")

        monkeypatch.setattr(gpu_budget, "read_physical_bytes", _boom)
        task: dict = {"task_id": "t-x"}
        ImageTaskQueue._budget_admit(None, task)  # type: ignore[arg-type]
        assert not task.get("_budget_token")
        ImageTaskQueue._budget_release(None, task)  # type: ignore[arg-type]


class TestCpuAssistTwoState:
    """批2 80% 档两态修复：生成期零成本动作，非生成期原行为。"""

    def test_locked_generation_period_skips_offload(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.services.scheduler.decision import DecisionEngine
        from backend.services.scheduler.dispatcher import TaskDispatcher

        calls: list[list[str]] = []
        monkeypatch.setattr(
            TaskDispatcher, "migrate_to_cpu",
            lambda self, layers: calls.append(list(layers)))
        monkeypatch.setattr(
            TaskDispatcher, "_feature_lock_active", staticmethod(lambda: True))
        # 隔离：供需帧记录路径不写事件库、不真读 GPU
        monkeypatch.setattr(
            GpuBudget, "snapshot",
            lambda self, device=0: gpu_budget.BudgetSnapshot(
                device=0, available=True, total_gb=16.0, free_gb=2.0,
                used_gb=14.0, ledger_gb=0.0, reserved_gb=0.0,
                external_gb=14.0))
        events: list[str] = []
        monkeypatch.setattr(
            "backend.services.event_log.log_event",
            lambda *a, **k: events.append(str(a[1])))

        DecisionEngine()._strategy_cpu_assist(TaskDispatcher())

        assert calls == []  # 生成期绝不动在途任务工作集
        assert events == ["gpu_vram_warning_active"]  # 但不再是空调区

    def test_idle_period_keeps_original_behavior(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.services.scheduler.decision import DecisionEngine
        from backend.services.scheduler.dispatcher import TaskDispatcher

        calls: list[list[str]] = []
        monkeypatch.setattr(
            TaskDispatcher, "migrate_to_cpu",
            lambda self, layers: calls.append(list(layers)))
        monkeypatch.setattr(
            TaskDispatcher, "_feature_lock_active", staticmethod(lambda: False))

        DecisionEngine()._strategy_cpu_assist(TaskDispatcher())

        assert calls == [["vae_decode", "postprocess"]]  # 原行为不变


class TestBusyRegistry:
    def test_register_unregister_is_busy(self) -> None:
        r = BusyRegistry()
        t_cloud = r.register("paint", "cloud", "task-1")
        assert r.is_busy() is True
        assert r.is_busy(kind="cloud") is True
        assert r.is_busy("dialog") is False
        t_local = r.register("dialog", "local", "chat")
        assert r.is_busy("dialog") is True
        assert r.is_busy("dialog", "cloud") is False
        assert len(r.entries()) == 2
        assert r.unregister(t_cloud) is True
        assert r.unregister(t_cloud) is False  # 幂等
        assert r.is_busy(kind="cloud") is False
        assert r.is_busy() is True  # 本地任务仍在
        r.unregister(t_local)
        assert r.is_busy() is False

    def test_entry_fields(self) -> None:
        r = BusyRegistry()
        token = r.register("video_gen", "cloud", "vid-42")
        (entry,) = r.entries()
        assert entry.feature == "video_gen"
        assert entry.kind == "cloud"
        assert entry.note == "vid-42"
        assert entry.since > 0.0
        assert token.startswith("video_gen#")


class TestCloudBusyAdmit:
    """批5 云任务上榜：队列云道执行处登记（不取本地锁但系统非空闲）。"""

    def test_image_queue_cloud_admit_release(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.services.image_queue import ImageTaskQueue

        task: dict = {"task_id": "cloud-img-1"}
        token = ImageTaskQueue._cloud_busy_admit(None, task)  # type: ignore[arg-type]
        assert token
        registry = get_busy_registry()
        assert registry.is_busy("paint", "cloud") is True
        ImageTaskQueue._cloud_busy_release(None, token)  # type: ignore[arg-type]
        assert registry.is_busy("paint", "cloud") is False

    def test_video_queue_cloud_admit_release(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.services.video_queue import VideoTaskQueue

        task: dict = {"task_id": "cloud-vid-1"}
        token = VideoTaskQueue._cloud_busy_admit(None, task)  # type: ignore[arg-type]
        assert token
        assert get_busy_registry().is_busy("video_gen", "cloud") is True
        VideoTaskQueue._cloud_busy_release(None, token)  # type: ignore[arg-type]
        assert get_busy_registry().is_busy("video_gen", "cloud") is False

    def test_admit_survives_registry_failure(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.services.image_queue import ImageTaskQueue

        def _boom() -> object:
            raise RuntimeError("registry down")

        monkeypatch.setattr(
            "backend.services.inference.gpu_budget.get_busy_registry", _boom)
        token = ImageTaskQueue._cloud_busy_admit(None, {"task_id": "x"})  # type: ignore[arg-type]
        assert token == ""  # 登记失败不影响云任务（空令牌 no-op）
        ImageTaskQueue._cloud_busy_release(None, "")  # type: ignore[arg-type]


class TestModelsVramEndpoint:
    """/models/vram 供需全景输出（批5）。"""

    def test_endpoint_includes_budget_and_busy(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.api import models as api_models

        fake_mgr = SimpleNamespace(
            get_gpu_status=lambda: {
                "available": False, "device": 0, "reserved_vram_gb": 0.0},
            get_loaded_models=lambda: [],
        )
        monkeypatch.setattr(api_models, "get_model_manager", lambda: fake_mgr)
        monkeypatch.setattr(api_models, "_vram_fragmentation", lambda: {})
        monkeypatch.setattr(
            gpu_budget, "read_physical_bytes", _mock_read(13.4, 16.0))
        get_busy_registry().register("paint", "cloud", "ep-test")

        resp = api_models.models_vram()

        assert resp.get("success") is True
        data = resp.get("data") or {}
        assert data["budget"]["free_gb"] == 13.4
        assert data["budget"]["budget_gb"] == 13.4
        assert {"feature", "kind", "note"} <= set(data["busy_registry"][0])
        assert data["busy_registry"][0]["kind"] == "cloud"

    def test_endpoint_degrades_when_budget_blind(
        self, monkeypatch: MonkeyPatch
    ) -> None:
        from backend.api import models as api_models

        fake_mgr = SimpleNamespace(
            get_gpu_status=lambda: {
                "available": False, "device": 0, "reserved_vram_gb": 0.0},
            get_loaded_models=lambda: [],
        )
        monkeypatch.setattr(api_models, "get_model_manager", lambda: fake_mgr)
        monkeypatch.setattr(api_models, "_vram_fragmentation", lambda: {})

        def _boom() -> object:
            raise RuntimeError("budget down")

        monkeypatch.setattr(
            "backend.services.inference.gpu_budget.get_gpu_budget", _boom)

        resp = api_models.models_vram()

        assert resp.get("success") is True  # 账本失明不阻断端点
        data = resp.get("data") or {}
        assert data["budget"] == {}
        assert data["busy_registry"] == []


class TestIdleConsumerWiring:
    """批5 联动源码哨兵：三处空闲消费方必须查登记簿（防回退）。"""

    def test_reclaim_checks_registry(self) -> None:
        src = (_ROOT / "backend" / "services" / "scheduler" / "__init__.py"
               ).read_text(encoding="utf-8")
        assert "get_busy_registry" in src, "空闲回收丢了登记簿联动"
        assert "is_busy()" in src

    def test_dialog_watchdog_checks_cloud(self) -> None:
        src = (_ROOT / "backend" / "services" / "inference"
               / "dialog_engine.py").read_text(encoding="utf-8")
        assert 'is_busy(kind="cloud")' in src, "对话看门狗丢了云任务联动"

    def test_all_idle_checks_registry(self) -> None:
        src = (_ROOT / "backend" / "services" / "scheduler" / "decision.py"
               ).read_text(encoding="utf-8")
        assert "get_busy_registry" in src, "ALL_IDLE 预加载丢了登记簿联动"

    def test_queues_register_cloud(self) -> None:
        for rel in ("image_queue.py", "video_queue.py"):
            src = (_ROOT / "backend" / "services" / rel
                   ).read_text(encoding="utf-8")
            assert "_cloud_busy_admit" in src, f"{rel} 云道登记被删"
            assert '"cloud"' in src


def test_singletons_are_single() -> None:
    assert get_gpu_budget() is get_gpu_budget()
    assert get_busy_registry() is get_busy_registry()
